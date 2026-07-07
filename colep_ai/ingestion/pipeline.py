# from excel_to_image import excel_to_pdf,pdf_to_images
# from pdf_to_images import extract_images_from_page
# import fitz
# def main(excel_path: str, output_dir: str = "output") -> list[str]:
#     pdf = excel_to_pdf(excel_path, f"{output_dir}/pdf")
#     pdf_to_images(pdf, f"{output_dir}/images")
#     # pdf=r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\colep_ai\ingestion\output\pdf\e2.pdf"
#     doc = fitz.open(pdf)
#     for i, page in enumerate(doc, start=1):
#         results = extract_images_from_page(
#         pdf_path=pdf,
#         page_number=4,  # 0-based
#         dpi=200,
#         output_dir="output/crops"
#     )
#         print(results)
#     return 


# if __name__ == "__main__":
#     p=r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\docs\e1.xlsx"
#     images = main(p)
#     print(images)

from excel_to_image import excel_to_pdf, pdf_to_images
from pdf_to_images import extract_images_from_page
from xlsx_group_resolver import XlsxGroupResolver   # ADD THIS LINE
import fitz

def main(excel_path: str, output_dir: str = "output") -> list[str]:
    pdf = r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\ops\output_7_7\pdf\e1.pdf"

    resolver = XlsxGroupResolver(excel_path)   # ADD THIS LINE — build once, outside the loop

    doc = fitz.open(pdf)
    all_results = []
    for i, page in enumerate(doc):              # CHANGED: start=0, i is now the 0-based page index
        results = extract_images_from_page(
            pdf_path=pdf,
            page_number=i,                        # CHANGED: was hardcoded 5
            dpi=200,
            output_dir="output/crops",
            group_resolver=resolver,              # ADD THIS LINE
        )
        all_results.append(results)
        print(results)
    return all_results


if __name__ == "__main__":
    p=r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\docs\e1.xlsx"
    images = main(p)
    print(images)
