import anthropic
from openai import AzureOpenAI
from qdrant_client import QdrantClient
from colep_ai.indexing.embedder import get_openai_client
from colep_ai.generation.claude_client import get_claude_client
from colep_ai.database.qdrant_page_client import get_qdrant_client

openai_client: AzureOpenAI | None = None
claude_client: anthropic.Anthropic | None = None
qdrant_client: QdrantClient | None = None


def get_openai() -> AzureOpenAI:
    assert openai_client is not None
    return get_openai_client()


def get_claude() -> anthropic.Anthropic:
    assert claude_client is not None
    return get_claude_client()


def get_qdrant() -> QdrantClient:
    assert qdrant_client is not None
    return get_qdrant_client()