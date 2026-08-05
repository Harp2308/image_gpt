"""
api/ingestion_indexing_main.py
"""

from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import colep_ai.api.dependencies as deps
from colep_ai.core.logger import get_logger
from colep_ai.database.azure_search_client import get_search_client,get_index_client,ensure_page_index
# from colep_ai.database.cosmos_client import ensure_cosmos_resources
from colep_ai.generation.claude_client import get_claude_client_sync
from colep_ai.indexing.embedder import get_openai_client_sync
from colep_ai.api.routes.ingest import router as ingest_router

logger = get_logger(__name__)

app = FastAPI(title="Colep AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

Path("outputs").mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")
app.mount("/static", StaticFiles(directory="colep_ai/frontend"), name="static")

@app.on_event("startup")
async def _init_clients():
    deps.openai_client = get_openai_client_sync()  
    deps.claude_client = get_claude_client_sync() 
    ensure_page_index(get_index_client())   # ensure index exists before search client is used
    deps.search_client = get_search_client()
    logger.info("Clients initialized")


app.include_router(ingest_router)
