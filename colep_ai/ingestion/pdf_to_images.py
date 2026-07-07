import fitz
import cv2
import numpy as np
import uuid
from pathlib import Path


def extract_images_from_page(
    pdf_path: str,
    page_number: int,
    dpi: int = 200,
    output_dir: str = "page_img",
    min_size: int = 20,
) -> list[dict]:

    output_dir = Path(output_dir)
    crops_dir = output_dir / "crops"
    marked_dir = output_dir / "marked"

    crops_dir.mkdir(parents=True, exist_ok=True)
    marked_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    page = doc[page_number]

    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    pix = page.get_pixmap(matrix=matrix)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n
    )

    if pix.n == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    else:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

    visual = img.copy()

    page_label = f"page_{page_number + 1}"

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.55
    thickness = 2
    padding = 4

    def draw_label(image, text, x, y):
        (tw, th), baseline = cv2.getTextSize(
            text, font, font_scale, thickness
        )

        box_w = tw + 2 * padding
        box_h = th + baseline + 2 * padding

        # keep inside image
        x = max(0, min(x, image.shape[1] - box_w))
        y = max(0, min(y, image.shape[0] - box_h))

        # white background
        cv2.rectangle(
            image,
            (x, y),
            (x + box_w, y + box_h),
            (255, 255, 255),
            -1,
        )

        # blue border
        cv2.rectangle(
            image,
            (x, y),
            (x + box_w, y + box_h),
            (255, 0, 0),
            2,
        )

        # black text
        cv2.putText(
            image,
            text,
            (x + padding, y + th + padding),
            font,
            font_scale,
            (0, 0, 0),
            thickness,
            cv2.LINE_AA,
        )

        return box_h

    boxes = []

    for xref, *_ in page.get_images(full=True):
        for rect in page.get_image_rects(xref):
            x0 = int(rect.x0 * zoom)
            y0 = int(rect.y0 * zoom)
            x1 = int(rect.x1 * zoom)
            y1 = int(rect.y1 * zoom)

            if (x1 - x0) < min_size or (y1 - y0) < min_size:
                continue

            boxes.append((x0, y0, x1, y1))

    boxes.sort(key=lambda b: (b[1], b[0]))

    saved = []

    for x0, y0, x1, y1 in boxes:

        name = f"{page_label}_{uuid.uuid4().hex[:4]}"

        crop = img[y0:y1, x0:x1]
        crop_path = crops_dir / f"{name}.png"
        cv2.imwrite(str(crop_path), crop)

        # Draw bounding box
        cv2.rectangle(visual, (x0, y0), (x1, y1), (0, 255, 0), 3)

        (_, text_h), baseline = cv2.getTextSize(
            name, font, font_scale, thickness
        )
        label_height = text_h + baseline + 2 * padding

        # Draw above if possible
        # if y0 > label_height + 5:
        #     draw_label(visual, name, x0, y0 - label_height - 5)
        # else:
        #     # otherwise below
        #     draw_label(visual, name, x0, y1 + 5)
        (text_w, text_h), baseline = cv2.getTextSize(
            name, font, font_scale, thickness
        )

        box_w = text_w + 2 * padding
        box_h = text_h + baseline + 2 * padding

        margin = 5

        # Try left
        label_x = x0 - box_w - margin
        label_y = y0

        # If left goes outside, use right
        if label_x < 0:
            label_x = x1 + margin

        # If right also goes outside, clamp it
        if label_x + box_w > visual.shape[1]:
            label_x = visual.shape[1] - box_w - margin

        # Keep inside vertically
        label_y = min(label_y, visual.shape[0] - box_h - margin)

        draw_label(visual, name, label_x, label_y)

        saved.append(
            {
                "image_id": name,
                "bbox": [x0, y0, x1, y1],
                "crop_path": str(crop_path),
            }
        )

    cv2.imwrite(str(marked_dir / f"{page_label}_marked.png"), visual)

    doc.close()

    return saved


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