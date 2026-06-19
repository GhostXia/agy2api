"""FastAPI entry point for agy2api."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agy_runner import run_agy_async
from config import resolve_model, settings
from fake_stream import make_heartbeat, sse_event, stream_chunks
from models import ChatCompletionRequest, ModelInfo, ModelList


app = FastAPI(title="agy2api", version="0.1.0")
security = HTTPBearer(auto_error=False)


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

    if request_body.stream:
        return StreamingResponse(
            _stream_response(prompt, model, agy_model, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        result = await run_agy_async(prompt, agy_model)
    except Exception as exc:
        return _error_response(str(exc), status_code=500)

    return JSONResponse(
        content=build_completion_response(
            answer=result.response.answer,
            reasoning=result.response.reasoning,
            model=model,
        )
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _stream_response(
    prompt: str, model: str, agy_model: str | None, request: Request
) -> AsyncIterator[bytes]:
    task = asyncio.create_task(run_agy_async(prompt, agy_model))
    try:
        while not task.done():
            if await request.is_disconnected():
                task.cancel()
                return
            yield sse_event(make_heartbeat(model))
            await asyncio.sleep(1)

        result = await task
        async for item in stream_chunks(result.response.answer, result.response.reasoning, model):
            yield item
    except Exception as exc:
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


def build_completion_response(answer: str, reasoning: str, model: str) -> dict:
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
                "finish_reason": "stop",
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=settings.host, port=settings.port, reload=False)
