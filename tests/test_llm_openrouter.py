"""Offline robustness tests for `OpenRouterClient.complete_json` (§5.3).

No network: `_create` is stubbed to yield canned response bodies so the parse /
retry / graceful-degrade behavior can be exercised deterministically. The key
contract these lock in: a single empty or unparseable model reply must degrade
THIS call to `{}` (callers handle that — no issues / default clarity / no
questions) and never raise, or one bad reply would crash the whole poller — and
an empty reply (reasoning models occasionally return empty `content`) is retried
once before giving up.

Stdlib `unittest` only.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from gchat_agent.config import Config
from gchat_agent.llm.openrouter import OpenRouterClient


def _resp(content: str) -> SimpleNamespace:
    """A minimal stand-in for an OpenAI chat-completions response object."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class _StubClient(OpenRouterClient):
    """`OpenRouterClient` with `_create` overridden to return canned bodies in
    order (then "" once exhausted), so `complete_json` runs fully offline."""

    def __init__(self, contents: list[str]) -> None:
        super().__init__(Config())
        self._contents = list(contents)
        self.calls = 0

    def _create(self, messages, **kwargs):  # type: ignore[override]  # noqa: ARG002
        self.calls += 1
        body = self._contents.pop(0) if self._contents else ""
        return _resp(body)


class CompleteJsonRobustnessTest(unittest.TestCase):
    """`complete_json` degrades gracefully and retries an empty reply once."""

    def test_empty_twice_degrades_to_empty_dict(self) -> None:
        c = _StubClient(["", ""])
        self.assertEqual(c.complete_json("s", "u"), {})
        self.assertEqual(c.calls, 2)  # retried once on the empty content

    def test_empty_then_valid_retries_and_parses(self) -> None:
        c = _StubClient(["", '{"questions": ["q1"]}'])
        self.assertEqual(c.complete_json("s", "u"), {"questions": ["q1"]})
        self.assertEqual(c.calls, 2)

    def test_fenced_json_parses(self) -> None:
        c = _StubClient(["```json\n{\"a\": 1}\n```"])
        self.assertEqual(c.complete_json("s", "u"), {"a": 1})

    def test_prose_garbage_degrades_to_empty_dict(self) -> None:
        c = _StubClient(["I cannot help with that."])
        self.assertEqual(c.complete_json("s", "u"), {})
        self.assertEqual(c.calls, 1)  # non-empty (just unparseable) — no retry


if __name__ == "__main__":
    unittest.main()
