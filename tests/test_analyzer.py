"""Offline tests for the retrieval-augmented `Analyzer` (§12 + §3 + §6).

These exercise the three bot LLM tasks end-to-end against `MockLLM` with no
network and no real retriever:

- `detect_issues` over a fixed incident transcript mints a fully-formed OPEN
  `Issue` with a fingerprint, real in-transcript source ids, and `root` = the
  earliest cited message (§6);
- `assess_clarity` reports "not clear" with `missing_info` while owner/deadline
  are absent, then flips to `is_clear=True` (confidence 0.9) once a follow-up
  supplies an owner + a date + a number (the `MockLLM` clarity rule);
- `generate_questions` returns >=1 targeted question;
- the §3 direct-context bypass: with `retriever=None` the analyzer never attempts
  retrieval (asserted with a spy retriever that records every call).

Stdlib `unittest` only; deterministic.
"""
from __future__ import annotations

import unittest

from gchat_agent.agent.analyzer import Analyzer
from gchat_agent.llm.mock import MockLLM
from gchat_agent.models import (
    Conversation,
    Message,
    SenderType,
    Status,
)
from gchat_agent.rag.base import Passage

_SPACE = "spaces/S"
_THREAD = f"{_SPACE}/threads/t1"


def _msg(seq: int, sender: str, text: str, thread: str = _THREAD) -> Message:
    """A deterministic `Message` with a sortable RFC-3339 `create_time`."""
    return Message(
        id=f"{_SPACE}/messages/m{seq}",
        space=_SPACE,
        thread_id=thread,
        sender=sender,
        sender_type=SenderType.HUMAN,
        text=text,
        create_time=f"2026-06-13T09:0{seq}:00Z",
    )


