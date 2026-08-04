import logging
import uuid
from pathlib import Path

import cv2
import fitz
import numpy as np

from colep_ai.ingestion.xlsx_group_resolver import XlsxGroupResolver
from colep_ai.core.logger import get_logger
from colep_ai.utils.blob_storage import (
    get_blob_client,
    blob_crop_key,
    blob_marked_key,
)
logger = get_logger(__name__)



def _iou(a: tuple, b: tuple) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    return inter / (area_a + area_b - inter)


def _dedupe_boxes(boxes: list[tuple], iou_thresh: float = 0.9) -> list[tuple]:
    kept: list[tuple] = []
    for b in boxes:
        if not any(_iou(b, k) > iou_thresh for k in kept):
            kept.append(b)
    return kept


def _union_box(boxes: list[tuple]) -> tuple:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    return (x0, y0, x1, y1)



def _rects_overlap(a: tuple, b: tuple) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


def _is_column_grouped(box: tuple, all_boxes: list[tuple], x_tol: int = 20, min_count: int = 3) -> bool:
    """
    Returns True if 3+ boxes (including this one) share nearly the same x0,
    indicating a vertical column layout where top/bottom labels would bleed
    into adjacent rows.
    """
    x0 = box[0]
    matches = sum(1 for b in all_boxes if abs(b[0] - x0) <= x_tol)
    return matches >= min_count


def _is_noise(box: tuple, canvas_w: int, canvas_h: int) -> bool:
    """
    Filter out non-content image regions:
      1. Branding: small area AND lives in top 8% of page (logo/header)
      2. Narrow strip: width/height < 0.3 — icon columns (action symbols etc.)
    """
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if h == 0:
        return True

    # narrow vertical strip (icon/action column)
    if (w / h) < 0.3:
        return True

    # small image in page header
    area_ratio = (w * h) / (canvas_w * canvas_h)
    if area_ratio < 0.01 and y1 < (canvas_h * 0.08):
        return True

    return False
def _is_brand_logo(
    box: tuple,
    canvas_w: int,
    canvas_h: int,
) -> bool:
    """
    Skip the Colep branding/logo image that appears in the page header.
    """

    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0

    if y1 > canvas_h * 0.20:
        return False

    if 150 <= w <= 320 and 50 <= h <= 110:
        logger.info(
            f"Skipping header logo ({w}x{h}) at ({x0}, {y0}, {x1}, {y1})"
        )
        return True

    return False

def _find_label_position(
    box: tuple,
    label_w: int,
    label_h: int,
    occupied: list[tuple],
    canvas_w: int,
    canvas_h: int,
    margin: int = 5,
    force_sides: bool = False,
) -> tuple:
    """
    box: (x0, y0, x1, y1) of the crop.
    occupied: all previously placed boxes + labels, to avoid collision.
    Tries: above -> below -> left -> right (clamped to canvas).
    force_sides: if True, skips above/below — used for column-grouped images
    to prevent labels bleeding into adjacent rows.
    """
    x0, y0, x1, y1 = box

    if force_sides:
        candidates = [
            (x0 - label_w - margin, y0, x0 - margin, y0 + label_h),          # left
            (x1 + margin, y0, x1 + label_w + margin, y0 + label_h),          # right
        ]
    else:
        candidates = [
            (x0, y0 - label_h - margin, x0 + label_w, y0 - margin),          # above
            (x0, y1 + margin, x0 + label_w, y1 + label_h + margin),          # below
            (x0 - label_w - margin, y0, x0 - margin, y0 + label_h),          # left
            (x1 + margin, y0, x1 + label_w + margin, y0 + label_h),          # right
        ]

    for cx0, cy0, cx1, cy1 in candidates:
        if cx0 < 0 or cy0 < 0 or cx1 > canvas_w or cy1 > canvas_h:
            continue
        rect = (cx0, cy0, cx1, cy1)
        if not any(_rects_overlap(rect, occ) for occ in occupied):
            return rect

    # nothing fully clear — fall back to original left-clamp behavior
    cx0 = max(0, min(x0 - label_w - margin, canvas_w - label_w))
    cy0 = max(0, min(y0, canvas_h - label_h))
    return (cx0, cy0, cx0 + label_w, cy0 + label_h)




