"""Tests for the text-to-speech client + `build_tts` factory (voice reports).

Fully offline: `MockTTS` needs nothing, and `OpenRouterTTS` is exercised with a
hand-rolled fake OpenAI client injected in place of the lazy `openai` import, so
no network / key / `openai` package is touched.
"""
from __future__ import annotations

import types
import unittest
from dataclasses import replace
from unittest import mock

from gchat_agent.config import load_config
from gchat_agent.llm import tts as tts_mod
from gchat_agent.llm.tts import MockTTS, OpenRouterTTS, build_tts


def _cfg(**over):
    return replace(load_config(env_file="no-such.env"), **over)


class _FakeStreaming:
    """Stand-in for `with_streaming_response.create(...)`'s context manager."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __enter__(self) -> "_FakeStreaming":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def iter_bytes(self):
        yield from self._chunks


class _FakeCreate:
    """Records call kwargs; fails `fail_times` (transiently) before succeeding."""

    def __init__(self, captured: list[dict], chunks: list[bytes], fail_times: int = 0,
                 fatal: bool = False) -> None:
        self.captured = captured
        self.chunks = chunks
        self.fail_times = fail_times
        self.fatal = fatal
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        self.captured.append(kwargs)
        if self.fatal:
            raise RuntimeError("boom: bad request")
        if self.calls <= self.fail_times:
            raise RuntimeError("429 rate limit exceeded")
        return _FakeStreaming(self.chunks)


def _fake_client(create: _FakeCreate):
    return types.SimpleNamespace(
        audio=types.SimpleNamespace(
            speech=types.SimpleNamespace(
                with_streaming_response=types.SimpleNamespace(create=create)
            )
        )
    )


class MockTTSTest(unittest.TestCase):
    def test_synthesize_returns_marked_bytes(self) -> None:
        out = MockTTS().synthesize("Hello there")
        self.assertTrue(out.startswith(tts_mod._MOCK_PREFIX))
        self.assertIn(b"Hello there", out)

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(MockTTS().synthesize("   "), b"")


class BuildTTSTest(unittest.TestCase):
    def test_disk_delivery_needs_no_tts(self) -> None:
        self.assertIsNone(build_tts(_cfg(REPORT_DELIVERY="disk")))

    def test_mock_provider_voice(self) -> None:
        for mode in ("voice", "both"):
            tts = build_tts(_cfg(REPORT_DELIVERY=mode, LLM_PROVIDER="mock"))
            self.assertIsInstance(tts, MockTTS)

    def test_openrouter_requires_key(self) -> None:
        with self.assertRaises(RuntimeError):
            build_tts(_cfg(REPORT_DELIVERY="voice", LLM_PROVIDER="openrouter",
                           OPENROUTER_API_KEY=""))

    def test_openrouter_with_key(self) -> None:
        tts = build_tts(_cfg(REPORT_DELIVERY="both", LLM_PROVIDER="openrouter",
                             OPENROUTER_API_KEY="sk-x"))
        self.assertIsInstance(tts, OpenRouterTTS)

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            build_tts(_cfg(REPORT_DELIVERY="voice", LLM_PROVIDER="nope"))


class OpenRouterTTSTest(unittest.TestCase):
    def test_synthesize_streams_into_memory(self) -> None:
        captured: list[dict] = []
        create = _FakeCreate(captured, [b"ID3", b"\x00audio", b"bytes"])
        tts = OpenRouterTTS(_cfg(TTS_MODEL="x-ai/grok-voice-tts-1.0", TTS_VOICE="alloy"))
        tts._client = _fake_client(create)

        out = tts.synthesize("Issue resolved: login is back.")

        self.assertEqual(out, b"ID3\x00audiobytes")
        self.assertEqual(captured[0]["model"], "x-ai/grok-voice-tts-1.0")
        self.assertEqual(captured[0]["voice"], "alloy")
        self.assertEqual(captured[0]["input"], "Issue resolved: login is back.")
        self.assertIn("HTTP-Referer", captured[0]["extra_headers"])

    def test_empty_input_skips_call(self) -> None:
        captured: list[dict] = []
        tts = OpenRouterTTS(_cfg())
        tts._client = _fake_client(_FakeCreate(captured, [b"x"]))
        self.assertEqual(tts.synthesize(""), b"")
        self.assertEqual(captured, [])

    def test_retries_transient_then_succeeds(self) -> None:
        captured: list[dict] = []
        create = _FakeCreate(captured, [b"ok"], fail_times=1)
        tts = OpenRouterTTS(_cfg())
        tts._client = _fake_client(create)
        with mock.patch.object(tts_mod.time, "sleep", return_value=None):
            out = tts.synthesize("hi")
        self.assertEqual(out, b"ok")
        self.assertEqual(create.calls, 2)

    def test_fatal_error_propagates(self) -> None:
        create = _FakeCreate([], [b"x"], fatal=True)
        tts = OpenRouterTTS(_cfg())
        tts._client = _fake_client(create)
        with self.assertRaises(RuntimeError):
            tts.synthesize("hi")


if __name__ == "__main__":
    unittest.main()