class _SpyRetriever:
    """A `Retriever` that records each `retrieve` call so a test can assert it
    was (or was never) invoked. Returns a single canned passage."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def retrieve(self, query: str, k: int) -> list[Passage]:
        self.calls.append((query, k))
        return [
            Passage(
                text="Runbook: payments gateway restart procedure.",
                source="runbook.md",
                section="Payments",
                kind="kb",
                create_time="",
                score=1.0,
            )
        ]


def _incident_conversation() -> Conversation:
    """A fixed 2-message incident transcript with `#ids`, no owner/date/number."""
    return Conversation(
        messages=[
            _msg(1, "users/staff-1", "Payments gateway is down, checkout failing for everyone"),
            _msg(2, "users/staff-2", "Confirmed, deposits broken too and this is urgent"),
        ]
    )


class DetectIssuesTest(unittest.TestCase):
    """detect_issues over a fixed incident transcript (§12 + §6)."""

    def setUp(self) -> None:
        self.conv = _incident_conversation()
        self.analyzer = Analyzer(MockLLM(), retriever=None)

    def test_detects_open_issue_with_fingerprint_and_real_sources(self) -> None:
        issues = self.analyzer.detect_issues(self.conv)
        self.assertGreaterEqual(len(issues), 1)

        issue = issues[0]
        # OPEN status + a non-empty fingerprint that equals the id (§6).
        self.assertEqual(issue.status, Status.OPEN)
        self.assertTrue(issue.fingerprint)
        self.assertEqual(issue.id, issue.fingerprint)

        # Every cited source id is a real message present in the transcript.
        present = {m.id for m in self.conv.messages}
        self.assertTrue(issue.source_message_ids)
        for sid in issue.source_message_ids:
            self.assertIn(sid, present)

        # Root is the earliest cited source id (transcript order).
        order = {m.id: idx for idx, m in enumerate(self.conv.messages)}
        self.assertEqual(
            issue.root_message_id,
            min(issue.source_message_ids, key=lambda i: order[i]),
        )
        self.assertEqual(issue.root_message_id, self.conv.messages[0].id)

        # Thread is anchored to the root message's thread; timestamp is set.
        self.assertEqual(issue.thread_id, _THREAD)
        self.assertTrue(issue.created_at)

    def test_no_retrieval_attempted_when_retriever_none(self) -> None:
        # §3 direct-context bypass: a spy on a separate analyzer proves that the
        # retriever=None analyzer cannot have called any retriever.
        spy = _SpyRetriever()
        with_retriever = Analyzer(MockLLM(), retriever=spy)
        with_retriever.detect_issues(self.conv)
        self.assertEqual(len(spy.calls), 1)  # control: retrieval happens when present

        # The retriever=None analyzer has no retriever object to call at all.
        self.assertIsNone(self.analyzer.retriever)
        # _retrieve_context short-circuits to "" without touching any retriever.
        self.assertEqual(self.analyzer._retrieve_context("anything"), "")


class _HashCitingLLM:
    """An `LLMClient` stub whose detection cites `source_message_ids` WITH the
    transcript's leading `#` marker (as glm-5.1 does), to prove the analyzer
    strips it instead of dropping the whole issue."""

    def complete_json(self, system, user, schema_hint=None):  # noqa: ARG002
        return {
            "issues": [
                {
                    "title": "Gateway down",
                    "summary": "Payments gateway failing",
                    "category": "incident",
                    "severity": "high",
                    "source_message_ids": ["#spaces/S/messages/m1", " #spaces/S/messages/m2"],
                    "missing_info": ["owner"],
                }
            ]
        }

    def chat(self, system, messages):  # noqa: ARG002
        return ""


class _ShortCitingLLM:
    """An `LLMClient` stub whose detection cites only the trailing id segment
    ("m1" instead of "spaces/S/messages/m1"), as minimax-m3 does."""

    def complete_json(self, system, user, schema_hint=None):  # noqa: ARG002
        return {
            "issues": [
                {
                    "title": "Gateway down",
                    "summary": "Payments gateway failing",
                    "category": "incident",
                    "severity": "high",
                    "source_message_ids": ["m1", "m2"],
                    "missing_info": ["owner"],
                }
            ]
        }

    def chat(self, system, messages):  # noqa: ARG002
        return ""


class CitedIdMarkerTest(unittest.TestCase):
    """Models cite source ids inconsistently (full id, `#<id>` marker, or just the
    trailing segment). Every form must resolve to the real id — not get the whole
    issue silently dropped (glm / minimax regression)."""

    def test_strip_id_marker_normalizes(self) -> None:
        self.assertEqual(Analyzer._strip_id_marker("#abc"), "abc")
        self.assertEqual(Analyzer._strip_id_marker("  #abc "), "abc")
        self.assertEqual(Analyzer._strip_id_marker("abc"), "abc")  # no marker, untouched

    def test_resolve_cited_id_forms(self) -> None:
        present = ["spaces/S/messages/m1", "spaces/S/messages/m2"]
        self.assertEqual(  # exact
            Analyzer._resolve_cited_id("spaces/S/messages/m1", present),
            "spaces/S/messages/m1",
        )
        self.assertEqual(  # '#'-prefixed full id
            Analyzer._resolve_cited_id("#spaces/S/messages/m2", present),
            "spaces/S/messages/m2",
        )
        self.assertEqual(  # trailing segment only
            Analyzer._resolve_cited_id("m1", present), "spaces/S/messages/m1"
        )
        self.assertIsNone(Analyzer._resolve_cited_id("nope", present))
        self.assertIsNone(Analyzer._resolve_cited_id("#", present))

    def _assert_two_real_sources(self, llm) -> None:
        conv = _incident_conversation()
        issues = Analyzer(llm, retriever=None).detect_issues(conv)
        self.assertEqual(len(issues), 1)
        present = {m.id for m in conv.messages}
        self.assertTrue(issues[0].source_message_ids)
        for sid in issues[0].source_message_ids:
            self.assertIn(sid, present)  # stored as the real, full id
        self.assertEqual(issues[0].root_message_id, conv.messages[0].id)

    def test_hash_prefixed_citations_still_resolve(self) -> None:
        self._assert_two_real_sources(_HashCitingLLM())  # glm form

    def test_short_segment_citations_still_resolve(self) -> None:
        self._assert_two_real_sources(_ShortCitingLLM())  # minimax form


class ClarityFlowTest(unittest.TestCase):
    """assess_clarity / generate_questions flow (§6) with MockLLM + retriever=None."""

    def setUp(self) -> None:
        self.conv = _incident_conversation()
        self.analyzer = Analyzer(MockLLM(), retriever=None)
        self.issue = self.analyzer.detect_issues(self.conv)[0]

    def test_initially_not_clear_then_questions_then_clear(self) -> None:
        # Initially under-specified: not clear, with missing_info to chase.
        first = self.analyzer.assess_clarity(self.issue, self.conv)
        self.assertFalse(first.is_clear)
        self.assertTrue(first.missing_info)

        # generate_questions yields at least one targeted question.
        questions = self.analyzer.generate_questions(
            self.issue, self.conv, first.missing_info
        )
        self.assertGreaterEqual(len(questions), 1)
        self.assertTrue(all(q.strip() for q in questions))

        # A follow-up supplying owner + date + number flips clarity to clear.
        self.conv.add(
            _msg(
                3,
                "users/ops",
                "I'll take this, target tomorrow EOD, scaling to 4 nodes",
            )
        )
        second = self.analyzer.assess_clarity(self.issue, self.conv)
        self.assertTrue(second.is_clear)
        self.assertEqual(second.confidence, 0.9)
        self.assertEqual(second.missing_info, [])

    def test_clarity_scoped_to_issue_thread(self) -> None:
        # Noise in a *different* thread must not satisfy the clarity rule for
        # this issue (assess_clarity scopes the transcript to the issue's thread).
        self.conv.add(
            _msg(
                3,
                "users/ops",
                "I'll own the marketing task, due tomorrow, 5 banners",
                thread=f"{_SPACE}/threads/other",
            )
        )
        assessment = self.analyzer.assess_clarity(self.issue, self.conv)
        self.assertFalse(assessment.is_clear)
        self.assertTrue(assessment.missing_info)


if __name__ == "__main__":
    unittest.main()
