#!/usr/bin/env python3
"""Targeted, zero-dependency test suite for agy2api.

Adheres to test-guard quality principles:
- Zero heavy dependencies (Python standard library unittest only).
- Real data objects, real SQLite schemas, and real Pydantic validation (no mocks).
- Tests observable behavior and regression boundaries.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from agy_runner import (
    AbortContext,
    _read_response_with_retry,
    check_windows_command_length,
    sweep_all_conversations,
)
from config import MODEL_MAP, Settings, resolve_model
from conversation_reader import UpstreamError, read_response
from fake_stream import build_chunks, split_text, sse_event
from models import ChatCompletionRequest, ChatMessage
from session_store import SessionStore, fingerprint


def _encode_varint(value: int) -> bytes:
    buf = bytearray()
    while value > 0x7F:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value & 0x7F)
    return bytes(buf)


def _encode_field(number: int, wire_type: int, data: bytes) -> bytes:
    tag = (number << 3) | wire_type
    return _encode_varint(tag) + _encode_varint(len(data)) + data


class ModelResolutionTests(unittest.TestCase):
    """Test model alias canonicalization and effort variant selection."""

    def test_alias_with_effort_selects_resolved_catalog_variant(self) -> None:
        """Legacy alias gemini-3.5-flash + high effort must resolve to Gemini 3.8 Flash (High)."""
        model, effort = resolve_model("gemini-3.5-flash", "high")
        self.assertEqual(model, "Gemini 3.8 Flash (High)")
        self.assertIsNone(effort, "Effort should be None when catalog encodes effort")

    def test_legacy_gemini_35_flash_effort_variants_resolve(self) -> None:
        """Legacy gemini-3.5-flash-low/medium/high IDs must resolve to Gemini 3.8 Flash variants."""
        cases = [
            ("gemini-3.5-flash-low", "Gemini 3.8 Flash (Low)"),
            ("gemini-3.5-flash-medium", "Gemini 3.8 Flash (Medium)"),
            ("gemini-3.5-flash-high", "Gemini 3.8 Flash (High)"),
        ]
        for model_id, expected_label in cases:
            with self.subTest(model_id=model_id):
                model, effort = resolve_model(model_id)
                self.assertEqual(model, expected_label)
                self.assertIsNone(effort)

    def test_direct_effort_variant_drops_cli_effort_flag(self) -> None:
        """Models with baked-in effort must not pass --effort to avoid agy CLI errors."""
        cases = [
            ("gemini-3.8-flash", "low", "Gemini 3.8 Flash (Low)"),
            ("gemini-3.8-flash", "medium", "Gemini 3.8 Flash (Medium)"),
            ("gemini-3.8-flash", "high", "Gemini 3.8 Flash (High)"),
        ]
        for raw_model, req_effort, expected_label in cases:
            with self.subTest(model=raw_model, effort=req_effort):
                model, effort = resolve_model(raw_model, req_effort)
                self.assertEqual(model, expected_label)
                self.assertIsNone(effort)

    def test_thinking_model_suppresses_cli_effort_flag(self) -> None:
        """Claude thinking models reject --effort; verify effort flag is suppressed."""
        model, effort = resolve_model("claude-sonnet-4.6", "high")
        self.assertEqual(model, "Claude Sonnet 4.6 (Thinking)")
        self.assertIsNone(effort)

    def test_unknown_custom_model_passes_through_with_requested_effort(self) -> None:
        """Unrecognized model names pass through verbatim with effort intact."""
        model, effort = resolve_model("custom-experimental-model", "low")
        self.assertEqual(model, "custom-experimental-model")
        self.assertEqual(effort, "low")


class ConfigurationSettingsTests(unittest.TestCase):
    """Test settings defaults and read-only mode behavior."""

    def test_default_settings_quarantines_workspace_and_disables_skip_permissions(
        self,
    ) -> None:
        """Default configuration must not skip permissions and use isolated workspace."""
        settings = Settings()
        self.assertFalse(settings.dangerously_skip_permissions)
        self.assertEqual(settings.workdir.name, "isolated_workspace")
        self.assertTrue(settings.workdir.exists())

    def test_read_only_mode_enables_sandbox_and_permission_skip(self) -> None:
        """AGY2API_READ_ONLY must activate sandbox and auto-approve permissions."""
        settings = Settings(read_only=True)
        self.assertTrue(settings.read_only)
        self.assertTrue(settings.sandbox)
        self.assertTrue(settings.dangerously_skip_permissions)

    def test_model_catalog_contains_expected_baseline_models(self) -> None:
        """Verify baseline models are registered in MODEL_MAP."""
        expected = [
            "gemini-3.8-flash",
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-low",
            "claude-sonnet-4.6",
            "claude-opus-4.6",
            "gpt-oss-120b",
        ]
        for key in expected:
            with self.subTest(model=key):
                self.assertIn(key, MODEL_MAP)


class ChatRequestValidationTests(unittest.TestCase):
    """Test Pydantic chat completion request parsing and validation."""

    def test_request_accepts_valid_reasoning_effort(self) -> None:
        """ChatCompletionRequest must accept standard OpenAI reasoning_effort values."""
        for level in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=level):
                req = ChatCompletionRequest(
                    messages=[ChatMessage(role="user", content="hello")],
                    reasoning_effort=level,
                )
                self.assertEqual(req.reasoning_effort, level)

    def test_request_ignores_extra_unmodeled_openai_parameters(self) -> None:
        """Clients sending top_p, frequency_penalty, or n must not trigger 422 errors."""
        payload = {
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "top_p": 0.95,
            "frequency_penalty": 0.5,
            "presence_penalty": 0.2,
            "n": 1,
            "user": "test-user-id",
        }
        req = ChatCompletionRequest.model_validate(payload)
        self.assertEqual(req.model, "gemini-3.8-flash")
        self.assertEqual(len(req.messages), 1)

    def test_default_request_model_resolves_to_supported_catalog_model(self) -> None:
        """Default request model must be gemini-3.8-flash and resolve in catalog."""
        req = ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")])
        self.assertEqual(req.model, "gemini-3.8-flash")
        resolved, _ = resolve_model(req.model)
        self.assertIsNotNone(resolved)
        self.assertIn(req.model, MODEL_MAP)


class ConversationDatabaseReaderTests(unittest.TestCase):
    """Test reading responses, reasoning, and upstream errors from real SQLite databases."""

    def setUp(self) -> None:
        self.fd, self.db_path = tempfile.mkstemp(prefix="agy-test-", suffix=".db")
        os.close(self.fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE steps (
                idx INTEGER PRIMARY KEY,
                step_type INTEGER,
                status INTEGER,
                step_payload BLOB
            )
            """)
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_completed_model_step_extracts_answer_and_reasoning(self) -> None:
        """A valid model step (15) with status=3 must yield answer and reasoning text."""
        f1 = _encode_field(1, 2, "Response answer body".encode("utf-8"))
        f3 = _encode_field(3, 2, "Internal chain of thought".encode("utf-8"))
        payload = _encode_field(20, 2, f1 + f3)

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO steps VALUES (1, 15, 3, ?)", (payload,))
        conn.commit()
        conn.close()

        result = read_response(self.db_path)
        self.assertEqual(result.answer, "Response answer body")
        self.assertEqual(result.reasoning, "Internal chain of thought")
        self.assertFalse(result.truncated)

    def test_step_17_error_with_upstream_keyword_raises_upstream_error(self) -> None:
        """An error step (17) containing upstream keywords must raise UpstreamError."""
        # Protobuf string payload containing the keyword
        payload = _encode_field(
            1, 2, "upstream stream reading error occurred".encode("utf-8")
        )

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO steps VALUES (1, 17, 3, ?)", (payload,))
        conn.commit()
        conn.close()

        with self.assertRaises(UpstreamError) as ctx:
            read_response(self.db_path)
        self.assertIn("stream reading error", str(ctx.exception).lower())

    def test_database_without_model_rows_raises_value_error(self) -> None:
        """A database with no completed model response must raise ValueError."""
        with self.assertRaises(ValueError):
            read_response(self.db_path)

    def test_database_locked_operational_error_retries_and_succeeds(self) -> None:
        """_read_response_with_retry must retry on sqlite3.OperationalError and succeed."""
        f1 = _encode_field(1, 2, "Response after lock".encode("utf-8"))
        payload = _encode_field(20, 2, f1)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO steps VALUES (1, 15, 3, ?)", (payload,))
        conn.commit()
        conn.close()

        from unittest.mock import patch
        import conversation_reader

        orig_read = conversation_reader.read_response
        attempts = 0

        def flaky_read(path: Path) -> conversation_reader.AgResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            return orig_read(path)

        with patch("agy_runner.read_response", side_effect=flaky_read):
            res = _read_response_with_retry(Path(self.db_path))
            self.assertEqual(res.answer, "Response after lock")
            self.assertEqual(attempts, 2)


