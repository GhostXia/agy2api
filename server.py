"""FastAPI entry point for agy2api."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agy_runner import run_agy_async
from config import is_loopback_host, resolve_model, settings
from fake_stream import make_heartbeat, sse_event, stream_chunks
from models import ChatCompletionRequest, ModelInfo, ModelList


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

app = FastAPI(title="agy2api", version="0.1.0")
security = HTTPBearer(auto_error=False)

# Limit concurrent agy runs (default 1) to mimic human-paced usage and avoid the
# concurrent conversation-DB race. See config.max_concurrency.
_run_semaphore = asyncio.Semaphore(max(1, settings.max_concurrency))


async def _run_agy_guarded(prompt: str, model: str | None):
    async with _run_semaphore:
        return await run_agy_async(prompt, model)


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
    prompt = format_messages(request_body.messages)
    model = request_body.model
    agy_model = resolve_model(model)
    logger.info(
        "request: model=%s (agy=%s) stream=%s messages=%d prompt_chars=%d",
        model, agy_model, request_body.stream, len(request_body.messages), len(prompt),
    )

    if request_body.stream:
        return StreamingResponse(
            _stream_response(prompt, model, agy_model, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    started = time.time()
    try:
        result = await _run_agy_guarded(prompt, agy_model)
    except Exception as exc:
        logger.error(
            "FAILED (non-stream): model=%s prompt_chars=%d after %.1fs -> %s",
            model, len(prompt), time.time() - started, exc, exc_info=True,
        )
        return _error_response(str(exc), status_code=500)

    fr = "length" if result.response.truncated else "stop"
    logger.info(
        "ok (non-stream): model=%s answer_chars=%d reasoning_chars=%d truncated=%s db=%s %.1fs",
        model, len(result.response.answer), len(result.response.reasoning),
        result.response.truncated, result.db_path.name, time.time() - started,
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
    prompt: str, model: str, agy_model: str | None, request: Request
) -> AsyncIterator[bytes]:
    started = time.time()
    task = asyncio.create_task(_run_agy_guarded(prompt, agy_model))
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
        fr = "length" if result.response.truncated else "stop"
        logger.info(
            "ok (stream): model=%s answer_chars=%d reasoning_chars=%d truncated=%s db=%s %.1fs",
            model, len(result.response.answer), len(result.response.reasoning),
            result.response.truncated, result.db_path.name, time.time() - started,
        )
        async for item in stream_chunks(result.response.answer, result.response.reasoning, model, finish_reason=fr):
            yield item
    except Exception as exc:
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
