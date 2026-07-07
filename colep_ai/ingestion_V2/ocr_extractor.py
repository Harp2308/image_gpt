import json
import logging
from pathlib import Path
from google.cloud import vision
from google.oauth2 import service_account

logger = logging.getLogger(__name__)


def get_vision_client(credentials_path: str) -> vision.ImageAnnotatorClient:
    """Create once, reuse across all image calls. Do not call per-image."""
    creds = service_account.Credentials.from_service_account_file(credentials_path)
    return vision.ImageAnnotatorClient(credentials=creds)


def image_to_vision_json(image_path: str, output_dir: str, client: vision.ImageAnnotatorClient) -> str:
    """
    Image -> Google Vision document_text_detection -> JSON saved to disk.
    Returns saved JSON path.
    """
    image_path = Path(image_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(image_path, "rb") as f:
        content = f.read()

    response = client.document_text_detection(image=vision.Image(content=content))
    if response.error.message:
        raise RuntimeError(f"Google Vision error on {image_path.name}: {response.error.message}")

    result = {
        "source_image": image_path.name,
        "full_text": response.full_text_annotation.text,
        "blocks": []
    }

    for page in response.full_text_annotation.pages:
        for block in page.blocks:
            words = []
            for para in block.paragraphs:
                for word in para.words:
                    words.append("".join(s.text for s in word.symbols))
            result["blocks"].append({
                "text": " ".join(words),
                "bbox": [(v.x, v.y) for v in block.bounding_box.vertices],
                "confidence": block.confidence
            })

    json_path = output_dir / f"{image_path.stem}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"{image_path.name} -> {json_path.name} ({len(result['blocks'])} blocks)")
    return str(json_path)
