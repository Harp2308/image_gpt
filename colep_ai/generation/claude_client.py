from anthropic import AsyncAnthropic
from colep_ai.core.logger import get_logger
from colep_ai.core.config import settings


# logger = get_logger(__name__)

def get_claude_client() -> AsyncAnthropic:
    client = AsyncAnthropic(
    api_key=settings.ANTHROPIC_API_KEY.get_secret_value(),
    base_url=settings.ANTHROPIC_ENDPOINT.get_secret_value().replace("/v1/messages", ""),)
    return client