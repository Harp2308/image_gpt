import anthropic
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

import colep_ai.api.dependencies as deps
from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.database.qdrant_page_client import get_qdrant_client
from colep_ai.api.routes.chat import router as chat_router
from fastapi.responses import FileResponse
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
    deps.openai_client = OpenAI(api_key=settings.OPENAI_API_KEY.get_secret_value())
    deps.claude_client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY.get_secret_value())
    deps.qdrant_client = get_qdrant_client()
    logger.info("Clients initialized")


# Register routers
app.include_router(chat_router)
# app.include_router(ingestion_router)  # next router goes here



@app.get("/")
async def serve_ui():
    return FileResponse("colep_ai/frontend/colep_ui.html")