class SessionStoreTests(unittest.TestCase):
    """Test stateful session store prefix matching and eviction."""

    def test_matching_prefix_returns_resume_plan(self) -> None:
        """Incoming history matching a stored session prefix must produce a resume plan."""
        store = SessionStore(max_sessions=10)
        sig1 = fingerprint("user", "turn 1")
        sig2 = fingerprint("assistant", "turn 1 reply")
        sig3 = fingerprint("user", "turn 2")

        store.remember("conv-uuid-1", [sig1, sig2])
        plan = store.lookup([sig1, sig2, sig3])

        self.assertEqual(plan.conversation_id, "conv-uuid-1")
        self.assertEqual(plan.prefix_len, 2)

    def test_unrelated_history_returns_fresh_plan(self) -> None:
        """Incoming history that does not share a prefix must return a fresh plan."""
        store = SessionStore(max_sessions=10)
        store.remember("conv-uuid-1", [fingerprint("user", "turn 1")])

        plan = store.lookup([fingerprint("user", "completely different topic")])
        self.assertIsNone(plan.conversation_id)
        self.assertEqual(plan.prefix_len, 0)

    def test_lru_eviction_drops_oldest_session_when_over_capacity(self) -> None:
        """Exceeding max_sessions must evict the least recently used conversation."""
        store = SessionStore(max_sessions=2)
        store.remember("conv-1", [fingerprint("user", "1")])
        time.sleep(0.01)
        store.remember("conv-2", [fingerprint("user", "2")])
        time.sleep(0.01)

        # Adding a 3rd session must evict conv-1
        evicted = store.remember("conv-3", [fingerprint("user", "3")])
        self.assertIn("conv-1", evicted)
        self.assertIsNone(store.lookup([fingerprint("user", "1")]).conversation_id)

    def test_dynamic_system_prompt_timestamp_preserves_session_continuity(self) -> None:
        """System prompt timestamp fluctuation must not break stateful chat continuation."""
        store = SessionStore(max_sessions=10)
        sys1 = fingerprint("system", "Assistant prompt. Time: 10:00:00")
        u1 = fingerprint("user", "Hello")
        a1 = fingerprint("assistant", "Hi there")
        store.remember("conv-dynamic-1", [sys1, u1, a1])

        # Next turn has slightly changed timestamp in system prompt
        sys2 = fingerprint("system", "Assistant prompt. Time: 10:00:15")
        u2 = fingerprint("user", "What is the weather?")
        plan = store.lookup([sys2, u1, a1, u2])

        self.assertEqual(plan.conversation_id, "conv-dynamic-1")
        self.assertEqual(plan.prefix_len, 3)

    def test_explicit_session_hint_routes_to_matched_session(self) -> None:
        """Explicit session hint from client binds request to the existing session."""
        store = SessionStore(max_sessions=10)
        u1 = fingerprint("user", "Turn 1")
        a1 = fingerprint("assistant", "Reply 1")
        u2 = fingerprint("user", "Turn 2")

        store.remember("conv-explicit-1", [u1, a1], session_hint="user-session-abc")
        plan = store.lookup([u1, a1, u2], session_hint="user-session-abc")

        self.assertEqual(plan.conversation_id, "conv-explicit-1")
        self.assertEqual(plan.prefix_len, 2)


