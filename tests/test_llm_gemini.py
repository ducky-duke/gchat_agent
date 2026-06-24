"""Offline tests for `GeminiClient` (the live `google-genai` transport, §5.3).

Fully hermetic — no network, no key, and the `google-genai` SDK is never imported:
`_generate` is stubbed to return canned response objects, so the parse / retry /
graceful-degrade behavior and the role/usage plumbing are exercised deterministically.
`GeminiClient.__init__` and the parts under test touch the SDK only inside
`_get_client`/`_build_config`, which the stub bypasses.

Contracts locked in (mirroring the OpenRouter client): a single empty or unparseable
reply degrades THIS call to `{}` (never raises — callers handle that) and an empty
reply is retried once before giving up; usage is accumulated from `usage_metadata`.

Stdlib `unittest` only.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from gchat_agent.config import Config
from gchat_agent.llm.gemini import GeminiClient
from gchat_agent.llm.openrouter import build_llm
from gchat_agent.llm.tts import build_tts


def _resp(text: str, usage: SimpleNamespace | None = None) -> SimpleNamespace:
    """A minimal stand-in for a `generate_content` response object."""
    return SimpleNamespace(text=text, usage_metadata=usage)


def _usage(prompt: int, completion: int, total: int) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=completion,
        total_token_count=total,
    )


class _StubClient(GeminiClient):
    """`GeminiClient` with `_generate` overridden to return canned responses in
    order (then a blank one once exhausted), so the public methods run fully
    offline without importing `google-genai`. Records the args each call saw."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        super().__init__(Config(GEMINI_API_KEY="test-key"))
        self._responses = list(responses)
        self.calls = 0
        self.seen: list[dict] = []

    def _generate(self, *, contents, system, json_mode):  # type: ignore[override]
        self.calls += 1
        self.seen.append({"contents": contents, "system": system, "json_mode": json_mode})
        resp = self._responses.pop(0) if self._responses else _resp("")
        self._record_usage(resp)
        return resp


class CompleteJsonRobustnessTest(unittest.TestCase):
    """`complete_json` degrades gracefully and retries an empty reply once."""

    def test_empty_twice_degrades_to_empty_dict(self) -> None:
        c = _StubClient([_resp(""), _resp("")])
        self.assertEqual(c.complete_json("s", "u"), {})
        self.assertEqual(c.calls, 2)  # retried once on the empty content

    def test_empty_then_valid_retries_and_parses(self) -> None:
        c = _StubClient([_resp(""), _resp('{"questions": ["q1"]}')])
        self.assertEqual(c.complete_json("s", "u"), {"questions": ["q1"]})
        self.assertEqual(c.calls, 2)

    def test_fenced_json_parses(self) -> None:
        c = _StubClient([_resp("```json\n{\"a\": 1}\n```")])
        self.assertEqual(c.complete_json("s", "u"), {"a": 1})

    def test_prose_garbage_degrades_to_empty_dict(self) -> None:
        c = _StubClient([_resp("I cannot help with that.")])
        self.assertEqual(c.complete_json("s", "u"), {})
        self.assertEqual(c.calls, 1)  # non-empty (just unparseable) — no retry

    def test_json_mode_requested_and_schema_hint_appended(self) -> None:
        c = _StubClient([_resp('{"ok": true}')])
        c.complete_json("SYS", "USER", schema_hint="HINT")
        self.assertTrue(c.seen[0]["json_mode"])
        self.assertIn("HINT", c.seen[0]["system"])


class ChatAndContentsTest(unittest.TestCase):
    def test_chat_returns_text_no_json_mode(self) -> None:
        c = _StubClient([_resp("hello back")])
        out = c.chat("be nice", [{"role": "user", "content": "hi"}])
        self.assertEqual(out, "hello back")
        self.assertFalse(c.seen[0]["json_mode"])
        self.assertEqual(c.seen[0]["system"], "be nice")

    def test_to_contents_role_mapping(self) -> None:
        contents = GeminiClient._to_contents([
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "system", "content": "s1"},
        ])
        self.assertEqual([c["role"] for c in contents], ["user", "model", "user"])
        self.assertEqual(contents[0]["parts"][0]["text"], "u1")


class UsageAccountingTest(unittest.TestCase):
    def test_usage_accumulates_from_metadata(self) -> None:
        c = _StubClient([_resp('{"x": 1}', _usage(10, 5, 15))])
        c.complete_json("s", "u")
        snap = c.usage_snapshot()
        self.assertEqual(snap["calls"], 1)
        self.assertEqual(snap["prompt_tokens"], 10)
        self.assertEqual(snap["completion_tokens"], 5)
        self.assertEqual(snap["total_tokens"], 15)

    def test_missing_usage_metadata_is_ignored(self) -> None:
        c = _StubClient([_resp('{"x": 1}', None)])
        c.complete_json("s", "u")
        self.assertEqual(c.usage_snapshot()["calls"], 0)


class BuildFactoryTest(unittest.TestCase):
    def test_build_llm_gemini_requires_key(self) -> None:
        with self.assertRaises(RuntimeError):
            build_llm(Config(LLM_PROVIDER="gemini", GEMINI_API_KEY=""))

    def test_build_llm_gemini_with_key(self) -> None:
        llm = build_llm(Config(LLM_PROVIDER="gemini", GEMINI_API_KEY="k"))
        self.assertIsInstance(llm, GeminiClient)

    def test_build_tts_gemini_returns_none(self) -> None:
        # Voice reports are retired on the gemini provider (Gemini Live call does
        # spoken delivery now) → None, so the runner degrades to the disk report.
        self.assertIsNone(
            build_tts(Config(LLM_PROVIDER="gemini", REPORT_DELIVERY="voice"))
        )


if __name__ == "__main__":
    unittest.main()
