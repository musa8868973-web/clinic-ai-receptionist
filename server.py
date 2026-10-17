"""Web server: serves the chat frontend and a small JSON API.

Run:  python server.py        (then open http://localhost:8000)
  or: uvicorn server:app --reload

Endpoints
  POST /api/chat    {session_id, message} -> {reply}
  POST /api/reset   {session_id}          -> {ok}
  GET  /api/health                        -> status + calendar mode
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must run before importing src.*

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src import calendar_agent  # noqa: E402
from src.bot_engine import BotError, ClinicBot  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("server")

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
MAX_SESSIONS = 200  # oldest idle chats are dropped first

app = FastAPI(title="Clinic AI Receptionist")

# One ClinicBot (= one conversation memory) per browser session.
_sessions: OrderedDict[str, ClinicBot] = OrderedDict()


class ChatIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=1000)


class ResetIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def _get_bot(session_id: str) -> ClinicBot:
    bot = _sessions.get(session_id)
    if bot is None:
        bot = ClinicBot()
        _sessions[session_id] = bot
        while len(_sessions) > MAX_SESSIONS:
            _sessions.popitem(last=False)
    _sessions.move_to_end(session_id)
    return bot


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "calendar_mode": "demo" if calendar_agent.is_mock() else "google",
        "model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
    }


# Plain `def` endpoints run in a worker thread, so the blocking LLM call won't freeze the server.
@app.post("/api/chat")
def chat(payload: ChatIn) -> dict[str, str]:
    try:
        reply = _get_bot(payload.session_id).chat(payload.message.strip())
    except BotError as exc:
        logger.error("Bot error: %s", exc)
        raise HTTPException(status_code=502, detail="The assistant is unavailable right now. Please try again.") from exc
    return {"reply": reply}


@app.post("/api/reset")
def reset(payload: ResetIn) -> dict[str, bool]:
    bot = _sessions.get(payload.session_id)
    if bot:
        bot.reset()
    return {"ok": True}


# Must be mounted last so it doesn't shadow the /api routes.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
