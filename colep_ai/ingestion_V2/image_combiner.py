# v1 works well 
import cv2
import numpy as np
import logging
from pathlib import Path
from collections import defaultdict
from typing import Any

import cv2

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

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


def reconstruct(page_path, crop_paths):
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
            print(f"Could not locate: {path}")
            continue

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

from collections import defaultdict
from typing import Any
import hashlib

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
    reconstruct_fn=reconstruct,
) -> dict[Any, bool]:
    page_dir = crops_root / f"page_{page_num}"
    page_path = page_dir / "marked" / f"page_{page_num}_marked.png"
    crops_dir = page_dir / "crops"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not page_path.exists():
        raise FileNotFoundError(f"Marked page image missing: {page_path}")

    status: dict[Any, bool] = {}

    for step_key, image_ids in grouped.items():
        if len(image_ids) < 2:
            logger.info(f"Step {step_key}: single image, no combine needed — skipped")
            status[step_key] = None  # not True (success) or False (failure) — not applicable
            continue

        crop_paths = [crops_dir / f"{img_id}.png" for img_id in image_ids]
        missing = [p for p in crop_paths if not p.exists()]
        if missing:
            logger.error(f"Step {step_key}: missing crops {missing} — skipping")
            status[step_key] = False
            continue

        result = reconstruct_fn(str(page_path), [str(p) for p in crop_paths])
        if result is None:
            logger.error(f"Step {step_key}: reconstruct_fn returned None — skipping")
            status[step_key] = False
            continue

        out_path = get_combined_output_path(output_dir, page_num, step_key, image_ids)
        cv2.imwrite(str(out_path), result)
        logger.info(f"Step {step_key}: saved {out_path}")
        status[step_key] = True

    return status

# v1 reconstrcuts all even when not needed
# def reconstruct_all_steps(
#     grouped: dict[Any, list[str]],
#     page_num: int,
#     crops_root: Path,
#     output_dir: Path,
#     reconstruct_fn=reconstruct,
# ) -> dict[Any, bool]:
#     """
#     Loops all steps in grouped dict, builds crop paths, calls reconstruct_fn,
#     saves one PNG per step. Returns {step_key: success_bool}.
#     """
#     page_dir = crops_root / f"page_{page_num}"
#     page_path = page_dir / "marked" / f"page_{page_num}_marked.png"
#     crops_dir = page_dir / "crops"
#     output_dir.mkdir(parents=True, exist_ok=True)

#     if not page_path.exists():
#         raise FileNotFoundError(f"Marked page image missing: {page_path}")

#     status: dict[Any, bool] = {}

#     for step_key, image_ids in grouped.items():
#         crop_paths = [crops_dir / f"{img_id}.png" for img_id in image_ids]

#         missing = [p for p in crop_paths if not p.exists()]
#         if missing:
#             logger.error(f"Step {step_key}: missing crops {missing} — skipping")
#             status[step_key] = False
#             continue

#         result = reconstruct_fn(str(page_path), [str(p) for p in crop_paths])

#         if result is None:
#             logger.error(f"Step {step_key}: reconstruct_fn returned None — skipping")
#             status[step_key] = False
#             continue
#         uid = make_step_uid(image_ids)

#         out_path = output_dir / f"page_{page_num}_{step_key}_{uid}.png"
#         cv2.imwrite(str(out_path), result)
#         logger.info(f"Step {step_key}: saved {out_path}")
#         status[step_key] = True

#     return status

def get_page_num(data: dict[str, Any]) -> int:
    """Single source of truth — never pass page_num separately again."""
    pages = {item["metadata"]["page"] for item in data["results"]}
    if len(pages) != 1:
        raise ValueError(f"Mixed pages in one result file: {pages}")
    return pages.pop()

# import json

# if __name__ == "__main__":
#     PATH = r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\outputsSONNET\results\e1_page_4_result.json"
#     with open(PATH, "r", encoding="utf-8") as f:
#         data = json.load(f)

#     grouped = group_image_ids_by_step(data["results"])
#     page_num = get_page_num(data)  # <-- 2, derived, not guessed

#     base_dir = Path(r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\outputsSONNET\crops")  # root, not page_2\crops
#     output_dir = Path(r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\outputsSONNET\test_reconstructed")

#     status = reconstruct_all_steps(grouped, page_num=page_num, base_dir=base_dir,
#                                     output_dir=output_dir, reconstruct_fn=reconstruct)

#     failed = [k for k, v in status.items() if not v]
#     if failed:
#         logger.warning(f"Steps failed: {failed}")
