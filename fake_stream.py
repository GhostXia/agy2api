"""OpenAI-compatible fake streaming helpers."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncIterator

from config import settings


def completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def split_text(text: str, size: int | None = None) -> list[str]:
    chunk_size = size or settings.chunk_size
    if not text:
        return []
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def build_chunks(answer: str, reasoning: str, model: str, finish_reason: str = "stop") -> list[dict]:
    cid = completion_id()
    created = int(time.time())
    chunks: list[dict] = [
        {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    ]

    if settings.expose_reasoning:
        for piece in split_text(reasoning):
            chunks.append(_chunk(cid, created, model, {"reasoning_content": piece}, None))

    answer_pieces = split_text(answer)
    if not answer_pieces:
        answer_pieces = [""]
    for piece in answer_pieces:
        chunks.append(_chunk(cid, created, model, {"content": piece}, None))

    chunks.append(_chunk(cid, created, model, {}, finish_reason))
    return chunks


def make_heartbeat(model: str) -> dict:
    return _chunk(completion_id(), int(time.time()), model, {"content": ""}, None)


def sse_event(data: dict | str) -> bytes:
    if isinstance(data, str):
        payload = data
    else:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n".encode("utf-8")


async def stream_chunks(answer: str, reasoning: str, model: str, finish_reason: str = "stop") -> AsyncIterator[bytes]:
    delay = settings.stream_delay
    for index, chunk in enumerate(build_chunks(answer, reasoning, model, finish_reason)):
        yield sse_event(chunk)
        # Drip content chunks so clients render a typing effect instead of one
        # burst. agy has no token-level streaming, so this is cosmetic pacing of
        # the already-complete answer. Skip the leading role chunk.
        if delay > 0 and index > 0:
            await asyncio.sleep(delay)
    yield sse_event("[DONE]")


def _chunk(
    cid: str,
    created: int,
    model: str,
    delta: dict,
    finish_reason: str | None,
) -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
