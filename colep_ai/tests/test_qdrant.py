

# COLLECTION_NAME = "colep_steps_test"
EMBED_DIM = 3072
QDRANT_URL="https://6ec1c2a5-4b08-472c-9229-91a89d3abd5e.us-west-1-0.aws.cloud.qdrant.io"
# QDRANT_URL="https://1ecf121c-0910-4a8f-a398-9e0f5c04a378.eu-west-2-0.aws.cloud.qdrant.io"  aa

QDRANT_API_KEY="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIiwic3ViamVjdCI6ImFwaS1rZXk6Njg2MzZkNWUtMjdkOS00OWYwLWE3MzctNzc0ZTgxY2RiMWFlIn0.tVS-uQ8FOlcbRFNU_DO7y20uY9xT5SXjWYKYcBfsv0E"
print(QDRANT_URL)
print(QDRANT_API_KEY)
from qdrant_client import QdrantClient
from qdrant_client import models 

COLLECTION_NAME = "colep_steps"
EMBED_DIM = 3072

client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY,timeout=30)

# create
if not client.collection_exists(COLLECTION_NAME):
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE),
    )
    print(f"created: {COLLECTION_NAME}")
else:
    print(f"already exists: {COLLECTION_NAME}")

# verify
info = client.get_collection(COLLECTION_NAME)
print("status:", info.status)
print("vectors config:", info.config.params.vectors)
print("points count:", info.points_count)

# list all collections
print("all collections:", [c.name for c in client.get_collections().collections])