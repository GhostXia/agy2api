"""Map the stateless OpenAI chat protocol onto stateful agy conversations.

SillyTavern (and any OpenAI client) resends the FULL message history every
request. Sending all of it to agy each turn makes every prompt huge and slow,
which is exactly what trips the upstream ~connection-timeout on long chats.

This store recognises a continuing chat by matching the incoming message list
against histories we have already forwarded: if the stored history is a prefix
of the incoming one, the chat is a continuation and only the *new* turn needs to
be sent to the existing agy conversation (via `--conversation <id>`). agy keeps
the rest in its own memory.

Experimental, opt-in (AGY2API_STATEFUL). Pure in-memory; not persisted.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


def fingerprint(role: str, text: str) -> str:
    return hashlib.sha1(f"{role}\n{text}".encode("utf-8", "replace")).hexdigest()


@dataclass
class _Session:
    conversation_id: str
    sigs: list[str]
    last_used: float = field(default_factory=time.time)


@dataclass(frozen=True)
class Plan:
    """How to handle a request: resume an existing conversation and send only
    `new_indices` messages, or (conversation_id=None) start fresh with all."""

    conversation_id: str | None
    prefix_len: int  # how many leading messages agy already has


class SessionStore:
    def __init__(self, max_sessions: int = 200) -> None:
        self._sessions: dict[str, _Session] = {}
        self._max = max_sessions

    def lookup(self, sigs: list[str]) -> Plan:
        """Find the session whose stored sigs are the LONGEST prefix of `sigs`.
        Returns a fresh Plan (conversation_id=None) if none qualifies."""
        best: _Session | None = None
        for sess in self._sessions.values():
            n = len(sess.sigs)
            if n < len(sigs) and sigs[:n] == sess.sigs:
                if best is None or n > len(best.sigs):
                    best = sess
        if best is None:
            return Plan(conversation_id=None, prefix_len=0)
        best.last_used = time.time()
        return Plan(conversation_id=best.conversation_id, prefix_len=len(best.sigs))

    def remember(self, conversation_id: str, sigs: list[str]) -> list[str]:
        """Record the full message history now covered by `conversation_id`.
        Returns conversation_ids evicted (caller should delete their DBs)."""
        self._sessions[conversation_id] = _Session(conversation_id, list(sigs))
        return self._evict()

    def forget(self, conversation_id: str) -> None:
        self._sessions.pop(conversation_id, None)

    def _evict(self) -> list[str]:
        evicted: list[str] = []
        while len(self._sessions) > self._max:
            oldest = min(self._sessions.values(), key=lambda s: s.last_used)
            self._sessions.pop(oldest.conversation_id, None)
            evicted.append(oldest.conversation_id)
        return evicted