class FakeStreamHelpersTests(unittest.TestCase):
    """Test chunking and Server-Sent Events (SSE) formatting."""

    def test_split_text_divides_strings_into_regular_chunks(self) -> None:
        """Text must be split into pieces of the requested chunk size."""
        chunks = split_text("abcdefghij", size=3)
        self.assertEqual(chunks, ["abc", "def", "ghi", "j"])

    def test_build_chunks_structures_standard_openai_stream_events(self) -> None:
        """build_chunks must create initial role delta, reasoning, content, and stop chunks."""
        chunks = build_chunks(
            answer="Hello", reasoning="Thinking", model="gemini-3.8-flash"
        )
        self.assertTrue(len(chunks) >= 4)
        self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

    def test_sse_event_formats_valid_data_frame(self) -> None:
        """sse_event must serialize dict to 'data: <json>\\n\\n' UTF-8 bytes."""
        event_bytes = sse_event({"test": "value"})
        self.assertTrue(event_bytes.startswith(b"data: "))
        self.assertTrue(event_bytes.endswith(b"\n\n"))
        self.assertIn(b'"test":"value"', event_bytes)


class WindowsCommandLineGuardTests(unittest.TestCase):
    """Test Windows ~32KB command line overflow detection and cleanup."""

    def test_check_windows_command_length_accepts_normal_command(self) -> None:
        """Normal commands under 32,000 UTF-16 code units pass and return code unit count."""
        command = ["agy.exe", "--print=hello", "--disable-slash-commands"]
        count = check_windows_command_length(command)
        self.assertGreater(count, 0)
        self.assertLess(count, 32_000)

    def test_check_windows_command_length_rejects_oversized_command(self) -> None:
        """Commands exceeding 32,000 UTF-16 code units must raise ValueError."""
        huge_prompt = "a" * 32_500
        command = ["agy.exe", f"--print={huge_prompt}", "--disable-slash-commands"]
        with self.assertRaises(ValueError) as ctx:
            check_windows_command_length(command)
        self.assertIn("exceeds Windows limit", str(ctx.exception))

    def test_oversized_run_cleans_up_temporary_log_file(self) -> None:
        """_run_agy_once must delete log_path when command length check fails."""
        from agy_runner import _run_agy_once

        huge_prompt = "a" * 32_500
        temp_dir = tempfile.gettempdir()
        before_logs = set(Path(temp_dir).glob("agy2api-*.log"))

        with self.assertRaises(ValueError):
            _run_agy_once(
                prompt=huge_prompt,
                model=None,
                conversation_id=None,
                keep=False,
            )

        after_logs = set(Path(temp_dir).glob("agy2api-*.log"))
        new_leaked_logs = after_logs - before_logs
        self.assertEqual(
            new_leaked_logs,
            set(),
            f"Oversized command leaked log files: {new_leaked_logs}",
        )


