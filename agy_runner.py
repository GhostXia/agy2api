"""Run the official Antigravity CLI and locate the generated conversation DB."""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from config import settings
from conversation_reader import AgResponse, UpstreamError, read_response

# Lesson from relay.mjs: catch headless auto-denied tool permissions
_PERMISSION_DENIED_RE = re.compile(
    r'(?:no output produced\s+[—-]\s+)?a tool required the "([^"]+)" permission that headless\s+mode cannot prompt for',
    re.IGNORECASE,
)

# Same logger name as server.py so retry/timeout warnings share one format.
logger = logging.getLogger("agy2api")


@dataclass(frozen=True)
class AgyRunResult:
    db_path: Path
    response: AgResponse
    stderr: str = ""


def _crash_diagnostic(returncode: int | None) -> str:
    """Lesson from relay.mjs: classify process deaths into actionable hints."""
    if returncode is None:
        return ""
    if returncode in (-9, 137):
        return " [hint: process was killed with SIGKILL; commonly host OOM killer or supervisor timeout]"
    if returncode in (-15, 143):
        return " [hint: process was terminated by SIGTERM; manual kill or external supervisor]"
    if returncode in (-1073741510, -1, 0xC000013A):
        return " [hint: process was aborted or interrupted by user on Windows]"
    if returncode in (-1073741819, 0xC0000005):
        return " [hint: process crashed with memory Access Violation (0xC0000005)]"
    return ""


def _tail_lines(text: str, max_lines: int = 20) -> str:
    """Lesson from relay.mjs: ring buffer tail of last max_lines."""
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    return "\n".join(lines[-max_lines:])


def _enrich_error(exc: Exception, completed: subprocess.CompletedProcess) -> Exception:
    """Attach agy's exit status + rolling stderr tail to a read/parse error
    raised after a DB lookup.
    """
    stderr_tail = _tail_lines(completed.stderr or "", max_lines=20)
    stdout_tail = _tail_lines(completed.stdout or "", max_lines=10)
    diag_hint = _crash_diagnostic(completed.returncode)

    enriched = f"{exc} | agy returncode={completed.returncode}{diag_hint}"
    if stderr_tail:
        enriched += f" | stderr tail:\n{stderr_tail}"
    if stdout_tail:
        enriched += f" | stdout tail:\n{stdout_tail}"
    return (
        type(exc)(enriched) if isinstance(exc, ValueError) else RuntimeError(enriched)
    )


def check_windows_command_length(command: list[str]) -> int:
    """Validate Windows command line length against CreateProcessW's ~32,767 limit."""
    assembled_cmd = subprocess.list2cmdline(command)
    utf16_len = len(assembled_cmd.encode("utf-16-le")) // 2
    if utf16_len > 32_000:
        raise ValueError(
            f"Command line length ({utf16_len} UTF-16 code units) exceeds Windows limit (~32KB). "
            "Please enable stateful mode (AGY2API_STATEFUL=1) to send only incremental turns."
        )
    return utf16_len


