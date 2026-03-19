"""
Echo Agent API Client
=====================

Wraps the Echo Agent Service SSE streaming API (POST /api/chat_streaming)
to be used as an alternative AI backend alongside OpenAI.

Key differences from OpenAI:
- NOT OpenAI-compatible; uses custom SSE protocol
- Streaming via Server-Sent Events (event: initialize / streaming / completed)
- Session management built-in on Echo side
- Authorization via custom header, not Bearer token format
"""

import logging
import json
import asyncio
import random
import uuid
from typing import AsyncIterator

import httpx

from .config import settings

logger = logging.getLogger(__name__)

# --- Per-user session tracking ---
# Map player_id -> echo session_id so conversations persist within a game session
# This is also backed by the game session's persistent storage (survives server restarts)
_echo_sessions: dict[str, str] = {}

# --- User concurrency ---
MAX_CONCURRENT = 2
_user_semaphores: dict[str, asyncio.Semaphore] = {}
_sem_lock = asyncio.Lock()


async def _get_semaphore(user_id: str) -> asyncio.Semaphore:
    async with _sem_lock:
        if user_id not in _user_semaphores:
            _user_semaphores[user_id] = asyncio.Semaphore(MAX_CONCURRENT)
        return _user_semaphores[user_id]


def is_echo_enabled() -> bool:
    """Check whether Echo backend is configured and selected."""
    return (
        settings.AI_BACKEND.lower() == "echo"
        and bool(settings.ECHO_API_URL)
        and bool(settings.ECHO_AGENT_ID)
    )


def get_echo_session(player_id: str) -> str | None:
    """Get the current Echo session ID for a player (memory cache)."""
    return _echo_sessions.get(player_id)


def set_echo_session(player_id: str, session_id: str | None):
    """Set or clear the Echo session ID for a player (memory cache)."""
    if session_id:
        _echo_sessions[player_id] = session_id
        logger.info(f"Echo session STORED for {player_id}: {session_id}")
    elif player_id in _echo_sessions:
        old = _echo_sessions.pop(player_id)
        logger.info(f"Echo session CLEARED for {player_id} (was {old})")


def restore_echo_session(player_id: str, session_id: str | None):
    """
    Restore a previously persisted echo session_id into memory cache.
    Called during game session load to survive server restarts.
    """
    if session_id:
        _echo_sessions[player_id] = session_id
        logger.info(f"Echo session RESTORED from persistent storage for {player_id}: {session_id}")


def _build_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.ECHO_API_KEY:
        headers["Authorization"] = settings.ECHO_API_KEY
    if settings.ECHO_MONGO_ID:
        headers["x-mongo-id"] = settings.ECHO_MONGO_ID
    return headers


