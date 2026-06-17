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
    Issue,
    Message,
    SenderType,
    Severity,
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


class _CrossThreadCitingLLM:
    """An `LLMClient` stub whose detection lumps a greeting (one top-level thread)
    and the real incident (another top-level thread) into a single issue, citing
    BOTH source messages with the greeting first — the live failure where a model
    groups consecutive top-level messages from one reporter into one issue."""

    def __init__(self, source_ids: list[str]) -> None:
        self._source_ids = source_ids

    def complete_json(self, system, user, schema_hint=None):  # noqa: ARG002
        return {
            "issues": [
                {
                    "title": "Homepage returning 404",
                    "summary": "Homepage is down with a 404",
                    "category": "incident",
                    "severity": "high",
                    "source_message_ids": list(self._source_ids),
                    "missing_info": ["scope"],
                }
            ]
        }

    def chat(self, system, messages):  # noqa: ARG002
        return ""


class CrossThreadAnchorTest(unittest.TestCase):
    """An issue whose cited sources span several top-level threads must anchor to
    the thread that actually holds the report — never a stray greeting in another
    thread. Otherwise every clarifying reply lands in the wrong thread (the
    screenshot bug: 404 questions posted under a "hi" greeting)."""

    _GREET = f"{_SPACE}/threads/greet"
    _INCIDENT = f"{_SPACE}/threads/incident"
    _M1 = f"{_SPACE}/messages/m1"  # greeting (thread greet), earliest
    _M2 = f"{_SPACE}/messages/m2"  # the real report (thread incident), latest

    def _conv(self) -> Conversation:
        return Conversation(messages=[
            _msg(1, "users/ty", "hi, anyone around?", thread=self._GREET),
            _msg(2, "users/ty", "our homepage is 404 now", thread=self._INCIDENT),
        ])

    def test_anchors_to_incident_thread_not_greeting(self) -> None:
        conv = self._conv()
        # Greeting cited FIRST (earliest in transcript) — the old "root = earliest
        # cited" rule would have anchored the whole issue to the greeting thread.
        issue = Analyzer(
            _CrossThreadCitingLLM([self._M1, self._M2]), retriever=None
        ).detect_issues(conv)[0]

        self.assertEqual(issue.thread_id, self._INCIDENT)
        self.assertEqual(issue.root_message_id, self._M2)
        # The cross-thread greeting is dropped so root/thread/evidence stay coherent.
        self.assertEqual(issue.source_message_ids, [self._M2])
        self.assertNotIn(self._M1, issue.source_message_ids)
        # Reporter is the author of the real report (here the same user, but it is
        # resolved from the anchor root, not the earliest cited message).
        self.assertEqual(issue.reporter_id, "users/ty")

    def test_followup_reply_does_not_steal_the_anchor(self) -> None:
        # The mirror case: the real report comes FIRST, and a later out-of-thread
        # follow-up reply (here carrying an issue-signal word) is lumped in. The
        # title matches the report, so the issue stays anchored to the report's
        # thread — a recency rule would wrongly anchor to the trailing reply.
        conv = Conversation(messages=[
            _msg(1, "users/ty", "our homepage is 404 now", thread=self._INCIDENT),
            _msg(2, "users/ty", "here's some context on the outage", thread=self._GREET),
        ])
        issue = Analyzer(
            _CrossThreadCitingLLM([self._M1, self._M2]), retriever=None
        ).detect_issues(conv)[0]
        self.assertEqual(issue.thread_id, self._INCIDENT)
        self.assertEqual(issue.root_message_id, self._M1)
        self.assertNotIn(self._M2, issue.source_message_ids)

    def test_anchor_thread_helper(self) -> None:
        by_id = {m.id: m for m in self._conv().messages}
        # Cross-thread: anchor to the thread the title/summary is about (incident).
        self.assertEqual(
            Analyzer._anchor_thread(
                [self._M1, self._M2], by_id, "Homepage returning 404"
            ),
            self._INCIDENT,
        )
        # No overlap with either thread → fall back to the earliest cited thread.
        self.assertEqual(
            Analyzer._anchor_thread([self._M1, self._M2], by_id, "unrelated words"),
            self._GREET,
        )
        # Single thread → that thread.
        self.assertEqual(
            Analyzer._anchor_thread([self._M1], by_id, "anything"), self._GREET
        )


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


class _DedupLLM:
    """An `LLMClient` stub for `match_duplicate_issue`: returns a preset
    `duplicate_of` value and counts how many times it was called."""

    def __init__(self, duplicate_of: object) -> None:
        self.duplicate_of = duplicate_of
        self.calls = 0

    def complete_json(self, system, user, schema_hint=None):  # noqa: ARG002
        self.calls += 1
        return {"duplicate_of": self.duplicate_of}

    def chat(self, system, messages):  # noqa: ARG002
        return ""


def _bare_issue(title: str, summary: str = "", category: str = "incident") -> Issue:
    return Issue(
        id=title,
        fingerprint=title,
        title=title,
        summary=summary,
        category=category,
        severity=Severity.MEDIUM,
        status=Status.OPEN,
        thread_id=f"{_SPACE}/threads/{abs(hash(title)) % 97}",
        root_message_id=f"{_SPACE}/messages/{abs(hash(title)) % 97}",
    )


class MatchDuplicateIssueTest(unittest.TestCase):
    """The LLM cross-thread duplicate decider (§6): map the model's 1-based pick
    back to the open issue, or None for null / out-of-range / errors."""

    def setUp(self) -> None:
        self.candidate = _bare_issue("API gateway 504 timeouts in production")
        self.open_issues = [
            _bare_issue("Payouts stuck for VIP players"),
            _bare_issue("API gateway timing out in production with 504 errors"),
        ]

    def test_returns_the_chosen_open_issue(self) -> None:
        analyzer = Analyzer(_DedupLLM(2), retriever=None)
        match = analyzer.match_duplicate_issue(self.candidate, self.open_issues)
        self.assertIs(match, self.open_issues[1])

    def test_null_means_no_match(self) -> None:
        analyzer = Analyzer(_DedupLLM(None), retriever=None)
        self.assertIsNone(
            analyzer.match_duplicate_issue(self.candidate, self.open_issues)
        )

    def test_out_of_range_index_is_ignored(self) -> None:
        analyzer = Analyzer(_DedupLLM(99), retriever=None)
        self.assertIsNone(
            analyzer.match_duplicate_issue(self.candidate, self.open_issues)
        )

    def test_string_index_is_coerced(self) -> None:
        analyzer = Analyzer(_DedupLLM("1"), retriever=None)
        self.assertIs(
            analyzer.match_duplicate_issue(self.candidate, self.open_issues),
            self.open_issues[0],
        )

    def test_empty_open_list_skips_the_llm(self) -> None:
        llm = _DedupLLM(1)
        analyzer = Analyzer(llm, retriever=None)
        self.assertIsNone(analyzer.match_duplicate_issue(self.candidate, []))
        self.assertEqual(llm.calls, 0)  # no point asking with nothing to match

    def test_transport_error_yields_none(self) -> None:
        class _BoomLLM:
            def complete_json(self, system, user, schema_hint=None):  # noqa: ARG002
                raise RuntimeError("boom")

            def chat(self, system, messages):  # noqa: ARG002
                return ""

        analyzer = Analyzer(_BoomLLM(), retriever=None)
        self.assertIsNone(
            analyzer.match_duplicate_issue(self.candidate, self.open_issues)
        )


if __name__ == "__main__":
    unittest.main()
