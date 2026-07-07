import logging
from pathlib import Path

import cv2
import fitz
import numpy as np

from xlsx_group_resolver import XlsxGroupResolver

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
    """Drops near-identical bboxes. Fixes duplicate crops from SMask/transparency xrefs
    that overlap the same visible picture."""
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


def extract_images_from_page(
    pdf_path: str,
    page_number: int,  # 0-based
    dpi: int = 200,
    output_dir: str = "page_img",
    min_size: int = 20,
    xlsx_path: str | None = None,
    group_resolver: XlsxGroupResolver | None = None,
) -> list[dict]:
    """
    Pass group_resolver (built once per workbook) when processing multiple pages -
    building it re-parses the whole xlsx, don't do that per page.
    xlsx_path is a convenience fallback if you only have one page to process.
    """
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

    def draw_label(image, text, x, y):
        (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)
        box_w = tw + 2 * padding
        box_h = th + baseline + 2 * padding
        x = max(0, min(x, image.shape[1] - box_w))
        y = max(0, min(y, image.shape[0] - box_h))
        cv2.rectangle(image, (x, y), (x + box_w, y + box_h), (255, 255, 255), -1)
        cv2.rectangle(image, (x, y), (x + box_w, y + box_h), (255, 0, 0), 2)
        cv2.putText(image, text, (x + padding, y + th + padding), font, font_scale, (0, 0, 0), thickness, cv2.LINE_AA)
        return box_h

    # ---- collect raw boxes with xref, then dedupe ----
    raw_boxes = []  # (xref, (x0,y0,x1,y1))
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

    # ---- group-aware merge ----
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

    final_regions.sort(key=lambda r: (r[1], r[0]))  # reading order

    saved = []
    for idx, (x0, y0, x1, y1) in enumerate(final_regions, start=1):
        if x1 <= x0 or y1 <= y0:
            logger.warning("Skipping zero-size region on %s: %s", page_label, (x0, y0, x1, y1))
            continue

        name = f"{page_label}_{idx:02d}"
        crop = img[y0:y1, x0:x1]

        crop_path = crops_dir / f"{name}.png"
        if not cv2.imwrite(str(crop_path), crop):
            logger.error("Failed to write crop %s", crop_path)
            continue

        cv2.rectangle(visual, (x0, y0), (x1, y1), (0, 255, 0), 3)

        (text_w, text_h), baseline = cv2.getTextSize(name, font, font_scale, thickness)
        box_w = text_w + 2 * padding
        box_h = text_h + baseline + 2 * padding
        margin = 5

        label_x = x0 - box_w - margin
        label_y = y0
        if label_x < 0:
            label_x = x1 + margin
        if label_x + box_w > visual.shape[1]:
            label_x = visual.shape[1] - box_w - margin
        label_y = min(label_y, visual.shape[0] - box_h - margin)

        draw_label(visual, name, label_x, label_y)

        saved.append(
            {
                "image_id": name,
                "bbox": [x0, y0, x1, y1],
                "crop_path": str(crop_path),
            }
        )

    marked_path = marked_dir / f"{page_label}_marked.png"
    if not cv2.imwrite(str(marked_path), visual):
        logger.error("Failed to write marked page image %s", marked_path)

    doc.close()

    return saved

# import fitz
# import cv2
# import numpy as np
# import uuid
# from pathlib import Path


# def extract_images_from_page(
#     pdf_path: str,
#     page_number: int,
#     dpi: int = 200,
#     output_dir: str = "page_img",
#     min_size: int = 20,
# ) -> list[dict]:

#     output_dir = Path(output_dir)
#     crops_dir = output_dir / "crops"
#     marked_dir = output_dir / "marked"

#     crops_dir.mkdir(parents=True, exist_ok=True)
#     marked_dir.mkdir(parents=True, exist_ok=True)

#     doc = fitz.open(pdf_path)
#     page = doc[page_number]

#     zoom = dpi / 72
#     matrix = fitz.Matrix(zoom, zoom)

#     pix = page.get_pixmap(matrix=matrix)
#     img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
#         pix.height, pix.width, pix.n
#     )

#     if pix.n == 4:
#         img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
#     else:
#         img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

#     visual = img.copy()

#     page_label = f"page_{page_number + 1}"

#     font = cv2.FONT_HERSHEY_SIMPLEX
#     font_scale = 0.55
#     thickness = 2
#     padding = 4

#     def draw_label(image, text, x, y):
#         (tw, th), baseline = cv2.getTextSize(
#             text, font, font_scale, thickness
#         )

#         box_w = tw + 2 * padding
#         box_h = th + baseline + 2 * padding

#         # keep inside image
#         x = max(0, min(x, image.shape[1] - box_w))
#         y = max(0, min(y, image.shape[0] - box_h))

#         # white background
#         cv2.rectangle(
#             image,
#             (x, y),
#             (x + box_w, y + box_h),
#             (255, 255, 255),
#             -1,
#         )

#         # blue border
#         cv2.rectangle(
#             image,
#             (x, y),
#             (x + box_w, y + box_h),
#             (255, 0, 0),
#             2,
#         )