def _run_agy_once(
    prompt: str,
    model: str | None,
    conversation_id: str | None,
    keep: bool,
    effort: str | None = None,
) -> AgyRunResult:
    """Run agy exactly once and read the result. See run_agy for retry policy."""
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

    _keep_updater_quiet()
    before = _snapshot_conversations(settings.conversations_dir)

    # Per-run unique log file. agy logs its conversation UUID there, and the
    # conversation DB is named "<uuid>.db" — so we read exactly THIS run's DB
    # instead of guessing by newest mtime. That makes concurrent runs safe (no
    # cross-talk between requests racing for the same "newest" database).
    log_fd, log_path = tempfile.mkstemp(prefix="agy2api-", suffix=".log")
    os.close(log_fd)

    try:
        command = [
            settings.agy_path,
            f"--print={prompt}",
            "--disable-slash-commands",
            "--log-file",
            log_path,
            "--print-timeout",
            f"{int(settings.request_timeout)}s",
        ]
        # Lesson from relay.mjs: explicitly anchor workspace directory via absolute path
        command.extend(["--add-dir", str(Path(workdir).resolve())])
        if settings.sandbox:
            command.append("--sandbox")
        if settings.dangerously_skip_permissions:
            command.append("--dangerously-skip-permissions")
        if conversation_id:
            command.extend(["--conversation", conversation_id])
        if model:
            command.extend(["--model", model])
        # Lesson from relay.mjs: reasoning effort passed directly to agy --effort
        if effort:
            command.extend(["--effort", effort])

        # Lesson from relay.mjs: On Windows, CreateProcessW limits lpCommandLine to ~32,767
        # UTF-16 code units. Check the fully assembled command line to prevent WinError 206.
        if os.name == "nt":
            check_windows_command_length(command)

        # Windows: suppress the console window that flashes for each agy subprocess.
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )

        try:
            # Lesson from relay.mjs: read raw bytes and decode using incremental UTF-8
            # decoder so multi-byte Unicode/Arabic characters are never mangled across chunk boundaries.
            proc = subprocess.Popen(
                command,
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=settings.agy_env(),
                creationflags=creationflags,
            )
            try:
                raw_stdout, raw_stderr = proc.communicate(
                    timeout=settings.request_timeout + 20
                )
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                stdout = decoder.decode(raw_stdout, final=True)
                stderr = decoder.decode(raw_stderr, final=True)
                completed = subprocess.CompletedProcess(
                    args=command,
                    returncode=proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
            except subprocess.TimeoutExpired as exc:
                # Lesson from relay.mjs: forceful tree termination via taskkill on Windows
                if os.name == "nt" and proc.pid:
                    try:
                        subprocess.run(
                            ["taskkill", "/pid", str(proc.pid), "/t", "/f"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                            check=False,
                        )
                    except Exception:
                        proc.kill()
                else:
                    proc.kill()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
                raise RuntimeError(
                    f"agy timed out after {settings.request_timeout:g}s"
                ) from exc
            finally:
                # Lesson from relay.mjs: drain & close pipe streams so grandchild processes never hang
                for stream in (proc.stdout, proc.stderr, proc.stdin):
                    if stream and not stream.closed:
                        try:
                            stream.close()
                        except Exception:
                            pass
        except FileNotFoundError as exc:
            winerror = getattr(exc, "winerror", None)
            diag = (
                f"agy launch failed (winerror={winerror}); "
                f"exe={settings.agy_path!r} exists={Path(settings.agy_path).exists()}; "
                f"cwd={str(workdir)!r} exists={workdir.exists()}; "
                f"exc_filename={getattr(exc, 'filename', None)!r}"
            )
            raise RuntimeError(diag) from exc

        # Lesson from relay.mjs: inspect stderr for headless auto-denied tool permissions
        perm_match = _PERMISSION_DENIED_RE.search(completed.stderr or "")
        if perm_match:
            raise RuntimeError(
                f"Antigravity auto-denied the '{perm_match.group(1)}' tool permission because "
                "headless print mode cannot prompt for permissions. Enable AGY2API_SKIP_PERMISSIONS=1 if needed."
            )

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
            db_path = _find_new_conversation_db(
                settings.conversations_dir, before, start_time
            )

        if db_path is not None:
            try:
                response = _read_response_with_retry(db_path)
            except ValueError as exc:
                # Surface agy's exit status + stderr/stdout alongside the parse
                # failure. An empty/partial DB used to hide agy's real exit
                # reason; now log/error responses carry it. UpstreamError is a
                # ValueError subclass, so its retryable classification survives.
                #
                # A FRESH conversation we created that produced no usable
                # response is throwaway garbage — delete it so failed runs
                # (especially the retries above) don't accumulate orphan DBs.
                # Keep resumed-session DBs (the chat's live memory) and keep
                # everything when cleanup is disabled (debugging).
                if settings.cleanup_db and owned_by_us and conversation_id is None:
                    _cleanup_conversation(db_path)
                raise _enrich_error(exc, completed) from exc
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

        # No DB found at all — fallback lesson from relay.mjs: if agy printed to stdout, use it!
        stdout_text = (completed.stdout or "").strip()
        if completed.returncode == 0 and stdout_text:
            return AgyRunResult(
                db_path=Path(""),
                response=AgResponse(
                    answer=stdout_text,
                    reasoning="",
                    tool_summaries=(),
                    db_path="",
                    truncated=False,
                ),
                stderr=completed.stderr,
            )

        # Lesson from relay.mjs: reject silent no-op (exit 0 with no response or output)
        if completed.returncode == 0:
            raise RuntimeError(
                "agy exited with code 0 without producing a final message or database response "
                "(silent no-op). Please verify the prompt and model selection."
            )

        details = _tail_lines(completed.stderr) or stdout_text or "no output"
        diag_hint = _crash_diagnostic(completed.returncode)
        raise RuntimeError(
            f"agy exited with code {completed.returncode}{diag_hint}: {details} "
            f"(no conversation DB found; possible agy-side timeout or crash)"
        )
    finally:
        try:
            os.remove(log_path)
        except OSError:
            pass


# Retry policy for transient upstream failures (Google rate-limit / backend
# drop / model unreachable). 3 attempts total = up to 2 retries, waiting 2s then
# 4s. Kept short so the added latency stays well under the per-run budget and one
# agy attempt always has its full --print-timeout available.
_UPSTREAM_RETRY_MAX = 3
_UPSTREAM_BACKOFFS = (2.0, 4.0)

# Markers that make an upstream error PERMANENT for the *same* prompt: retrying
# would re-hit the same wall and only burn quota + latency. Content-safety /
# policy blocks and hard quota caps live here. Best-effort substring match on the
# error text (including agy's enriched stderr); unknown phrasings stay retryable.
_PERMANENT_MARKERS = (
    "safety",
    "block_reason",
    "blocked",
    "content policy",
    "policy violation",
    "prohibited",
    "quota exceeded",
    "permission denied",
    "unauthorized",
)


def _is_permanent_upstream(message: str) -> bool:
    """True if an upstream error looks permanent for this prompt (content-safety
    / policy / hard quota), so retrying the same prompt is pointless."""
    low = message.lower()
    return any(marker in low for marker in _PERMANENT_MARKERS)


def run_agy(
    prompt: str,
    model: str | None = None,
    conversation_id: str | None = None,
    keep: bool = False,
    effort: str | None = None,
) -> AgyRunResult:
    """Run agy, retrying transient upstream errors.

    UpstreamError (rate-limit / backend drop / model unreachable, as classified
    by conversation_reader._ERROR_KEYWORDS) is retried up to 2 times (3 attempts
    total) with 2s/4s backoff. NOT retried: parse errors, empty-DB failures,
    config/launch errors, and upstream errors that look permanent for this prompt
    (content-safety / policy / hard quota, see _is_permanent_upstream) —
    surfacing those immediately avoids burning quota + latency on a doomed retry.

    conversation_id: resume an existing agy conversation (stateful sessions) so
      only the new turn needs to be sent instead of the full history.
    keep: never delete the conversation DB afterwards (a stateful session owns
      its lifecycle and needs the memory for later turns).
    effort: reasoning effort level for models that support it (low, medium, high, etc.).
    """
    last_exc: Exception | None = None
    for attempt in range(_UPSTREAM_RETRY_MAX):
        try:
            return _run_agy_once(prompt, model, conversation_id, keep, effort)
        except UpstreamError as exc:
            last_exc = exc
            if _is_permanent_upstream(str(exc)):
                # Same prompt would re-hit the same block — surface immediately.
                logger.warning(
                    "agy upstream error looks permanent; not retrying: %s", exc
                )
                raise
            if attempt + 1 < _UPSTREAM_RETRY_MAX:
                wait = _UPSTREAM_BACKOFFS[min(attempt, len(_UPSTREAM_BACKOFFS) - 1)]
                logger.warning(
                    "agy upstream error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    _UPSTREAM_RETRY_MAX,
                    wait,
                    exc,
                )
                time.sleep(wait)
            else:
                logger.warning(
                    "agy upstream error exhausted %d attempts: %s",
                    _UPSTREAM_RETRY_MAX,
                    exc,
                )
    # Exhausted retries — surface the last upstream error verbatim.
    assert last_exc is not None
    raise last_exc


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


def _keep_updater_quiet() -> None:
    """Touch agy's `last_check.timestamp` to 'now' so agy's background
    auto-updater treats the check as fresh and does NOT spawn the
    `agy --bg-updater` child (which flashes its own console window despite our
    CREATE_NO_WINDOW, and could silently update agy underneath us). Best-effort;
    never raises. A future timestamp is treated as invalid by agy, so use now."""
    if not settings.suppress_autoupdate:
        return
    try:
        marker = settings.conversations_dir.parent / "last_check.timestamp"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
        now = time.time()
        os.utime(marker, (now, now))
    except OSError:
        pass


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
        except UpstreamError:
            # agy already exited and wrote an error step — the DB is static, so
            # re-reading won't change it. Re-raise now and let run_agy decide
            # whether to retry the whole run (with backoff), instead of burning
            # the 3s read-retry window here on every attempt.
            raise
        except ValueError:
            if time.time() >= deadline:
                raise
            time.sleep(0.2)


async def run_agy_async(
    prompt: str,
    model: str | None = None,
    conversation_id: str | None = None,
    keep: bool = False,
    effort: str | None = None,
) -> AgyRunResult:
    return await asyncio.to_thread(
        run_agy, prompt, model, conversation_id, keep, effort
    )


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
                if (
                    path not in before
                    or mtime > before.get(path, 0)
                    or mtime >= start_time - 1
                ):
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
