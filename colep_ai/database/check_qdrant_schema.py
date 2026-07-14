"""
Read-only schema check. Doesn't modify anything.

I need to see your actual Qdrant payload structure before writing any line-
number filtering logic — guessing field names and iterating against your
production system is exactly what I should stop doing.

Adjust the import for get_qdrant_client() / your client factory and the
collection name constant to match your actual codebase if they differ —
I'm inferring these names from what's shown in your other files
(page_retrieval.py imports PAGE_COLLECTION_NAME from
colep_ai.database.qdrant_page_client) but I have not run this against your
instance and cannot confirm the client construction matches your settings
schema exactly.

Run:
    python check_qdrant_schema.py

Paste the full output back — specifically I need:
  1. The complete list of top-level payload keys.
  2. Whether any key holds a line number as structured data (e.g. "line": "11"),
     as opposed to it only being embedded in source_file / document_code strings.
  3. Whether that field, if it exists, is set up as an indexed/filterable
     field in the collection schema (matters for whether a Qdrant-side
     filter is efficient vs needs a payload index added first).
"""

from colep_ai.database.qdrant_page_client import get_qdrant_client, PAGE_COLLECTION_NAME

if __name__ == "__main__":
    client = get_qdrant_client()

    # 1. Full payload keys + a few sample values, from a handful of points.
    points, _ = client.scroll(
        collection_name=PAGE_COLLECTION_NAME,
        limit=5,
        with_payload=True,
        with_vectors=False,
    )

    print(f"Sampled {len(points)} points from '{PAGE_COLLECTION_NAME}'\n")
    for p in points:
        print("---")
        print("payload keys:", sorted(p.payload.keys()))
        print("source_file:", p.payload.get("source_file"))
        print("document_code:", p.payload.get("document_code"))
        print("page_number:", p.payload.get("page_number"))
        for k, v in p.payload.items():
            if "line" in k.lower() or "linha" in k.lower():
                print(f"  possible line field -> {k!r}: {v!r}")

    # 2. Collection-level info: tells us what's actually indexed for filtering.
    print("\n--- collection info (payload schema / indexed fields) ---")
    info = client.get_collection(PAGE_COLLECTION_NAME)
    print(info.payload_schema if hasattr(info, "payload_schema") else info)
