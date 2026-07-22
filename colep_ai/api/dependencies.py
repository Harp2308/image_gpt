import anthropic
from azure.search.documents import SearchClient
from openai import AzureOpenAI

from colep_ai.indexing.embedder import get_openai_client
from colep_ai.generation.claude_client import get_claude_client
from colep_ai.database.azure_search_client import get_search_client

openai_client: AzureOpenAI | None = None
claude_client: anthropic.Anthropic | None = None
search_client: SearchClient | None = None


def get_openai() -> AzureOpenAI:
    assert openai_client is not None
    return openai_client


def get_claude() -> anthropic.Anthropic:
    assert claude_client is not None
    return claude_client


def get_search() -> SearchClient:
    assert search_client is not None
    return search_client