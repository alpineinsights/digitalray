"""
FastAPI web server.

Exposes one endpoint:
    POST /ask - receives a message, sends it to digitalray.ai, returns the reply.

Also exposes:
    GET /health - a health check (returns "ok") for uptime monitoring.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

from app.config import settings
from app.scraper import ask_digitalray

# Set up logging so we can see what's happening in Railway logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# -- Request/response models (FastAPI uses these for validation + docs) --

class AskRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000,
                         description="The question to send to digitalray.ai")


class AskResponse(BaseModel):
    reply: str
    message_received: str


# -- App setup --

# Serialize requests with a lock: running multiple Playwright browsers in
# parallel in a small container will exhaust memory. Since the use case is
# a few requests per day, processing them one at a time is totally fine.
_request_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Runs on startup/shutdown. Nothing fancy needed here."""
    logger.info("digitalray-bridge starting up")
    yield
    logger.info("digitalray-bridge shutting down")


app = FastAPI(
    title="digitalray.ai Bridge",
    description="Forwards chatbot messages to digitalray.ai and returns the reply.",
    version="1.0.0",
    lifespan=lifespan,
)


# -- Endpoints --

@app.get("/health")
async def health():
    """Health check - used by Railway to know the service is alive."""
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
async def ask(
    request: AskRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """
    The main endpoint. Your other chatbot calls this with a message; we
    reply with whatever digitalray.ai answered.

    Request:
        POST /ask
        Headers: X-API-Key: <your-secret>   (only if API_SECRET_KEY is set)
        Body:    {"message": "your question here"}

    Response:
        200 OK  {"reply": "digitalray's answer", "message_received": "..."}
        401     if API_SECRET_KEY is set and the header doesn't match
        500     if something went wrong talking to digitalray.ai
    """
    # -- Optional API key check --
    if settings.api_secret_key:
        if x_api_key != settings.api_secret_key:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    # -- Serialize requests so we never run two browsers at once --
    async with _request_lock:
        try:
            reply = await ask_digitalray(request.message)
        except Exception as e:
            logger.exception("ask_digitalray failed")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to get reply from digitalray.ai: {type(e).__name__}",
            )

    return AskResponse(
        reply=reply,
        message_received=request.message,
    )
