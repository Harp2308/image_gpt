# v1 works well 
import cv2
import numpy as np
import logging
from pathlib import Path
from collections import defaultdict
from typing import Any
import re
import cv2
from collections import defaultdict
from typing import Any
import hashlib

from colep_ai.core.logger import get_logger
from colep_ai.utils.blob_storage import (
    get_blob_client,
    blob_combined_key,
)
logger = get_logger(__name__)


def locate_crop(page, crop):
    gray_page = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
    gray_crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    sift = cv2.SIFT_create()

    kp_page, des_page = sift.detectAndCompute(gray_page, None)
    kp_crop, des_crop = sift.detectAndCompute(gray_crop, None)

    if des_page is None or des_crop is None:
        return None

    matcher = cv2.BFMatcher(cv2.NORM_L2)

    matches = matcher.knnMatch(des_crop, des_page, k=2)

    good = []
    for m, n in matches:
        if m.distance < 0.75 * n.distance:
            good.append(m)

    if len(good) < 10:
        return None

    src_pts = np.float32(
        [kp_crop[m.queryIdx].pt for m in good]
    ).reshape(-1, 1, 2)

    dst_pts = np.float32(
        [kp_page[m.trainIdx].pt for m in good]
    ).reshape(-1, 1, 2)

    H, _ = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5)

    if H is None:
        return None

    h, w = crop.shape[:2]

    corners = np.float32([
        [0, 0],
        [w, 0],
        [w, h],
        [0, h]
    ]).reshape(-1, 1, 2)

    transformed = cv2.perspectiveTransform(corners, H)

    x, y, bw, bh = cv2.boundingRect(transformed)

    return x, y, bw, bh


def reconstruct(page_path, crop_paths, bbox_index: dict[str, list[int]] | None = None):
    page = cv2.imread(page_path)

    placements = []

    min_x = 1e9
    min_y = 1e9
    max_x = -1e9
    max_y = -1e9

    # -------------------------
    # Compute coordinates
    # -------------------------
    for path in crop_paths:

        crop = cv2.imread(path)

        result = locate_crop(page, crop)

        if result is None:
            # SIFT failed — try bbox fallback
            image_id = Path(path).stem
            bbox = (bbox_index or {}).get(image_id)
            if bbox is None:
                logger.warning(f"SIFT failed and no bbox for {image_id} — skipping crop")
                continue
            x, y = bbox[0], bbox[1]
            logger.info(f"SIFT failed for {image_id} — using bbox fallback ({x}, {y})")
        else:
            x, y, _, _ = result

        h, w = crop.shape[:2]

        placements.append((crop, x, y))

        min_x = min(min_x, x)
        min_y = min(min_y, y)

        max_x = max(max_x, x + w)
        max_y = max(max_y, y + h)

    if len(placements) == 0:
        return None

    # -------------------------
    # Create NEW canvas
    # -------------------------
    canvas_w = int(max_x - min_x)
    canvas_h = int(max_y - min_y)

    canvas = np.ones(
        (canvas_h, canvas_w, 3),
        dtype=np.uint8
    ) * 255

    # -------------------------
    # Paste ORIGINAL crops
    # -------------------------
    for crop, x, y in placements:

        h, w = crop.shape[:2]

        xx = int(x - min_x)
        yy = int(y - min_y)

        canvas[
            yy:yy + h,
            xx:xx + w
        ] = crop

    return canvas


def make_step_uid(image_ids: list[str], length: int = 4) -> str:
    """Deterministic — same image_ids always produce the same uid. Idempotent reruns."""
    joined = "".join(sorted(image_ids))
    return hashlib.sha1(joined.encode()).hexdigest()[:length]

def group_image_ids_by_step(results: list[dict[str, Any]]) -> dict[Any, list[str]]:
    """
    Groups image_ids by step_number.
    Images with step_number=None go into 'unmapped' bucket.
    """
    grouped: dict[Any, list[str]] = defaultdict(list)
    
    for item in results:
        key = item["step_number"] if item["step_number"] is not None else "unmapped"
        grouped[key].append(item["image_id"])
    print("***"*50)
    print(dict(grouped))
    print("***"*50)

    return dict(grouped)

def group_image_ids_by_entry(entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    """
    Groups image_ids by entry_id.
    Entries with empty image_ids are skipped (nothing to combine).
    """
    grouped: dict[str, list[str]] = {}

    for entry in entries:
        image_ids = entry.get("image_ids") or []
        if not image_ids:
            continue
        grouped[entry["entry_id"]] = image_ids

    return grouped


def get_combined_output_path(
    combined_root: Path,
    page_num: int,
    step_key: Any,
    image_ids: list[str],
) -> Path:
    """
    Builds: combined/page_{page_num}/page_{page_num}_{step_key}_{uid}.png
    Creates the page-level subfolder if missing. Does not write the file.
    """
    page_dir = combined_root / f"page_{page_num}"
    page_dir.mkdir(parents=True, exist_ok=True)
    uid = make_step_uid(image_ids)
    return page_dir / f"page_{page_num}_{step_key}_{uid}.png"

def reconstruct_all_steps(
    grouped: dict[Any, list[str]],
    page_num: int,
    crops_root: Path,
    output_dir: Path,
    source_file: str,
    bbox_index: dict[str, list[int]] | None = None,
    reconstruct_fn=reconstruct,
) -> dict[Any, dict]:  # changed: value is now a dict, not bool
    page_dir = crops_root / f"page_{page_num}"
    page_path = page_dir / "marked" / f"page_{page_num}_marked.png"
    crops_dir = page_dir / "crops"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not page_path.exists():
        raise FileNotFoundError(f"Marked page image missing: {page_path}")

    status: dict[Any, dict] = {}

    for step_key, image_ids in grouped.items():
        if len(image_ids) < 2:
            logger.info(f"Step {step_key}: single image, no combine needed — skipped")
            status[step_key] = {"success": None, "is_combined": False, "combined_image": None}
            continue

        crop_paths = [crops_dir / f"{img_id}.png" for img_id in image_ids]
        missing = [p for p in crop_paths if not p.exists()]
        if missing:
            logger.error(f"Step {step_key}: missing crops {missing} — skipping")
            status[step_key] = {"success": False, "is_combined": False, "combined_image": None}
            continue

        result = reconstruct_fn(str(page_path), [str(p) for p in crop_paths], bbox_index=bbox_index)
        if result is None:
            logger.error(f"Step {step_key}: reconstruct_fn returned None — skipping")
            status[step_key] = {"success": False, "is_combined": False, "combined_image": None}
            continue

        out_path = get_combined_output_path(output_dir, page_num, step_key, image_ids)
        cv2.imwrite(str(out_path), result)
        blob_key = blob_combined_key(source_file, page_num, out_path.name)
        get_blob_client().upload_file(out_path, blob_key)
        logger.info(f"Step {step_key}: saved {out_path}")
        status[step_key] = {"success": True, "is_combined": True, "combined_image": out_path.name}

    return status