

import subprocess
import logging
from pathlib import Path
import fitz
logger = logging.getLogger(__name__)

import shutil

def find_soffice() -> str:
    path = shutil.which("soffice")
    if path:
        return path
    fallback = r"C:\Program Files\LibreOffice\program\soffice.exe"
    if Path(fallback).exists():
        return fallback
    raise RuntimeError("LibreOffice not found. Install it or set path manually.")

def pdf_to_images(pdf_path: str, output_dir: str, dpi: int = 200) -> list[str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    image_paths = []
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=matrix)
        img_path = output_dir / f"{Path(pdf_path).stem}_page_{i}.png"
        pix.save(str(img_path))
        image_paths.append(str(img_path))

    doc.close()
    return image_paths
def excel_to_images(
    excel_path: str,
    pdf_dir: str,
    images_dir: str,
    dpi: int = 200,
    timeout: int = 120,
) -> tuple[str, list[str]]:

    excel_path = Path(excel_path)
    pdf_dir    = Path(pdf_dir)
    images_dir = Path(images_dir)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    pdf_path    = pdf_dir / f"{excel_path.stem}.pdf"
    soffice_path = find_soffice()

    result = subprocess.run(
        [soffice_path, "--headless", "--convert-to", "pdf", "--outdir", str(pdf_dir), str(excel_path)],
        capture_output=True, text=True, timeout=timeout,
    )

    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed.\n{result.stderr}")

    if not pdf_path.exists():
        raise RuntimeError(f"PDF not created: {pdf_path}")

    image_paths = pdf_to_images(str(pdf_path), str(images_dir), dpi)

    logger.info("%s -> %d page images", excel_path.name, len(image_paths))
    return str(pdf_path), image_paths


import win32com.client
from pathlib import Path
def excel_to_pdf(excel_path: str, pdf_dir: str) -> str:
    excel_path = str(Path(excel_path).resolve())
    pdf_dir = str(Path(pdf_dir).resolve())
    Path(pdf_dir).mkdir(parents=True, exist_ok=True)
    pdf_path = str(Path(pdf_dir) / f"{Path(excel_path).stem}.pdf")

    excel = win32com.client.Dispatch("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False
    wb = excel.Workbooks.Open(excel_path)
    wb.ExportAsFixedFormat(0, pdf_path)
    wb.Close(False)
    excel.Quit()
    return pdf_path

# def excel_to_pdf(excel_path: str, pdf_dir: str) -> str:
#     excel_path = Path(excel_path).resolve()
#     pdf_path = str(Path(pdf_dir).resolve() / f"{excel_path.stem}.pdf").replace("/", "\\")
    
#     excel = win32com.client.Dispatch("Excel.Application")
#     excel.Visible = False
#     wb = excel.Workbooks.Open(str(excel_path))
#     wb.ExportAsFixedFormat(0, str(pdf_path))
#     wb.Close(False)
#     excel.Quit()
    
#     return str(pdf_path)