#         # black text
#         cv2.putText(
#             image,
#             text,
#             (x + padding, y + th + padding),
#             font,
#             font_scale,
#             (0, 0, 0),
#             thickness,
#             cv2.LINE_AA,
#         )

#         return box_h

#     boxes = []

#     for xref, *_ in page.get_images(full=True):
#         for rect in page.get_image_rects(xref):
#             x0 = int(rect.x0 * zoom)
#             y0 = int(rect.y0 * zoom)
#             x1 = int(rect.x1 * zoom)
#             y1 = int(rect.y1 * zoom)

#             if (x1 - x0) < min_size or (y1 - y0) < min_size:
#                 continue

#             boxes.append((x0, y0, x1, y1))

#     boxes.sort(key=lambda b: (b[1], b[0]))

#     saved = []

#     for x0, y0, x1, y1 in boxes:

#         name = f"{page_label}_{uuid.uuid4().hex[:4]}"

#         crop = img[y0:y1, x0:x1]
#         crop_path = crops_dir / f"{name}.png"
#         cv2.imwrite(str(crop_path), crop)

#         # Draw bounding box
#         cv2.rectangle(visual, (x0, y0), (x1, y1), (0, 255, 0), 3)

#         (_, text_h), baseline = cv2.getTextSize(
#             name, font, font_scale, thickness
#         )
#         label_height = text_h + baseline + 2 * padding

#         # Draw above if possible
#         # if y0 > label_height + 5:
#         #     draw_label(visual, name, x0, y0 - label_height - 5)
#         # else:
#         #     # otherwise below
#         #     draw_label(visual, name, x0, y1 + 5)
#         (text_w, text_h), baseline = cv2.getTextSize(
#             name, font, font_scale, thickness
#         )

#         box_w = text_w + 2 * padding
#         box_h = text_h + baseline + 2 * padding

#         margin = 5

#         # Try left
#         label_x = x0 - box_w - margin
#         label_y = y0

#         # If left goes outside, use right
#         if label_x < 0:
#             label_x = x1 + margin

#         # If right also goes outside, clamp it
#         if label_x + box_w > visual.shape[1]:
#             label_x = visual.shape[1] - box_w - margin

#         # Keep inside vertically
#         label_y = min(label_y, visual.shape[0] - box_h - margin)

#         draw_label(visual, name, label_x, label_y)

#         saved.append(
#             {
#                 "image_id": name,
#                 "bbox": [x0, y0, x1, y1],
#                 "crop_path": str(crop_path),
#             }
#         )

#     cv2.imwrite(str(marked_dir / f"{page_label}_marked.png"), visual)

#     doc.close()

#     return saved


# import fitz, cv2, numpy as np, uuid
# from pathlib import Path

# def extract_images_from_page(
#     pdf_path: str,
#     page_number: int,        # 0-based
#     dpi: int = 200,
#     output_dir: str = "page_img",
#     min_size: int = 20       # skip icons/noise below this px threshold
# ) -> list[dict]:

#     output_dir = Path(output_dir)
#     crops_dir = output_dir / "crops"
#     marked_dir = output_dir / "marked"
#     crops_dir.mkdir(parents=True, exist_ok=True)
#     marked_dir.mkdir(parents=True, exist_ok=True)

#     doc = fitz.open(pdf_path)
#     page = doc[page_number]
#     zoom = dpi / 72
#     matrix = fitz.Matrix(zoom, zoom)

#     pix = page.get_pixmap(matrix=matrix)
#     img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
#     img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR if pix.n == 4 else cv2.COLOR_RGB2BGR)
#     visual = img.copy()

#     page_label = f"page_{page_number + 1}"
    
#     boxes = []

#     for xref, *_ in page.get_images(full=True):
#         for rect in page.get_image_rects(xref):
#             x0, y0 = int(rect.x0 * zoom), int(rect.y0 * zoom)
#             x1, y1 = int(rect.x1 * zoom), int(rect.y1 * zoom)
#             if (x1 - x0) < min_size or (y1 - y0) < min_size:
#                 continue
#             boxes.append((x0, y0, x1, y1))

#     boxes.sort(key=lambda r: (r[1], r[0]))

#     saved = []
#     for x0, y0, x1, y1 in boxes:
#         name = f"{page_label}_{uuid.uuid4().hex[:4]}"
#         crop_path = crops_dir / f"{name}.png"
#         cv2.imwrite(str(crop_path), img[y0:y1, x0:x1])
#         cv2.rectangle(visual, (x0, y0), (x1, y1), (0, 255, 0), 3)
        
#         cv2.putText(visual, name, (x0, max(y0 - 10, 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)

#         saved.append({"image_id": name, "bbox": [x0, y0, x1, y1], "crop_path": str(crop_path)})

#     cv2.imwrite(str(marked_dir / f"{page_label}_marked.png"), visual)
#     doc.close()
#     return saved


# # usage 
# # extract_images_from_page(
#     # pdf_path="output_images\e1.pdf",
#     # page_number=3,   # page 2 (0-based indexing)
# # )