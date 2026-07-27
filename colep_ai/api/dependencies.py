import anthropic
from azure.search.documents import SearchClient
from openai import AsyncAzureOpenAI

import redis.asyncio as aioredis
from fastapi import Depends
 
from colep_ai.database.redis_client import get_redis_client
from colep_ai.database.cosmos_client import get_cosmos_container 
from colep_ai.database.mongo_client import get_cosmos_container
openai_client: AsyncAzureOpenAI | None = None
claude_client: anthropic.AsyncAnthropic | None = None
search_client: SearchClient | None = None


def get_openai() -> AsyncAzureOpenAI:
    assert openai_client is not None
    return openai_client


def get_claude() -> anthropic.AsyncAnthropic:
    assert claude_client is not None
    return claude_client


def get_search() -> SearchClient:
    assert search_client is not None
    return search_client


 
# ---------------------------------------------------------------------------
# Redis dependency
# ---------------------------------------------------------------------------
 
async def get_redis() -> aioredis.Redis:
    """
    Returns a shared async Redis client.
    redis-py manages the connection pool internally.
    No explicit close needed per-request.
    """
    return get_redis_client()
 
 
# ---------------------------------------------------------------------------
# Cosmos dependency
# ---------------------------------------------------------------------------
 
async def get_cosmos():
    """
    Returns an async Cosmos container client.
    Constructed per-request — lightweight, no persistent connection.
    """
    return get_cosmos_container()
 