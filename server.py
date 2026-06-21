"""FastAPI entry point for agy2api."""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pathlib import Path

from agy_runner import (
    cleanup_conversation,
    run_agy_async,
    sweep_all_conversations,
    sweep_orphan_sidecars,
)
from config import is_loopback_host, resolve_model, settings
from fake_stream import make_heartbeat, sse_event, stream_chunks
from models import ChatCompletionRequest, ModelInfo, ModelList
from session_store import SessionStore, fingerprint


logger = logging.getLogger("agy2api")


def _setup_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("[agy2api %(asctime)s %(levelname)s] %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_setup_logging()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Startup housekeeping.
    try:
        if settings.stateful:
            # Hard reset: the in-memory session store starts empty, so any .db
            # files kept alive by a previous run are unreachable orphans. Wipe
            # them now (stateful memory does not survive a restart anyway).
            removed = sweep_all_conversations()
            logger.info("startup (stateful): wiped %d conversation db(s)", removed)
        else:
            removed = sweep_orphan_sidecars()
            if removed:
                logger.info("startup: swept %d orphan SQLite sidecar file(s)", removed)
    except Exception as exc:  # never block startup on housekeeping
        logger.warning("startup sweep failed: %s", exc)

    yield

    # Shutdown housekeeping. The atexit hook covers plain `python server.py`
    # exit too; this fires on uvicorn's graceful stop. Belt-and-suspenders so a
    # clean stop never leaves orphaned stateful sessions behind.
    if settings.stateful:
        try:
            removed = sweep_all_conversations()
            if removed:
                logger.info("shutdown (stateful): wiped %d conversation db(s)", removed)
        except Exception as exc:  # never block shutdown on housekeeping
            logger.warning("shutdown sweep failed: %s", exc)


app = FastAPI(title="agy2api", version="1.1.0", lifespan=_lifespan)
security = HTTPBearer(auto_error=False)

# Limit concurrent agy runs (default 1) to mimic human-paced usage and avoid the
# concurrent conversation-DB race. See config.max_concurrency.
_run_semaphore = asyncio.Semaphore(max(1, settings.max_concurrency))

# Experimental stateful-session layer (AGY2API_STATEFUL).
_session_store = SessionStore(settings.max_sessions) if settings.stateful else None
_conv_locks: dict[str, asyncio.Lock] = {}
# Conversations currently being run (resumed). Eviction must not delete these.
_in_flight: set[str] = set()


def _stateful_wipe() -> None:
    """Last-resort cleanup of persistent stateful sessions on exit. Covers the
    cases the FastAPI shutdown event misses (atexit-level interpreter shutdown,
    SIGTERM that uvicorn doesn't translate into a graceful shutdown)."""
    if not settings.stateful:
        return
    try:
        removed = sweep_all_conversations()
        if removed:
            logger.info("exit (stateful): wiped %d conversation db(s)", removed)
    except Exception as exc:
        logger.warning("exit wipe failed: %s", exc)


# atexit runs on normal interpreter exit (covers `python server.py` Ctrl-C /
# and sys.exit); the FastAPI shutdown event covers graceful uvicorn stop.
atexit.register(_stateful_wipe)


def _conv_lock(conversation_id: str) -> asyncio.Lock:
    lock = _conv_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _conv_locks[conversation_id] = lock
    return lock


async def _run_agy_guarded(
    prompt: str, model: str | None, conversation_id: str | None = None, keep: bool = False
):
    async with _run_semaphore:
        return await run_agy_async(prompt, model, conversation_id, keep)


async def _execute_run(prompt: str, model: str | None, resume_id: str | None, keep: bool):
    """Run agy, serializing turns of the same resumed conversation so concurrent
    requests for one chat don't corrupt its session.

    Note: planning (_plan_request) happens before this lock, so two *concurrent*
    turns of the SAME chat could plan against stale state. OpenAI clients
    (SillyTavern) serialize turns per chat — they wait for each reply — so this
    is not hit in practice; concurrent turns of one chat are unsupported."""
    if resume_id:
        async with _conv_lock(resume_id):
            _in_flight.add(resume_id)
            try:
                return await _run_agy_guarded(prompt, model, resume_id, keep)
            finally:
                _in_flight.discard(resume_id)
    return await _run_agy_guarded(prompt, model, None, keep)


def _msg_sigs(messages: list[Any]) -> list[str]:
    return [
        fingerprint(getattr(m, "role", "user"), _content_to_text(getattr(m, "content", "")))
        for m in messages
    ]


def _plan_request(messages: list[Any]) -> tuple[str, str | None, list[str]]:
    """Return (prompt_to_send, resume_conversation_id, sigs).

    In stateful mode, if this request continues a known chat, send only the new
    turn against the existing agy conversation. Otherwise send the full history.
    """
    if _session_store is None:
        return format_messages(messages), None, []
    sigs = _msg_sigs(messages)
    plan = _session_store.lookup(sigs)
    if plan.conversation_id and plan.prefix_len < len(messages):
        new = messages[plan.prefix_len:]
        # Drop leading assistant turns — those are agy's own prior replies that
        # the client echoes back; agy already has them in memory.
        while new and getattr(new[0], "role", "") == "assistant":
            new = new[1:]
        if new:
            return format_messages(new), plan.conversation_id, sigs
    return format_messages(messages), None, sigs


def _record_session(conversation_id: str, sigs: list[str]) -> None:
    if _session_store is None or not conversation_id:
        return
    for evicted in _session_store.remember(conversation_id, sigs, protected=_in_flight):
        _conv_locks.pop(evicted, None)  # drop the lock too (no unbounded growth)
        try:
            cleanup_conversation(evicted)
        except Exception as exc:  # housekeeping must not break the request
            logger.warning("evicted-session cleanup failed for %s: %s", evicted, exc)


def _forget_session(resume_id: str | None) -> None:
    """On a failed resumed turn, drop the session so the NEXT turn rebuilds from
    full history instead of resuming a conversation that's now wedged. Without
    this, a broken session keeps failing until the server restarts."""
    if _session_store is not None and resume_id:
        _session_store.forget(resume_id)
        _conv_locks.pop(resume_id, None)


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> None:
    if not settings.api_key:
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="missing bearer token")
    if credentials.credentials != settings.api_key:
        raise HTTPException(status_code=401, detail="invalid bearer token")


