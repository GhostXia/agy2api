"""Runtime configuration for agy2api."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def _load_env_file() -> None:
    """Load key-value pairs from local .env if present."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        with env_path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                k = key.strip()
                v = val.strip().strip("\"'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_env_file()


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
    "gemini-3.8-flash": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
    "gemini-3.5-flash": "Gemini 3.8 Flash (Medium)",
    "gemini-3.5-flash-medium": "Gemini 3.8 Flash (Medium)",
    "gemini-3.5-flash-high": "Gemini 3.8 Flash (High)",
    "gemini-3.5-flash-low": "Gemini 3.8 Flash (Low)",
    "claude-sonnet-4.6": "Claude Sonnet 4.6 (Thinking)",
    "claude-sonnet-4-6": "Claude Sonnet 4.6 (Thinking)",
    "claude-opus-4.6": "Claude Opus 4.6 (Thinking)",
    "claude-opus-4-6": "Claude Opus 4.6 (Thinking)",
    "claude-opus-4-6-thinking": "Claude Opus 4.6 (Thinking)",
    "gpt-oss-120b": "GPT-OSS 120B (Medium)",
    "gpt-oss-120b-medium": "GPT-OSS 120B (Medium)",
}


# Aliases mapping legacy or alternative ids to canonical model families.
MODEL_ALIASES: dict[str, str] = {
    "gemini-3.5-flash": "gemini-3.8-flash",
    "gemini-3.5-flash-medium": "gemini-3.8-flash",
    "gemini-3.5-flash-high": "gemini-3.8-flash-high",
    "gemini-3.5-flash-low": "gemini-3.8-flash-low",
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    "claude-opus-4-6": "claude-opus-4.6",
    "claude-opus-4-6-thinking": "claude-opus-4.6",
    "gpt-oss-120b-medium": "gpt-oss-120b",
}


def resolve_model(
    model: str | None, effort: str | None = None
) -> tuple[str | None, str | None]:
    """Translate exposed model id and optional effort to (agy_model, agy_effort).

    If the catalog bakes effort into the model name,
    we select matching variant and drop agy_effort so agy does not exit with
    '--effort is not supported for model ...'.
    """
    if not model:
        return None, effort

    # Normalize alias first (e.g. gemini-3.5-flash -> gemini-3.8-flash)
    canonical = MODEL_ALIASES.get(model, model)

    effort_str = (effort or "").lower().strip()
    if effort_str:
        import re

        base_id = re.sub(r"-(low|medium|high)$", "", canonical)
        candidate_id = f"{base_id}-{effort_str}"
        if candidate_id in MODEL_MAP:
            return MODEL_MAP[candidate_id], None

    resolved = MODEL_MAP.get(canonical, MODEL_MAP.get(model, model))
    if resolved and any(
        marker in resolved for marker in ("(Medium)", "(High)", "(Low)", "(Thinking)")
    ):
        return resolved, None

    return resolved, effort


@dataclass(frozen=True)
class Settings:
    agy_path: str = _resolve_agy()
    api_key: str = os.getenv("AGY2API_KEY", os.getenv("API_KEY", "pwd"))
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "7862"))
    request_timeout: float = float(os.getenv("AGY_TIMEOUT", "300"))
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
    # Keep agy's `last_check.timestamp` fresh before each run so agy's background
    # auto-updater never thinks a check is due. This stops the `agy --bg-updater`
    # child process from spawning (it flashes its own console window despite our
    # CREATE_NO_WINDOW) AND prevents agy silently auto-updating underneath us
    # (which could change the DB/protobuf layout our parser depends on). Set
    # false to let agy auto-update normally.
    suppress_autoupdate: bool = _bool_env("AGY2API_SUPPRESS_AUTOUPDATE", True)
    # Experimental: keep a persistent agy conversation per chat and send only the
    # new turn each request (instead of resending the full history). Smaller
    # per-turn payloads finish faster and are less likely to hit an upstream
    # connection timeout on long chats. Opt-in; changes the cleanup lifecycle.
    stateful: bool = _bool_env("AGY2API_STATEFUL", False)
    max_sessions: int = int(os.getenv("AGY2API_MAX_SESSIONS", "200"))
    # When stateful, run agy inside its OWN home directory so our startup/exit
    # wipes never touch the user's real ~/.gemini (TUI conversations stay safe).
    # agy honors USERPROFILE for its home root, so we point it here. Default is a
    # `stateful_home/` folder INSIDE this project (gitignored), so it's easy to
    # find and manage. Set AGY2API_STATEFUL_HOME to override.
    stateful_home: Path = Path(
        os.getenv(
            "AGY2API_STATEFUL_HOME",
            str(Path(__file__).resolve().parent / "stateful_home"),
        )
    )
    conversations_dir: Path = Path(
        os.getenv(
            "AGY_CONVERSATIONS_DIR",
            str(Path.home() / ".gemini" / "antigravity-cli" / "conversations"),
        )
    )
    workdir: Path = Path(
        os.getenv(
            "AGY_WORKDIR", str(Path(__file__).resolve().parent / "isolated_workspace")
        )
    )
    # Lessons from relay.mjs: sandbox, permission automation, and read-only mode
    sandbox: bool = _bool_env("AGY2API_SANDBOX", False)
    dangerously_skip_permissions: bool = _bool_env("AGY2API_SKIP_PERMISSIONS", False)
    read_only: bool = _bool_env("AGY2API_READ_ONLY", False)
    models: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        if self.read_only:
            object.__setattr__(self, "sandbox", True)
            object.__setattr__(self, "dangerously_skip_permissions", True)
        object.__setattr__(
            self,
            "models",
            _csv_env("AGY_MODELS", list(MODEL_MAP.keys())),
        )
        # In stateful mode, redirect conversations_dir into the isolated home
        # so agy reads/writes only there (and our sweeps stay self-contained).
        if self.stateful:
            sandbox_conv = (
                self.stateful_home / ".gemini" / "antigravity-cli" / "conversations"
            )
            object.__setattr__(self, "conversations_dir", sandbox_conv)

    def agy_env(self) -> dict[str, str]:
        """Environment dict to pass to the agy subprocess.

        In stateful mode, set USERPROFILE to the isolated home so agy's whole
        data tree (conversations, brain, auth) lives there, fully separated
        from the user's real ~/.gemini. In stateless mode, pass the current
        environment unchanged.
        """
        if not self.stateful:
            return os.environ.copy()
        env = os.environ.copy()
        env["USERPROFILE"] = str(self.stateful_home)
        return env


settings = Settings()


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


def is_loopback_host(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_HOSTS
