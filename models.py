"""Pydantic models for the small OpenAI-compatible surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: Any = ""


class ChatCompletionRequest(BaseModel):
    model: str = "gemini-2.5-pro"
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "agy2api"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]


class ErrorBody(BaseModel):
    error: dict[str, Any] = Field(default_factory=dict)
