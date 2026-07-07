from excel_to_image import excel_to_pdf,pdf_to_images
from pdf_to_images import extract_images_from_page
import fitz
def main(excel_path: str, output_dir: str = "output") -> list[str]:
    pdf = excel_to_pdf(excel_path, f"{output_dir}/pdf")
    pdf_to_images(pdf, f"{output_dir}/images")
    # pdf=r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\colep_ai\ingestion\output\pdf\e2.pdf"
    doc = fitz.open(pdf)
    for i, page in enumerate(doc, start=1):
        results = extract_images_from_page(
        pdf_path=pdf,
        page_number=4,  # 0-based
        dpi=200,
        output_dir="output/crops"
    )
        print(results)
    return 


if __name__ == "__main__":
    p=r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\docs\e1.xlsx"
    images = main(p)
    print(images)