def extract_images_from_page(
    pdf_path: str,
    page_number: int,  # 0-based
    source_file: str ,
    folder_name: str,
    dpi: int = 200,
    output_dir: str = "page_img",
    min_size: int = 20,
    xlsx_path: str | None = None,
    group_resolver: XlsxGroupResolver | None = None,
) -> list[dict]:
    output_dir = Path(output_dir)
    crops_dir = output_dir / "crops"
    marked_dir = output_dir / "marked"
    crops_dir.mkdir(parents=True, exist_ok=True)
    marked_dir.mkdir(parents=True, exist_ok=True)

    if group_resolver is None and xlsx_path is not None:
        group_resolver = XlsxGroupResolver(xlsx_path)

    try:
        doc = fitz.open(pdf_path)
        page = doc[page_number]
    except Exception:
        logger.exception(f"Failed to open page {page_number} of {pdf_path}")
        raise

    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    pix = page.get_pixmap(matrix=matrix)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR if pix.n == 4 else cv2.COLOR_RGB2BGR)
    visual = img.copy()

    page_label = f"page_{page_number + 1}"

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.55
    thickness = 2
    padding = 4

    raw_boxes = []
    for xref, *_ in page.get_images(full=True):
        for rect in page.get_image_rects(xref):
            x0, y0 = int(rect.x0 * zoom), int(rect.y0 * zoom)
            x1, y1 = int(rect.x1 * zoom), int(rect.y1 * zoom)
            if (x1 - x0) < min_size or (y1 - y0) < min_size:
                continue
            raw_boxes.append((xref, (x0, y0, x1, y1)))

    deduped_bboxes = _dedupe_boxes([b for _, b in raw_boxes])
    boxes = []
    seen = set()
    for xref, bbox in raw_boxes:
        if bbox in deduped_bboxes and bbox not in seen:
            boxes.append({"xref": xref, "bbox": bbox})
            seen.add(bbox)

    final_regions: list[tuple] = []

    if group_resolver is not None and boxes:
        xref_to_group = group_resolver.resolve_groups_for_page(boxes, img)

        groups: dict[str, list[tuple]] = {}
        standalone: list[tuple] = []
        for b in boxes:
            gid = xref_to_group.get(b["xref"])
            if gid is None:
                standalone.append(b["bbox"])
            else:
                groups.setdefault(gid, []).append(b["bbox"])

        final_regions.extend(standalone)
        for member_boxes in groups.values():
            final_regions.append(_union_box(member_boxes))
    else:
        final_regions = [b["bbox"] for b in boxes]

    final_regions.sort(key=lambda r: (r[1], r[0]))

    canvas_h, canvas_w = img.shape[:2]
    before = len(final_regions)
    final_regions = [
        r for r in final_regions
        if not _is_noise(r, canvas_w, canvas_h)
    ]
    filtered = before - len(final_regions)
    if filtered:
        logger.info(f"{page_label}: filtered {filtered} noise region(s) (branding/icon strips)")

    occupied_rects: list[tuple] = []
    saved = []

    for x0, y0, x1, y1 in final_regions:
        if x1 <= x0 or y1 <= y0:
            logger.warning(f"Skipping zero-size region on { page_label}: {(x0, y0, x1, y1)}" )
            continue

        name = f"{page_label}_{uuid.uuid4().hex[:4]}"

        if _is_brand_logo((x0, y0, x1, y1), canvas_w, canvas_h):
            continue

        crop = img[y0:y1, x0:x1]
        if y1 < img.shape[0] * 0.15:
            h, w = crop.shape[:2]
            print(
                f"HEADER: bbox={(x0,y0,x1,y1)} "
                f"size={w}x{h} "
                f"aspect={w/h:.2f}"
            )

        crop_path = crops_dir / f"{name}.png"
        if not cv2.imwrite(str(crop_path), crop):
            logger.error(f"Failed to write crop {crop_path}" )
            continue

        blob_key = blob_crop_key(folder_name,source_file, page_number + 1, f"{name}.png")
        get_blob_client().upload_file(crop_path, blob_key)


        cv2.rectangle(visual, (x0, y0), (x1, y1), (0, 255, 0), 3)

        (text_w, text_h), baseline = cv2.getTextSize(name, font, font_scale, thickness)
        label_w = text_w + 2 * padding
        label_h = text_h + baseline + 2 * padding

        force_sides = _is_column_grouped((x0, y0, x1, y1), final_regions)
        lx0, ly0, lx1, ly1 = _find_label_position(
            (x0, y0, x1, y1), label_w, label_h, occupied_rects,
            visual.shape[1], visual.shape[0],
            force_sides=force_sides,
        )

        cv2.rectangle(visual, (lx0, ly0), (lx1, ly1), (255, 255, 255), -1)
        cv2.rectangle(visual, (lx0, ly0), (lx1, ly1), (255, 0, 0), 2)
        cv2.putText(visual, name, (lx0 + padding, ly0 + text_h + padding), font, font_scale, (0, 0, 0), thickness, cv2.LINE_AA)

        occupied_rects.append((x0, y0, x1, y1))
        occupied_rects.append((lx0, ly0, lx1, ly1))

        saved.append({
            "image_id": name,
            "bbox": [x0, y0, x1, y1],
            "crop_path": str(crop_path),
        })

    marked_path = marked_dir / f"{page_label}_marked.png"
    if not cv2.imwrite(str(marked_path), visual):
        logger.error(f"Failed to write marked page image {marked_path}")
    else:
        blob_key = blob_marked_key(folder_name,source_file, page_number + 1, marked_path.name)
        get_blob_client().upload_file(marked_path, blob_key)
    doc.close()

    return saved