async def ensure_session(player_id: str) -> tuple[str, bool]:
    """
    Ensure an Echo session exists for the player via /api/sessions/get_or_create.

    - If a session_id is already cached, verify it still exists on Echo side.
      If verification fails (network issue), still return the cached id
      since chat_streaming will also accept it.
    - If no cached session, create a new one.
    Returns (session_id, is_newly_created).
    """
    cached_sid = get_echo_session(player_id)

    url = f"{settings.ECHO_API_URL.rstrip('/')}/api/sessions/get_or_create"
    headers = _build_headers()

    if cached_sid:
        # Verify the cached session still exists
        body = {"session_id": cached_sid, "agent_id": settings.ECHO_AGENT_ID}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                resp = await client.post(url, json=body, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    verified_sid = data.get("session_id", cached_sid)
                    logger.info(f"Echo session verified for {player_id}: {verified_sid}")
                    set_echo_session(player_id, verified_sid)
                    return verified_sid, False  # Not new — session existed
                else:
                    logger.warning(
                        f"Echo session verify failed ({resp.status_code}), "
                        f"using cached session_id anyway: {cached_sid}"
                    )
                    # Still return cached_sid — chat_streaming will accept it,
                    # and creating a NEW session would lose all conversation history.
                    return cached_sid, False
        except Exception as e:
            logger.warning(
                f"Echo session verify error for {player_id}: {e}. "
                f"Using cached session_id: {cached_sid}"
            )
            # Network issue — trust the cached session_id rather than creating a new one
            return cached_sid, False

    # No cached session — create a new one
    new_sid = f"game_{player_id}_{uuid.uuid4().hex[:8]}"
    body = {"session_id": new_sid, "agent_id": settings.ECHO_AGENT_ID}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                created_sid = data.get("session_id", new_sid)
                set_echo_session(player_id, created_sid)
                logger.info(f"Echo session CREATED for {player_id}: {created_sid}")
                return created_sid, True  # Newly created
            else:
                error_text = resp.text[:300]
                logger.error(f"Echo session create failed ({resp.status_code}): {error_text}")
    except Exception as e:
        logger.error(f"Echo session create error for {player_id}: {e}")

    # Fallback: let chat_streaming create implicitly
    set_echo_session(player_id, new_sid)
    return new_sid, True


def _build_request_body(
    prompt: str,
    history: list[dict] | None,
    agent_id: str | None = None,
    session_id: str | None = None,
    player_id: str | None = None,
    is_new_session: bool = False,
) -> dict:
    """
    Build the request body for Echo /api/chat_streaming.

    When is_new_session=True (first message of a trial), we inject
    history context into user_message since Echo has no prior conversation.
    For subsequent messages, Echo already has the session history, so we
    only send the current prompt to avoid duplicate context.
    """
    if is_new_session and history:
        # First message: inject history context since Echo session is empty
        combined_parts = []
        recent_history = history[-6:]
        for msg in recent_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                continue
            elif role == "assistant":
                combined_parts.append(f"[AI之前的回复]: {content[:500]}")
            elif role == "user":
                combined_parts.append(f"[玩家之前的输入]: {content[:200]}")
        combined_parts.append(f"[当前指令]:\n{prompt}")
        user_message = "\n\n".join(combined_parts)
    else:
        # Subsequent messages: Echo has full session history, just send prompt
        user_message = prompt

    # Extract system prompt from history
    system_prompt = None
    if history:
        for msg in history:
            if msg.get("role") == "system":
                system_prompt = msg["content"]
                break

    body = {
        "agent_id": agent_id or settings.ECHO_AGENT_ID,
        "user_message": user_message,
        "session_id": session_id,
        "model_name": None,
        "actual_username": player_id,
        "enable_skills": False,
        "disabled_tools": [],
        "enabled_tools": None,
    }

    if system_prompt:
        body["system_prompt"] = system_prompt

    return body


async def _parse_sse_stream(response: httpx.Response) -> AsyncIterator[tuple[str, dict]]:
    """
    Parse SSE (Server-Sent Events) from an httpx streaming response.
    Yields (event_type, data_dict) tuples.

    Handles:
    - Multi-line data: fields (joined with '\n' per spec)
    - Final event without trailing blank line (flush on stream end)
    - Raw text data: fields that are not valid JSON (yielded as {"raw": text})
    """
    event_type = None
    data_lines: list[str] = []

    def _flush():
        """Try to yield the buffered event. Returns (event_type, data_dict) or None."""
        nonlocal event_type, data_lines
        if not event_type or not data_lines:
            event_type = None
            data_lines = []
            return None
        joined = "\n".join(data_lines)
        evt = event_type
        event_type = None
        data_lines = []
        try:
            data = json.loads(joined)
            return evt, data
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse SSE data for event '{evt}': {joined[:300]}")
            # Return raw text so callers can still use it
            return evt, {"raw": joined}

    async for line in response.aiter_lines():
        if not line:
            # Empty line = end of current event
            result = _flush()
            if result:
                yield result
            continue

        if line.startswith("event: "):
            # If we already have a buffered event that wasn't flushed
            # (server sent two events without a blank line between them),
            # flush the previous one first.
            if event_type and data_lines:
                result = _flush()
                if result:
                    yield result
            event_type = line[7:].strip()
        elif line.startswith("data: "):
            data_lines.append(line[6:])
        elif line.startswith("data:"):
            # Handle "data:" with no space (some servers do this)
            data_lines.append(line[5:])

    # Stream ended — flush any remaining buffered event
    result = _flush()
    if result:
        yield result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public API — drop-in replacements for openai_client functions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def get_ai_response(
    prompt: str,
    history: list[dict] | None = None,
    model: str | None = None,
    force_json: bool = True,
    user_id: str | None = None,
) -> str:
    """
    Non-streaming: send prompt to Echo and collect the full response.
    Returns the complete AI response text.
    """
    if user_id:
        sem = await _get_semaphore(user_id)
        async with sem:
            return await _get_ai_response_impl(prompt, history, user_id)
    return await _get_ai_response_impl(prompt, history, user_id)


async def _get_ai_response_impl(
    prompt: str,
    history: list[dict] | None,
    player_id: str | None,
) -> str:
    url = f"{settings.ECHO_API_URL.rstrip('/')}/api/chat_streaming"
    headers = _build_headers()

    # Ensure we have a valid session (create if needed)
    if player_id:
        session_id, is_new = await ensure_session(player_id)
    else:
        session_id = None
        is_new = True

    expected_sid = session_id  # The session_id we INTEND to use
    logger.info(f"Echo non-stream request for {player_id}, session_id={session_id}, is_new={is_new}")
    body = _build_request_body(
        prompt, history,
        session_id=session_id,
        player_id=player_id,
        is_new_session=is_new,
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        error_text = ""
                        async for chunk in resp.aiter_text():
                            error_text += chunk
                        logger.error(f"Echo API error {resp.status_code}: {error_text[:500]}")
                        raise Exception(f"Echo API error: {resp.status_code}")

                    full_response = ""
                    # Echo streaming delta.content is CUMULATIVE (full text
                    # up to current point), NOT incremental.  We must track
                    # the last seen content to compute deltas ourselves.
                    _last_cumulative = ""

                    async for event_type, data in _parse_sse_stream(resp):
                        if event_type == "initialize":
                            new_sid = data.get("session_id")
                            if new_sid and player_id:
                                if expected_sid and new_sid != expected_sid:
                                    # Echo returned a DIFFERENT session than we asked for.
                                    # This means our session was invalid/expired.
                                    # Accept the new one but log a warning.
                                    logger.warning(
                                        f"Echo session CHANGED for {player_id}: "
                                        f"expected={expected_sid}, got={new_sid}"
                                    )
                                set_echo_session(player_id, new_sid)

                        elif event_type == "streaming":
                            messages = data.get("messages", [])
                            for msg in messages:
                                delta = msg.get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    if len(content) > len(_last_cumulative):
                                        _last_cumulative = content
                                    else:
                                        _last_cumulative = content
                            full_response = _last_cumulative

                        elif event_type == "completed":
                            final = data.get("final_answer", "")
                            if final:
                                logger.info(
                                    f"Echo non-stream: got final_answer ({len(final)} chars), "
                                    f"replacing streamed ({len(full_response)} chars)"
                                )
                                full_response = final
                            raw = data.get("raw", "")
                            if raw and not final and not full_response:
                                full_response = raw

                    if full_response:
                        return full_response
                    return "错误：Echo API返回空响应"

        except Exception as e:
            logger.error(f"Echo API request failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return f"错误：Echo AI服务出现问题。详情: {e}"
            await asyncio.sleep(1 * (2 ** attempt) + random.uniform(0, 1))

    return "错误：Echo AI服务连接失败"


async def get_ai_response_stream(
    prompt: str,
    history: list[dict] | None = None,
    model: str | None = None,
    user_id: str | None = None,
) -> AsyncIterator[str | None]:
    """
    Streaming: yield text chunks as they arrive from Echo SSE.
    Yields None as sentinel when done.
    """
    if user_id:
        sem = await _get_semaphore(user_id)
        async with sem:
            async for chunk in _get_ai_response_stream_impl(prompt, history, user_id):
                yield chunk
    else:
        async for chunk in _get_ai_response_stream_impl(prompt, history, user_id):
            yield chunk


async def _get_ai_response_stream_impl(
    prompt: str,
    history: list[dict] | None,
    player_id: str | None,
) -> AsyncIterator[str | None]:
    url = f"{settings.ECHO_API_URL.rstrip('/')}/api/chat_streaming"
    headers = _build_headers()

    # Ensure we have a valid session (create if needed)
    if player_id:
        session_id, is_new = await ensure_session(player_id)
    else:
        session_id = None
        is_new = True

    expected_sid = session_id
    logger.info(f"Echo stream request for {player_id}, session_id={session_id}, is_new={is_new}")
    body = _build_request_body(
        prompt, history,
        session_id=session_id,
        player_id=player_id,
        is_new_session=is_new,
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Echo streaming delta.content is CUMULATIVE (full text from the
            # beginning each time), NOT incremental.  We track the last
            # cumulative text and only yield the NEW delta to callers.
            _last_cumulative = ""
            streamed_content = ""  # What we've actually yielded (= incremental total)

            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        error_text = ""
                        async for chunk in resp.aiter_text():
                            error_text += chunk
                        logger.error(f"Echo stream error {resp.status_code}: {error_text[:500]}")
                        yield f"错误：Echo AI服务返回 {resp.status_code}"
                        return

                    async for event_type, data in _parse_sse_stream(resp):
                        if event_type == "initialize":
                            new_sid = data.get("session_id")
                            if new_sid and player_id:
                                if expected_sid and new_sid != expected_sid:
                                    logger.warning(
                                        f"Echo stream session CHANGED for {player_id}: "
                                        f"expected={expected_sid}, got={new_sid}"
                                    )
                                set_echo_session(player_id, new_sid)

                        elif event_type == "streaming":
                            messages = data.get("messages", [])
                            for msg in messages:
                                delta = msg.get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    # content is cumulative: "混", "混沌", "混沌初", ...
                                    # Yield only the new characters since last time.
                                    if content.startswith(_last_cumulative):
                                        new_chars = content[len(_last_cumulative):]
                                    elif len(content) > len(_last_cumulative):
                                        new_chars = content
                                    else:
                                        new_chars = ""
                                    _last_cumulative = content
                                    if new_chars:
                                        streamed_content += new_chars
                                        yield new_chars
                            raw = data.get("raw", "")
                            if raw and not messages:
                                streamed_content += raw
                                yield raw

                        elif event_type == "completed":
                            final = data.get("final_answer", "")
                            if final and len(final) > len(streamed_content) + 10:
                                logger.info(
                                    f"Echo completed: final_answer ({len(final)} chars) "
                                    f"replaces streamed content ({len(streamed_content)} chars)"
                                )
                                yield f"__ECHO_FULL_REPLACE__{final}"
                            elif final and not streamed_content:
                                logger.info(
                                    f"Echo completed: no streamed content, using final_answer ({len(final)} chars)"
                                )
                                yield final

                            raw = data.get("raw", "")
                            if raw and not final and not streamed_content:
                                logger.info(f"Echo completed: using raw text ({len(raw)} chars)")
                                yield raw

                    yield None  # sentinel
                    return

        except Exception as e:
            logger.error(f"Echo stream failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                yield f"错误：Echo AI服务连接失败: {e}"
                return
            await asyncio.sleep(1 * (2 ** attempt) + random.uniform(0, 1))

    yield None


def reset_player_session(player_id: str):
    """
    Called when a player starts a new trial — clear their Echo session
    so a fresh conversation begins.
    """
    set_echo_session(player_id, None)
    logger.info(f"Reset Echo session for player {player_id}")
