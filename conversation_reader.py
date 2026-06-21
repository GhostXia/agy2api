"""Read Antigravity conversation SQLite files and extract model responses."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from proto_decoder import ProtoField, decode_message, find_fields, first_text, raw_text, walk_text


USER_STEP_TYPE = 14
MODEL_STEP_TYPE = 15
TOOL_STEP_TYPES = {8, 9}
ERROR_STEP_TYPE = 17
DONE_STATUS = 3

# Keywords that mark an agy error step (type 17) as an upstream/transport
# failure worth surfacing verbatim to the caller.
_ERROR_KEYWORDS = (
    "model unreachable",
    "forcibly closed",
    "terminated due to error",
    "retryable error",
    "failed to get load code assist",
    "stream reading error",
)


class AgyUpstreamError(ValueError):
    """Raised when agy logged a transient upstream/transport failure (e.g. the
    connection to Google was reset mid-stream). Subclasses ValueError so callers
    that already catch ValueError keep working; the runner catches this
    specifically to retry the whole run."""


@dataclass(frozen=True)
class AgResponse:
    answer: str
    reasoning: str = ""
    tool_summaries: tuple[str, ...] = ()
    db_path: str = ""
    truncated: bool = False


def read_response(db_path: str | Path) -> AgResponse:
    path = Path(db_path)
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT idx, step_type, status, step_payload FROM steps ORDER BY idx"
        ).fetchall()
    finally:
        conn.close()

    tool_summaries = _extract_tool_summaries(rows)

    # Scan ALL completed model rows (newest first) for one that actually carries
    # an answer. agy can emit several type-15 rows — reasoning-only, empty
    # terminal markers, etc. — so picking just the last one misses the answer.
    completed = [
        payload
        for _, step_type, status, payload in rows
        if step_type == MODEL_STEP_TYPE and status == DONE_STATUS and isinstance(payload, bytes)
    ]
    reasoning_only = ""
    for payload in reversed(completed):
        answer, reasoning = _extract_model_response(payload)
        if answer:
            return AgResponse(
                answer=answer,
                reasoning=reasoning,
                tool_summaries=tuple(tool_summaries),
                db_path=str(path),
            )
        if reasoning and not reasoning_only:
            reasoning_only = reasoning

    # No answer anywhere. If agy logged an upstream/transport error (type 17),
    # surface it verbatim — this is usually a network/proxy drop to Google, not
    # a parsing problem.
    error = _extract_error(rows)
    if error:
        raise AgyUpstreamError(f"agy upstream error: {error}")

    # Degraded read: agy killed mid-generation may leave a partial (non-DONE)
    # row whose text is still better than a blank response.
    incomplete = [
        payload
        for _, step_type, status, payload in rows
        if step_type == MODEL_STEP_TYPE and status != DONE_STATUS and isinstance(payload, bytes)
    ]
    for payload in reversed(incomplete):
        answer, reasoning = _extract_model_response(payload)
        if answer or reasoning:
            return AgResponse(
                answer=answer,
                reasoning=reasoning,
                tool_summaries=tuple(tool_summaries),
                db_path=str(path),
                truncated=True,
            )

    # Last resort: reasoning was produced but no answer and no explicit error.
    if reasoning_only:
        return AgResponse(
            answer="",
            reasoning=reasoning_only,
            tool_summaries=tuple(tool_summaries),
            db_path=str(path),
            truncated=True,
        )

    raise ValueError(f"no model response found in {path}")


def read_user_prompt(db_path: str | Path) -> str:
    path = Path(db_path)
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT step_payload
            FROM steps
            WHERE step_type = ? AND status = ?
            ORDER BY idx
            """,
            (USER_STEP_TYPE, DONE_STATUS),
        ).fetchall()
    finally:
        conn.close()

    for (payload,) in rows:
        if not isinstance(payload, bytes):
            continue
        fields = decode_message(payload)
        for field19 in find_fields(fields, 19):
            if isinstance(field19.value, list):
                prompt = first_text(field19.value, 2)
                if prompt:
                    return prompt
    return ""


def _extract_model_response(payload: bytes) -> tuple[str, str]:
    fields = decode_message(payload)
    response_fields = [
        field.value
        for field in find_fields(fields, 20)
        if isinstance(field.value, list)
    ]
    if not response_fields:
        return "", ""

    response = response_fields[-1]
    answer = raw_text(response, 1)
    reasoning = raw_text(response, 3)
    return answer, reasoning


def _extract_error(rows: list[tuple[int, int, int, bytes]]) -> str:
    """Pull a concise upstream-error message out of agy error steps (type 17)."""
    found: list[str] = []
    for _, step_type, _status, payload in rows:
        if step_type != ERROR_STEP_TYPE or not isinstance(payload, bytes):
            continue
        for text in walk_text(decode_message(payload)):
            normalized = " ".join(text.split())
            if len(normalized) < 10:
                continue
            low = normalized.lower()
            if any(kw in low for kw in _ERROR_KEYWORDS):
                snippet = normalized[:200]
                if snippet not in found:
                    found.append(snippet)
    return " | ".join(found[:2])


def _extract_tool_summaries(rows: list[tuple[int, int, int, bytes]]) -> list[str]:
    summaries: list[str] = []
    for _, step_type, status, payload in rows:
        if step_type not in TOOL_STEP_TYPES or status != DONE_STATUS or not isinstance(payload, bytes):
            continue
        fields = decode_message(payload)
        summaries.extend(_pick_tool_texts(fields))
    return summaries


def _pick_tool_texts(fields: list[ProtoField]) -> list[str]:
    seen: set[str] = set()
    picked: list[str] = []
    for text in walk_text(fields):
        clean = text.strip()
        if not clean or clean in seen:
            continue
        if len(clean) > 240:
            clean = clean[:237] + "..."
        if any(marker in clean.lower() for marker in ("view_file", "list_dir", "tool", "file://")):
            picked.append(clean)
            seen.add(clean)
    return picked
