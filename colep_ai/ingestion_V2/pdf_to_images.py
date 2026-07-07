import logging
import uuid
from pathlib import Path

import cv2
import fitz
import numpy as np

from colep_ai.ingestion.xlsx_group_resolver import XlsxGroupResolver

logger = logging.getLogger(__name__)


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

# v1
# def extract_images_from_page(
#     pdf_path: str,
#     page_number: int,  # 0-based
#     dpi: int = 200,
#     output_dir: str = "page_img",
#     min_size: int = 20,
#     xlsx_path: str | None = None,
#     group_resolver: XlsxGroupResolver | None = None,
# ) -> list[dict]:
#     output_dir = Path(output_dir)
#     crops_dir = output_dir / "crops"
#     marked_dir = output_dir / "marked"
#     crops_dir.mkdir(parents=True, exist_ok=True)
#     marked_dir.mkdir(parents=True, exist_ok=True)

#     if group_resolver is None and xlsx_path is not None:
#         group_resolver = XlsxGroupResolver(xlsx_path)

#     try:
#         doc = fitz.open(pdf_path)
#         page = doc[page_number]
#     except Exception:
#         logger.exception("Failed to open page %s of %s", page_number, pdf_path)
#         raise

#     zoom = dpi / 72
#     matrix = fitz.Matrix(zoom, zoom)

#     pix = page.get_pixmap(matrix=matrix)
#     img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
#     img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR if pix.n == 4 else cv2.COLOR_RGB2BGR)
#     visual = img.copy()

#     page_label = f"page_{page_number + 1}"

#     font = cv2.FONT_HERSHEY_SIMPLEX
#     font_scale = 0.55
#     thickness = 2
#     padding = 4

#     def draw_label(image, text, x, y):
#         (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)
#         box_w = tw + 2 * padding
#         box_h = th + baseline + 2 * padding
#         x = max(0, min(x, image.shape[1] - box_w))
#         y = max(0, min(y, image.shape[0] - box_h))
#         cv2.rectangle(image, (x, y), (x + box_w, y + box_h), (255, 255, 255), -1)
#         cv2.rectangle(image, (x, y), (x + box_w, y + box_h), (255, 0, 0), 2)
#         cv2.putText(image, text, (x + padding, y + th + padding), font, font_scale, (0, 0, 0), thickness, cv2.LINE_AA)
#         return box_h

#     raw_boxes = []
#     for xref, *_ in page.get_images(full=True):
#         for rect in page.get_image_rects(xref):
#             x0, y0 = int(rect.x0 * zoom), int(rect.y0 * zoom)
#             x1, y1 = int(rect.x1 * zoom), int(rect.y1 * zoom)
#             if (x1 - x0) < min_size or (y1 - y0) < min_size:
#                 continue
#             raw_boxes.append((xref, (x0, y0, x1, y1)))

#     deduped_bboxes = _dedupe_boxes([b for _, b in raw_boxes])
#     boxes = []
#     seen = set()
#     for xref, bbox in raw_boxes:
#         if bbox in deduped_bboxes and bbox not in seen:
#             boxes.append({"xref": xref, "bbox": bbox})
#             seen.add(bbox)

#     final_regions: list[tuple] = []

#     if group_resolver is not None and boxes:
#         xref_to_group = group_resolver.resolve_groups_for_page(boxes, img)

#         groups: dict[str, list[tuple]] = {}
#         standalone: list[tuple] = []
#         for b in boxes:
#             gid = xref_to_group.get(b["xref"])
#             if gid is None:
#                 standalone.append(b["bbox"])
#             else:
#                 groups.setdefault(gid, []).append(b["bbox"])

#         final_regions.extend(standalone)
#         for member_boxes in groups.values():
#             final_regions.append(_union_box(member_boxes))
#     else:
#         final_regions = [b["bbox"] for b in boxes]

#     final_regions.sort(key=lambda r: (r[1], r[0]))

#     saved = []
#     for x0, y0, x1, y1 in final_regions:
#         if x1 <= x0 or y1 <= y0:
#             logger.warning("Skipping zero-size region on %s: %s", page_label, (x0, y0, x1, y1))
#             continue

#         name = f"{page_label}_{uuid.uuid4().hex[:4]}"
#         crop = img[y0:y1, x0:x1]

#         crop_path = crops_dir / f"{name}.png"
#         if not cv2.imwrite(str(crop_path), crop):
#             logger.error("Failed to write crop %s", crop_path)
#             continue

#         cv2.rectangle(visual, (x0, y0), (x1, y1), (0, 255, 0), 3)

#         (text_w, text_h), baseline = cv2.getTextSize(name, font, font_scale, thickness)
#         box_w = text_w + 2 * padding
#         box_h = text_h + baseline + 2 * padding
#         margin = 5

#         label_x = x0 - box_w - margin
#         label_y = y0
#         if label_x < 0:
#             label_x = x1 + margin
#         if label_x + box_w > visual.shape[1]:
#             label_x = visual.shape[1] - box_w - margin
#         label_y = min(label_y, visual.shape[0] - box_h - margin)

#         draw_label(visual, name, label_x, label_y)

#         saved.append({
#             "image_id": name,
#             "bbox": [x0, y0, x1, y1],
#             "crop_path": str(crop_path),
#         })

#     marked_path = marked_dir / f"{page_label}_marked.png"
#     if not cv2.imwrite(str(marked_path), visual):
#         logger.error("Failed to write marked page image %s", marked_path)

#     doc.close()

#     return saved



def _rects_overlap(a: tuple, b: tuple) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


def _find_label_position(
    box: tuple,
    label_w: int,
    label_h: int,
    occupied: list[tuple],
    canvas_w: int,
    canvas_h: int,
    margin: int = 5,
) -> tuple:
    """
    box: (x0, y0, x1, y1) of the crop.
    occupied: all previously placed boxes + labels, to avoid collision.
    Tries: above -> below -> left -> right (clamped to canvas).
    """
    x0, y0, x1, y1 = box

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
        logger.exception("Failed to open page %s of %s", page_number, pdf_path)
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

    occupied_rects: list[tuple] = []
    saved = []

    for x0, y0, x1, y1 in final_regions:
        if x1 <= x0 or y1 <= y0:
            logger.warning("Skipping zero-size region on %s: %s", page_label, (x0, y0, x1, y1))
            continue

        name = f"{page_label}_{uuid.uuid4().hex[:4]}"
        crop = img[y0:y1, x0:x1]

        crop_path = crops_dir / f"{name}.png"
        if not cv2.imwrite(str(crop_path), crop):
            logger.error("Failed to write crop %s", crop_path)
            continue

        cv2.rectangle(visual, (x0, y0), (x1, y1), (0, 255, 0), 3)

        (text_w, text_h), baseline = cv2.getTextSize(name, font, font_scale, thickness)
        label_w = text_w + 2 * padding
        label_h = text_h + baseline + 2 * padding

        lx0, ly0, lx1, ly1 = _find_label_position(
            (x0, y0, x1, y1), label_w, label_h, occupied_rects,
            visual.shape[1], visual.shape[0]
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
        logger.error("Failed to write marked page image %s", marked_path)

    doc.close()

    return saved