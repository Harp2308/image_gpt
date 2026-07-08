from openai import OpenAI
from colep_ai.core.logger import get_logger
from colep_ai.core.config import settings
# from core.logger import get_logger
# from core.config import settings
EMBED_MODEL = "text-embedding-3-large"
EMBED_DIM = 3072
BATCH_SIZE = 100

logger = get_logger(__name__)

def get_openai_client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY.get_secret_value())  # reads OPENAI_API_KEY from env

def embed_texts(client: OpenAI, texts: list[str]) -> list[list[float]]:
    vectors = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
        vectors.extend([d.embedding for d in resp.data])
    return vectors