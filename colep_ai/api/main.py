"""
api/main.py
"""
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import colep_ai.api.dependencies as deps
from colep_ai.core.logger import get_logger
from colep_ai.database.azure_search_client import get_search_client,get_index_client,ensure_page_index

# from colep_ai.database.mongo_client import ensure_cosmos_resources
from colep_ai.database.cosmos_client import ensure_cosmos_resources
from colep_ai.generation.claude_client import get_claude_client
from colep_ai.indexing.embedder import get_openai_client
from colep_ai.core.config import  settings
from colep_ai.database.query_log_client import ensure_query_logs_container, close_query_logs_client

from colep_ai.api.routes.chat_session import router as chat_router
from colep_ai.api.routes.history import router as history_router
from colep_ai.api.routes.sessions import router as sessions_router
from colep_ai.api.routes.debugger import router as admin_router
from colep_ai.api.routes.blob_viewer import router as blob_router
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
    deps.openai_client = get_openai_client()  # ← just use the same factory as indexing
    deps.claude_client = get_claude_client() 
    ensure_page_index(get_index_client())   # ensure index exists before search client is used
    deps.search_client = get_search_client()
    await ensure_cosmos_resources()
    await ensure_query_logs_container()    
    await close_query_logs_client()      
    logger.info("Clients initialized")


# Register routers
app.include_router(chat_router)
app.include_router(history_router)
app.include_router(sessions_router)
app.include_router(admin_router)
app.include_router(blob_router)
@app.get("/")
async def serve_ui():
    return FileResponse("colep_ai/frontend/chat_history.html")

@app.get("/shinchan")
async def serve_dashboard():
    return FileResponse("colep_ai/frontend/dashboard.html")
 
 