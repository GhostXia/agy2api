"""Runtime configuration for agy2api."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def _resolve_agy() -> str:
    """Find the agy executable regardless of the launching shell's PATH.

    Order: explicit AGY_PATH env > on PATH > known per-user install location.
    Falls back to the bare name so the original FileNotFoundError still surfaces
    a clear message if nothing is found.
    """
    explicit = os.getenv("AGY_PATH")
    if explicit:
        return explicit
    found = shutil.which("agy")
    if found:
        return found
    candidate = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"
    if candidate.exists():
        return str(candidate)
    return "agy"


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


# Maps clean OpenAI-style model ids (exposed via /v1/models) to the exact
# display labels that `agy models` prints and `agy --model` accepts verbatim.
# Update this if `agy models` changes its catalog.
MODEL_MAP: dict[str, str] = {
    "gemini-3.5-flash": "Gemini 3.5 Flash (Medium)",
    "gemini-3.5-flash-high": "Gemini 3.5 Flash (High)",
    "gemini-3.5-flash-low": "Gemini 3.5 Flash (Low)",
    "gemini-3.1-pro": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
    "claude-sonnet-4.6": "Claude Sonnet 4.6 (Thinking)",
    "claude-opus-4.6": "Claude Opus 4.6 (Thinking)",
    "gpt-oss-120b": "GPT-OSS 120B (Medium)",
}


def resolve_model(model: str | None) -> str | None:
    """Translate an exposed model id to the agy label. Pass through unknown
    values unchanged so raw `agy` labels also work directly."""
    if not model:
        return None
    return MODEL_MAP.get(model, model)


@dataclass(frozen=True)
class Settings:
    agy_path: str = _resolve_agy()
    api_key: str = os.getenv("AGY2API_KEY", os.getenv("API_KEY", "pwd"))
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "7862"))
    request_timeout: float = float(os.getenv("AGY_TIMEOUT", "180"))
    poll_interval: float = float(os.getenv("AGY_POLL_INTERVAL", "0.25"))
    chunk_size: int = int(os.getenv("AGY2API_CHUNK_SIZE", "10"))
    stream_delay: float = float(os.getenv("AGY2API_STREAM_DELAY", "0.03"))
    expose_reasoning: bool = _bool_env("AGY2API_EXPOSE_REASONING", True)
    # Safety: refuse to bind a non-loopback address unless explicitly allowed.
    # Exposing this endpoint shares your personal Google quota with others.
    allow_remote: bool = _bool_env("AGY2API_ALLOW_REMOTE", False)
    # Concurrent agy runs. Safe now that each run reads its OWN conversation DB
    # (resolved from the per-run --log-file conversation id), not the newest by
    # mtime. Kept modest to stay human-paced / avoid abuse-like quota bursts.
    max_concurrency: int = int(os.getenv("AGY2API_MAX_CONCURRENCY", "3"))
    # Stateless wrapper: full history is resent in each prompt, so a finished
    # run's conversation DB + brain/ dir are throwaway. Delete them after a
    # successful read to keep local session files from piling up. Set false to
    # retain them for debugging. (Does NOT reduce server-side conversation count.)
    cleanup_db: bool = _bool_env("AGY2API_CLEANUP_DB", True)
    conversations_dir: Path = Path(
        os.getenv(
            "AGY_CONVERSATIONS_DIR",
            str(Path.home() / ".gemini" / "antigravity-cli" / "conversations"),
        )
    )
    workdir: Path = Path(
        os.getenv("AGY_WORKDIR", str(Path(__file__).resolve().parent.parent))
    )
    models: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "models",
            _csv_env("AGY_MODELS", list(MODEL_MAP.keys())),
        )


settings = Settings()


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


def is_loopback_host(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_HOSTS
