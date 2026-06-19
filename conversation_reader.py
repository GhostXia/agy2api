"""Read Antigravity conversation SQLite files and extract model responses."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from proto_decoder import ProtoField, decode_message, find_fields, first_text, raw_text, walk_text


USER_STEP_TYPE = 14
MODEL_STEP_TYPE = 15
TOOL_STEP_TYPES = {8, 9}
DONE_STATUS = 3


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
    model_rows = [
        (idx, payload)
        for idx, step_type, status, payload in rows
        if step_type == MODEL_STEP_TYPE and status == DONE_STATUS and isinstance(payload, bytes)
    ]
    if model_rows:
        _, payload = model_rows[-1]
        answer, reasoning = _extract_model_response(payload)
        if not answer and not reasoning:
            raise ValueError(f"model response payload did not contain answer fields in {path}")
        return AgResponse(
            answer=answer,
            reasoning=reasoning,
            tool_summaries=tuple(tool_summaries),
            db_path=str(path),
        )

    # Degraded read: agy may have been killed mid-generation (e.g. Windows
    # external kill, agy-side timeout).  A status=2 (in-progress) row can
    # contain partial text that is still better than a blank error response.
    incomplete_rows = [
        (idx, payload)
        for idx, step_type, status, payload in rows
        if step_type == MODEL_STEP_TYPE and isinstance(payload, bytes)
    ]
    if incomplete_rows:
        _, payload = incomplete_rows[-1]
        answer, reasoning = _extract_model_response(payload)
        if answer or reasoning:
            return AgResponse(
                answer=answer,
                reasoning=reasoning,
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
