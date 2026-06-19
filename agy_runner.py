"""Run the official Antigravity CLI and locate the generated conversation DB."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from config import settings
from conversation_reader import AgResponse, read_response


@dataclass(frozen=True)
class AgyRunResult:
    db_path: Path
    response: AgResponse
    stderr: str = ""


def run_agy(prompt: str, model: str | None = None) -> AgyRunResult:
    start_time = time.time()

    # Validate prerequisites up front so failures name the real culprit instead
    # of a generic "agy not found" (subprocess raises FileNotFoundError for a
    # missing executable AND for a missing cwd).
    if not (Path(settings.agy_path).exists() or shutil.which(settings.agy_path)):
        raise RuntimeError(f"agy executable not found: {settings.agy_path}")
    workdir = Path(settings.workdir)
    if not workdir.exists():
        raise RuntimeError(f"agy working directory does not exist: {workdir}")
    if not settings.conversations_dir.exists():
        raise RuntimeError(
            f"agy conversations directory does not exist: {settings.conversations_dir} "
            "(is agy installed and logged in?)"
        )

    before = _snapshot_conversations(settings.conversations_dir)

    # The prompt is fed via stdin (with an empty --print value) instead of as a
    # command-line argument. agy concatenates stdin into the prompt, and stdin
    # has no length limit, avoiding the Windows ~32767-char command-line cap that
    # raises WinError 206 for long prompts (system + history).
    command = [settings.agy_path, "--print", ""]
    if model:
        command.extend(["--model", model])

    try:
        completed = subprocess.run(
            command,
            cwd=workdir,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.request_timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        winerror = getattr(exc, "winerror", None)
        diag = (
            f"agy launch failed (winerror={winerror}); "
            f"exe={settings.agy_path!r} exists={Path(settings.agy_path).exists()}; "
            f"cwd={str(workdir)!r} exists={workdir.exists()}; "
            f"exc_filename={getattr(exc, 'filename', None)!r}"
        )
        raise RuntimeError(diag) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"agy timed out after {settings.request_timeout:g}s") from exc

    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise RuntimeError(f"agy exited with code {completed.returncode}: {details}")

    db_path = _find_new_conversation_db(settings.conversations_dir, before, start_time)
    if db_path is None:
        raise RuntimeError("agy completed but no new conversation database was found")

    return AgyRunResult(
        db_path=db_path,
        response=read_response(db_path),
        stderr=completed.stderr,
    )


async def run_agy_async(prompt: str, model: str | None = None) -> AgyRunResult:
    return await asyncio.to_thread(run_agy, prompt, model)


def _snapshot_conversations(directory: Path) -> dict[Path, float]:
    if not directory.exists():
        return {}
    return {path: path.stat().st_mtime for path in directory.glob("*.db")}


def _find_new_conversation_db(
    directory: Path,
    before: dict[Path, float],
    start_time: float,
) -> Path | None:
    deadline = time.time() + max(2.0, settings.poll_interval * 4)
    newest: Path | None = None

    while time.time() <= deadline:
        candidates: list[Path] = []
        if directory.exists():
            for path in directory.glob("*.db"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if path not in before or mtime > before.get(path, 0) or mtime >= start_time - 1:
                    candidates.append(path)

        if candidates:
            newest = max(candidates, key=lambda item: item.stat().st_mtime)
            if _looks_ready(newest):
                return newest

        time.sleep(settings.poll_interval)

    return newest if newest and _looks_ready(newest) else None


def _looks_ready(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False
