import anthropic
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from openai import AzureOpenAI

import colep_ai.api.dependencies as deps
from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.database.azure_search_client import get_search_client
from colep_ai.generation.claude_client import get_claude_client
from colep_ai.indexing.embedder import get_openai_client
from colep_ai.api.routes.chat import router as chat_router

logger = get_logger(__name__)

app = FastAPI(title="Colep AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")
app.mount("/static", StaticFiles(directory="colep_ai/frontend"), name="static")

@app.on_event("startup")
def _init_clients():
    deps.openai_client = get_openai_client()
    deps.claude_client = get_claude_client()
    deps.search_client = get_search_client()
    logger.info("Clients initialized: AzureOpenAI, Anthropic, AzureSearch")


app.include_router(chat_router)


@app.get("/")
async def serve_ui():
    return FileResponse("colep_ai/frontend/colep_ui.html")