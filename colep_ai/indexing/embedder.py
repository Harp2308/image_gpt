from openai import AsyncAzureOpenAI
from colep_ai.core.logger import get_logger
from colep_ai.core.config import settings


logger = get_logger(__name__)

def get_openai_client() -> AsyncAzureOpenAI:
    client = AsyncAzureOpenAI(
    azure_endpoint=settings.OPENAI_ENDPOINT.get_secret_value(),
    api_key=settings.OPENAI_API_KEY.get_secret_value(),
    api_version="2025-04-01-preview",
     timeout=15.0,
    max_retries=5,
)
    return client
    # return OpenAI(api_key=settings.OPENAI_API_KEY.get_secret_value())  # reads OPENAI_API_KEY from env

# can handle empty strings
def embed_texts(client: AsyncAzureOpenAI, texts: list[str]) -> list[list[float]]:
    vectors = []
    for i in range(0, len(texts), settings.BATCH_SIZE):
        batch = []
        for idx, t in enumerate(texts[i:i +  settings.BATCH_SIZE]):
            if not t or not t.strip():
                logger.warning(f"Empty text at index {i + idx} — replacing with placeholder")
                batch.append(" ")
            else:
                batch.append(t.strip())
        resp = client.embeddings.create(model= settings.EMBED_MODEL, input=batch)
        vectors.extend([d.embedding for d in resp.data])
    return vectors

# def embed_texts(client: AsyncAzureOpenAI, texts: list[str]) -> list[list[float]]:
#     vectors = []
#     for i in range(0, len(texts), BATCH_SIZE):
#         batch = texts[i:i + BATCH_SIZE]
#         resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
#         vectors.extend([d.embedding for d in resp.data])
#     return vectors