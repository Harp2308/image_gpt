from qdrant_client import QdrantClient
from dotenv import load_dotenv
import os

load_dotenv()

client = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY"),
)

# COLLECTION_NAME = "colep_page_based_chunks"  # change as needed
COLLECTION_NAME = "colep_page_based_chunks_line"  # change as needed

def delete_collection(name: str):
    existing = [c.name for c in client.get_collections().collections]
    if name not in existing:
        print(f"Collection '{name}' does not exist.")
        return
    client.delete_collection(name)
    print(f"Deleted: {name}")

if __name__ == "__main__":
    confirm = input(f"Delete '{COLLECTION_NAME}'? (yes/no): ")
    if confirm.strip().lower() == "y":
        delete_collection(COLLECTION_NAME)
    else:
        print("Aborted.")