"""Run the official Antigravity CLI and locate the generated conversation DB."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
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


def run_agy(
    prompt: str,
    model: str | None = None,
    conversation_id: str | None = None,
    keep: bool = False,
) -> AgyRunResult:
    """Run agy once.

    conversation_id: resume an existing agy conversation (stateful sessions) so
      only the new turn needs to be sent instead of the full history.
    keep: never delete the conversation DB afterwards (a stateful session owns
      its lifecycle and needs the memory for later turns).
    """
    start_time = time.time()

    # Validate prerequisites up front so failures name the real culprit instead
    # of a generic "agy not found" (subprocess raises FileNotFoundError for a
    # missing executable AND for a missing cwd).
    if not (Path(settings.agy_path).exists() or shutil.which(settings.agy_path)):
        raise RuntimeError(f"agy executable not found: {settings.agy_path}")
    workdir = Path(settings.workdir)
    if not workdir.exists():
        raise RuntimeError(f"agy working directory does not exist: {workdir}")
    # In stateful mode agy runs inside an isolated home; make sure it exists so
    # agy can write its data there. (Auth/login still lives in that home — see
    # the README; log in there once after enabling stateful mode.) The
    # conversations subdir is ours to create; the real auth state is not.
    if settings.stateful:
        settings.stateful_home.mkdir(parents=True, exist_ok=True)
        settings.conversations_dir.mkdir(parents=True, exist_ok=True)
    elif not settings.conversations_dir.exists():
        raise RuntimeError(
            f"agy conversations directory does not exist: {settings.conversations_dir} "
            "(is agy installed and logged in?)"
        )

    before = _snapshot_conversations(settings.conversations_dir)

    # Per-run unique log file. agy logs its conversation UUID there, and the
    # conversation DB is named "<uuid>.db" — so we read exactly THIS run's DB
    # instead of guessing by newest mtime. That makes concurrent runs safe (no
    # cross-talk between requests racing for the same "newest" database).
    log_fd, log_path = tempfile.mkstemp(prefix="agy2api-", suffix=".log")
    os.close(log_fd)

    # The prompt is fed via stdin (with an empty --print value) instead of as a
    # command-line argument. agy concatenates stdin into the prompt, and stdin
    # has no length limit, avoiding the Windows ~32767-char command-line cap that
    # raises WinError 206 for long prompts (system + history).
    # Give agy its OWN print-timeout equal to our budget, and let our subprocess
    # timeout sit a margin ABOVE it. That way agy hits its timeout first and
    # flushes whatever partial response it has (degrade-readable) instead of us
    # hard-killing it and truncating long replies.
    command = [
        settings.agy_path,
        "--print", "",
        "--log-file", log_path,
        "--print-timeout", f"{int(settings.request_timeout)}s",
    ]
    if conversation_id:
        command.extend(["--conversation", conversation_id])
    if model:
        command.extend(["--model", model])

    # Windows: suppress the console window that flashes for each agy subprocess.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    try:
        try:
            completed = subprocess.run(
                command,
                cwd=workdir,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=settings.request_timeout + 20,
                check=False,
                env=settings.agy_env(),
                creationflags=creationflags,
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

        # Attempt to read the conversation DB *before* checking returncode.
        # When agy is killed mid-generation (returncode -1 on Windows, agy-side
        # timeout, external process kill), the DB may contain a partial response
        # (status=2) that conversation_reader can degrade-read into usable text.
        # Skipping the DB on non-zero exit was the original blank-response bug.
        db_path = _db_from_log(log_path)
        # owned_by_us: the DB was positively identified via THIS run's --log-file
        # conversation id, so it is definitely the conversation this invocation
        # created. The fallback below is a heuristic ("newest DB") that could in
        # theory point at a conversation the user started manually at the same
        # moment — so we never auto-delete in that case.
        owned_by_us = db_path is not None
        if db_path is None:
            # Fallback: log gave no conversation id (older agy?) — pick newest new DB.
            db_path = _find_new_conversation_db(settings.conversations_dir, before, start_time)

        if db_path is not None:
            response = _read_response_with_retry(db_path)
            # Only delete artifacts we positively own (resolved via our log id),
            # on a clean, non-truncated run. Never delete a heuristically-matched
            # DB — it might be the user's own manual agy conversation.
            if (
                settings.cleanup_db
                and not keep
                and owned_by_us
                and completed.returncode == 0
                and not response.truncated
            ):
                _cleanup_conversation(db_path)
            return AgyRunResult(
                db_path=db_path,
                response=response,
                stderr=completed.stderr,
            )

        # No DB found at all — now report the exit status.
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip() or "no output"
            raise RuntimeError(
                f"agy exited with code {completed.returncode}: {details} "
                f"(no conversation DB found; possible agy-side timeout or crash)"
            )
        raise RuntimeError("agy completed but no new conversation database was found")
    finally:
        try:
            os.remove(log_path)
        except OSError:
            pass


_CONVERSATION_ID_RE = re.compile(
    r"conversation[\"=:\s]+([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


# SQLite (WAL mode) keeps these sidecar files next to each <id>.db.
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _cleanup_conversation(db_path: Path) -> None:
    """Remove a finished run's local artifacts: the conversation DB, its SQLite
    WAL/SHM sidecars, and the sibling brain/<id>/ directory. Best-effort.

    The DB stays briefly locked right after agy exits (Windows handle/flush
    lag), so retry the unlink for a short window before giving up."""
    deadline = time.time() + 3.0
    while True:
        try:
            db_path.unlink()
            break
        except FileNotFoundError:
            break
        except OSError:
            if time.time() >= deadline:
                break
            time.sleep(0.2)
    for suffix in _DB_SIDECAR_SUFFIXES:
        sidecar = db_path.with_name(db_path.name + suffix)
        try:
            sidecar.unlink()
        except OSError:
            pass
    brain_dir = db_path.parent.parent / "brain" / db_path.stem
    if brain_dir.is_dir():
        shutil.rmtree(brain_dir, ignore_errors=True)


def cleanup_conversation(conversation_id: str) -> None:
    """Public: delete a conversation's DB + sidecars + brain dir by id. Used to
    drop evicted stateful sessions."""
    _cleanup_conversation(settings.conversations_dir / f"{conversation_id}.db")


def sweep_orphan_sidecars() -> int:
    """Delete SQLite sidecar files whose parent .db no longer exists. These are
    pure garbage left behind (e.g. by older cleanup that only removed the .db).
    Returns the number of files removed."""
    directory = settings.conversations_dir
    if not directory.exists():
        return 0
    removed = 0
    for suffix in _DB_SIDECAR_SUFFIXES:
        for sidecar in directory.glob(f"*.db{suffix}"):
            base = sidecar.with_name(sidecar.name[: -len(suffix)])
            if not base.exists():
                try:
                    sidecar.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def sweep_all_conversations() -> int:
    """Delete EVERY conversation artifact: every *.db (+ its SQLite sidecars)
    and every brain/<id>/ directory.

    This is the hard reset behind AGY2API_STATEFUL. The session store is pure
    in-memory, so after a process restart the persistent .db files it kept
    alive are orphans no eviction can ever reach. To stop them accumulating we
    wipe the whole directory on startup and on clean shutdown. Stateful memory
    does not survive a restart anyway, so nothing of value is lost.

    Destructive: also removes conversations the user opened manually in the agy
    TUI. Acceptable for this personal-use tool; documented in the README.

    Returns the number of *.db files removed."""
    directory = settings.conversations_dir
    if not directory.exists():
        return 0
    removed = 0
    for db_path in directory.glob("*.db"):
        _cleanup_conversation(db_path)
        if not db_path.exists():  # count only DBs actually deleted
            removed += 1
    # brain/<id>/ dirs whose .db we may already have removed (or never existed).
    brain_dir = directory.parent / "brain"
    if brain_dir.is_dir():
        for sub in brain_dir.iterdir():
            if sub.is_dir():
                shutil.rmtree(sub, ignore_errors=True)
    return removed


def _db_from_log(log_path: str) -> Path | None:
    """Map this run's agy log to its conversation DB via the logged UUID, named
    "<uuid>.db". The DB file can appear a beat after the id is logged, so poll
    for it (deterministic per-run filename — safe under concurrency)."""
    log_file = Path(log_path)
    deadline = time.time() + max(2.0, settings.poll_interval * 8)
    candidate: Path | None = None
    while time.time() <= deadline:
        if candidate is None:
            try:
                ids = _CONVERSATION_ID_RE.findall(
                    log_file.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                ids = []
            if ids:
                candidate = settings.conversations_dir / f"{ids[-1]}.db"
        if candidate is not None and _looks_ready(candidate):
            return candidate
        time.sleep(settings.poll_interval)
    return candidate if candidate is not None and _looks_ready(candidate) else None


def _read_response_with_retry(db_path: Path) -> AgResponse:
    """Read the response, tolerating a brief lag between agy's process exit and
    the final completed model row being flushed to the conversation DB."""
    deadline = time.time() + 3.0
    while True:
        try:
            return read_response(db_path)
        except ValueError:
            if time.time() >= deadline:
                raise
            time.sleep(0.2)


async def run_agy_async(
    prompt: str,
    model: str | None = None,
    conversation_id: str | None = None,
    keep: bool = False,
) -> AgyRunResult:
    return await asyncio.to_thread(run_agy, prompt, model, conversation_id, keep)


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
