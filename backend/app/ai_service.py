"""
AI Service Abstraction Layer
=============================

Provides a unified interface for all AI backend operations.
Business logic (game_logic.py) should ONLY import this module,
never openai_client or echo_client directly.

All AI calls are automatically rate-limited through the global
request queue (RPM control). Callers don't need to worry about
rate limiting — it's transparent.

Switching or removing a backend (e.g. Echo) only requires changes
in this file — the rest of the codebase is unaffected.
"""

import logging
from typing import AsyncIterator

from .config import settings

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Backend detection (evaluated once at import time)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _is_echo_backend() -> bool:
    """Check if Echo backend is configured and selected."""
    return (
        settings.AI_BACKEND.lower() == "echo"
        and bool(settings.ECHO_API_URL)
        and bool(settings.ECHO_AGENT_ID)
    )


_use_echo = _is_echo_backend()

if _use_echo:
    logger.info("AI Service: Echo Agent API backend selected")
    from . import echo_client as _echo
else:
    logger.info("AI Service: OpenAI Compatible API backend selected")
    _echo = None  # type: ignore


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public AI Interface (backend-agnostic, rate-limited)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def get_ai_response(
    prompt: str,
    history: list[dict] | None = None,
    model: str | None = None,
    force_json: bool = True,
    user_id: str | None = None,
) -> str:
    """
    Get a complete (non-streaming) AI response.
    Automatically waits for a rate-limit slot before calling the API.
    """
    from .request_queue import queue
    from . import openai_client

    label = f"non-stream:{(model or settings.OPENAI_MODEL)[:20]}"
    async with queue.slot(user_id or "system", label=label):
        return await openai_client.get_ai_response(
            prompt=prompt, history=history,
            model=model or settings.OPENAI_MODEL,
            force_json=force_json, user_id=user_id,
        )


async def get_ai_response_stream(
    prompt: str,
    history: list[dict] | None = None,
    model: str | None = None,
    user_id: str | None = None,
) -> AsyncIterator[str | None]:
    """
    Get a streaming AI response. Yields text chunks; None = sentinel (done).
    
    Rate limiting: acquires a queue slot BEFORE the first chunk is yielded.
    The slot is held for the duration of the stream (since the API connection
    is active). The token-bucket refills concurrently, so this does not
    block other requests from starting while we're iterating chunks.
    """
    from .request_queue import queue
    from . import openai_client

    label = "stream"
    async with queue.slot(user_id or "system", label=label):
        async for chunk in openai_client.get_ai_response_stream(
            prompt=prompt, history=history,
            model=model or settings.OPENAI_MODEL,
            user_id=user_id,
        ):
            yield chunk


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session management (Echo-specific, no-op for OpenAI)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def restore_backend_session(player_id: str, session: dict):
    """
    Restore any backend-specific session state from persisted game session.
    Called when loading a session (e.g. on server restart, session load).

    For Echo: restores the echo_session_id into memory cache.
    For OpenAI: no-op.
    """
    if _echo is None:
        return
    saved_sid = session.get("echo_session_id")
    if saved_sid and not _echo.get_echo_session(player_id):
        _echo.restore_echo_session(player_id, saved_sid)


def reset_backend_session(player_id: str, session: dict):
    """
    Reset backend-specific session state when starting a new trial.
    Called when player starts a fresh trial.

    For Echo: clears the echo session so a new conversation starts.
    For OpenAI: no-op.
    """
    if _echo is None:
        return
    _echo.reset_player_session(player_id)
    session.pop("echo_session_id", None)


def persist_backend_session(player_id: str, session: dict):
    """
    Persist backend-specific session state into the game session dict.
    Called before saving session to disk (e.g. in finally blocks).

    For Echo: saves current echo_session_id into session dict.
    For OpenAI: no-op.
    """
    if _echo is None:
        return
    current_sid = _echo.get_echo_session(player_id)
    if current_sid:
        session["echo_session_id"] = current_sid


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Image generation (always via OpenAI-compatible client)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def is_image_gen_enabled() -> bool:
    from . import openai_client
    return openai_client.is_image_gen_enabled()


async def generate_image(scene_prompt: str, user_id: str | None = None) -> str | None:
    """Generate an image. Rate-limited through the queue."""
    from .request_queue import queue
    from . import openai_client

    async with queue.slot(user_id or "system", label="image-gen"):
        return await openai_client.generate_image(scene_prompt, user_id=user_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stream sentinel constant (used by game_logic for Echo full-replace)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ECHO_FULL_REPLACE_SENTINEL = "__ECHO_FULL_REPLACE__"