@app.get("/v1/models", response_model=ModelList)
async def list_models(_: None = Depends(verify_token)) -> ModelList:
    return ModelList(data=[ModelInfo(id=model) for model in settings.models])


@app.post("/v1/chat/completions")
async def chat_completions(
    request_body: ChatCompletionRequest,
    request: Request,
    _: None = Depends(verify_token),
):
    model = request_body.model
    agy_model = resolve_model(model)
    prompt, resume_id, sigs = _plan_request(request_body.messages)
    keep = settings.stateful
    logger.info(
        "request: model=%s (agy=%s) stream=%s messages=%d prompt_chars=%d resume=%s",
        model, agy_model, request_body.stream, len(request_body.messages),
        len(prompt), bool(resume_id),
    )

    if request_body.stream:
        return StreamingResponse(
            _stream_response(prompt, model, agy_model, request, resume_id, keep, sigs),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    started = time.time()
    try:
        result = await _execute_run(prompt, agy_model, resume_id, keep)
    except Exception as exc:
        _forget_session(resume_id)
        logger.error(
            "FAILED (non-stream): model=%s prompt_chars=%d after %.1fs -> %s",
            model, len(prompt), time.time() - started, exc, exc_info=True,
        )
        return _error_response(str(exc), status_code=500)

    _record_session(Path(result.db_path).stem, sigs)
    fr = "length" if result.response.truncated else "stop"
    logger.info(
        "ok (non-stream): model=%s answer_chars=%d reasoning_chars=%d truncated=%s db=%s resume=%s %.1fs",
        model, len(result.response.answer), len(result.response.reasoning),
        result.response.truncated, result.db_path.name, bool(resume_id), time.time() - started,
    )
    return JSONResponse(
        content=build_completion_response(
            answer=result.response.answer,
            reasoning=result.response.reasoning,
            model=model,
            finish_reason=fr,
        )
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _stream_response(
    prompt: str, model: str, agy_model: str | None, request: Request,
    resume_id: str | None = None, keep: bool = False, sigs: list[str] | None = None,
) -> AsyncIterator[bytes]:
    started = time.time()
    task = asyncio.create_task(_execute_run(prompt, agy_model, resume_id, keep))
    try:
        while not task.done():
            if await request.is_disconnected():
                logger.warning(
                    "client disconnected after %.1fs; agy run continues in background "
                    "until it finishes (cannot be interrupted mid-print)",
                    time.time() - started,
                )
                task.cancel()
                return
            yield sse_event(make_heartbeat(model))
            await asyncio.sleep(1)

        result = await task
        if sigs:
            _record_session(Path(result.db_path).stem, sigs)
        fr = "length" if result.response.truncated else "stop"
        logger.info(
            "ok (stream): model=%s answer_chars=%d reasoning_chars=%d truncated=%s db=%s resume=%s %.1fs",
            model, len(result.response.answer), len(result.response.reasoning),
            result.response.truncated, result.db_path.name, bool(resume_id), time.time() - started,
        )
        async for item in stream_chunks(result.response.answer, result.response.reasoning, model, finish_reason=fr):
            yield item
    except Exception as exc:
        _forget_session(resume_id)
        logger.error(
            "FAILED (stream): model=%s prompt_chars=%d after %.1fs -> %s",
            model, len(prompt), time.time() - started, exc, exc_info=True,
        )
        yield sse_event({"error": {"message": str(exc), "type": "agy_error"}})
        yield sse_event("[DONE]")


def format_messages(messages: list[Any]) -> str:
    lines: list[str] = []
    for message in messages:
        role = getattr(message, "role", "user")
        content = _content_to_text(getattr(message, "content", ""))
        if content:
            lines.append(f"{role}: {content}")
    return "\n\n".join(lines).strip()


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    pieces.append(str(item.get("text", "")))
                elif "text" in item:
                    pieces.append(str(item["text"]))
            else:
                pieces.append(str(item))
        return "\n".join(piece for piece in pieces if piece)
    return str(content)


def build_completion_response(answer: str, reasoning: str, model: str, finish_reason: str = "stop") -> dict:
    created = int(time.time())
    message: dict[str, Any] = {"role": "assistant", "content": answer}
    if settings.expose_reasoning and reasoning:
        message["reasoning_content"] = reasoning

    return {
        "id": f"chatcmpl-{created}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": len(answer),
            "total_tokens": len(answer),
        },
    }


def _error_response(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": "agy_error"}},
    )


def _enforce_bind_safety() -> None:
    if is_loopback_host(settings.host) or settings.allow_remote:
        return
    import sys

    sys.stderr.write(
        f"\nRefusing to bind non-loopback host {settings.host!r}.\n"
        "Exposing this endpoint shares your personal Google quota with anyone\n"
        "who can reach it. For personal/local use keep HOST=127.0.0.1.\n"
        "If you truly intend remote access, set AGY2API_ALLOW_REMOTE=1 and a\n"
        "strong AGY2API_KEY, and accept that callers run prompts under your\n"
        "Google account.\n"
    )
    sys.exit(2)


if __name__ == "__main__":
    import uvicorn

    _enforce_bind_safety()
    uvicorn.run("server:app", host=settings.host, port=settings.port, reload=False)
