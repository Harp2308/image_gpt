"""
XlsxGroupResolver

Parses xl/drawings/drawingN.xml inside an .xlsx to recover xdr:grpSp group
membership, then matches embedded media to images extracted from a rendered
PDF page using rotation-invariant perceptual hashing (dhash).

Builds ONE hash registry per workbook (media_hash -> group_id); matching
happens per PDF page via nearest-hash lookup.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import cv2
import numpy as np
from PIL import Image

NS = {
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def dhash(image: Image.Image, hash_size: int = 8) -> int:
    image = image.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = np.asarray(image, dtype=np.int16)
    diff = pixels[:, 1:] > pixels[:, :-1]
    bits = diff.flatten()
    h = 0
    for bit in bits:
        h = (h << 1) | int(bit)
    return h


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class XlsxGroupResolver:
    def __init__(self, xlsx_path: str, hamming_thresh: int = 5, log_low_confidence: bool = True):
        self.xlsx_path = Path(xlsx_path)
        self.hamming_thresh = hamming_thresh
        self.log_low_confidence = log_low_confidence
        self._media_hashes: list[dict] = []
        self._build()

    def _build(self) -> None:
        with zipfile.ZipFile(self.xlsx_path) as z:
            names = set(z.namelist())
            drawing_files = sorted(n for n in names if re.match(r"xl/drawings/drawing\d+\.xml$", n))
            for drawing_file in drawing_files:
                rels_file = f"xl/drawings/_rels/{Path(drawing_file).name}.rels"
                rid_to_media = self._parse_rels(z, rels_file, names)
                root = ET.fromstring(z.read(drawing_file))
                group_counter = [0]
                self._walk(root, z, names, rid_to_media, drawing_file, parent_group=None, group_counter=group_counter)

    def _parse_rels(self, z: zipfile.ZipFile, rels_file: str, names: set[str]) -> dict[str, str]:
        rid_to_media: dict[str, str] = {}
        if rels_file not in names:
            return rid_to_media
        rels_root = ET.fromstring(z.read(rels_file))
        for rel in rels_root:
            rid = rel.get("Id")
            target = rel.get("Target")
            if rid is None or target is None:
                continue
            rid_to_media[rid] = self._normalize_media_path(target)
        return rid_to_media

    @staticmethod
    def _normalize_media_path(target: str) -> str:
        parts = ("xl/drawings/" + target).split("/")
        stack: list[str] = []
        for p in parts:
            if p == "..":
                if stack:
                    stack.pop()
            elif p == ".":
                continue
            else:
                stack.append(p)
        return "/".join(stack)

    def _walk(self, node, z, names, rid_to_media, drawing_file, parent_group, group_counter):
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag == "grpSp":
                group_counter[0] += 1
                gid = f"{drawing_file}::group_{group_counter[0]}"
                self._walk(child, z, names, rid_to_media, drawing_file, parent_group=gid, group_counter=group_counter)
            elif tag == "pic":
                self._register_pic(child, z, names, rid_to_media, parent_group)
            else:
                self._walk(child, z, names, rid_to_media, drawing_file, parent_group, group_counter)

    def _register_pic(self, pic_node, z, names, rid_to_media, parent_group):
        blip = pic_node.find(".//a:blip", NS)
        if blip is None:
            return
        rid = blip.get(f"{{{NS['r']}}}embed")
        if rid is None or rid not in rid_to_media:
            return
        media_path = rid_to_media[rid]
        if media_path not in names:
            return
        try:
            img_bytes = z.read(media_path)
            img = Image.open(io.BytesIO(img_bytes))
            img.load()
        except Exception:
            return

        hashes = {}
        for angle in (0, 90, 180, 270):
            rotated = img.rotate(-angle, expand=True) if angle else img
            hashes[angle] = dhash(rotated)

        self._media_hashes.append({
            "rid": rid,
            "media_path": media_path,
            "group_id": parent_group,
            "hashes": hashes,
        })

    def resolve_groups_for_page(self, page_boxes: list[dict], page_image_bgr: np.ndarray) -> dict[int, str | None]:
        result: dict[int, str | None] = {}
        for b in page_boxes:
            x0, y0, x1, y1 = b["bbox"]
            crop = page_image_bgr[y0:y1, x0:x1]
            if crop.size == 0:
                result[b["xref"]] = None
                continue

            crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            crop_pil = Image.fromarray(crop_rgb)
            crop_hash = dhash(crop_pil)

            best_entry = None
            best_dist = self.hamming_thresh + 1
            for entry in self._media_hashes:
                for _angle, h in entry["hashes"].items():
                    d = hamming(crop_hash, h)
                    if d < best_dist:
                        best_dist = d
                        best_entry = entry

            if best_entry is None:
                result[b["xref"]] = None
                continue

            if self.log_low_confidence and best_dist > 2:
                print(f"[XlsxGroupResolver] low-confidence match xref={b['xref']} -> {best_entry['media_path']} (hamming={best_dist})")

            result[b["xref"]] = best_entry["group_id"]

        return result