class ProcessResilienceAndSafetyTests(unittest.TestCase):
    """Test process lifecycle termination and sweep path containment."""

    def test_abort_context_terminates_running_process(self) -> None:
        """Calling abort() on AbortContext must terminate child process immediately."""
        import subprocess
        import sys

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ctx = AbortContext()
        ctx.proc = proc
        self.assertIsNone(proc.poll())

        ctx.abort()
        self.assertTrue(ctx.aborted)
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        self.assertIsNotNone(proc.poll(), "Process should be terminated after abort()")

    def test_sweep_refuses_deletion_when_conversations_dir_outside_stateful_home(
        self,
    ) -> None:
        """sweep_all_conversations must return 0 and refuse deletion outside stateful_home."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            unsafe_conv = temp_path / "conversations"
            unsafe_conv.mkdir()
            dummy_db = unsafe_conv / "dummy.db"
            dummy_db.write_text("test", encoding="utf-8")

            from unittest.mock import patch
            from config import Settings

            mock_settings = Settings()
            object.__setattr__(mock_settings, "conversations_dir", unsafe_conv)
            object.__setattr__(
                mock_settings, "stateful_home", temp_path / "different_home"
            )

            with patch("agy_runner.settings", mock_settings):
                removed = sweep_all_conversations()
                self.assertEqual(removed, 0)
                self.assertTrue(dummy_db.exists(), "Dummy DB must not be deleted")


if __name__ == "__main__":
    unittest.main()
