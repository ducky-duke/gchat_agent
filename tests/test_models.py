"""Tests for the domain models (§5.2/§12) — lossless JSON round-trip, enum
coercion, the `Conversation` view helpers, and `issue_fingerprint` stability.

Pure stdlib `unittest`; no network and no LLM/Chat clients are needed since the
models are plain dataclasses.
"""
from __future__ import annotations

import unittest

from gchat_agent.models import (
    AgentState,
    ClarityAssessment,
    Conversation,
    Issue,
    Message,
    QAPair,
    ResolutionReport,
    SenderType,
    Severity,
    Status,
    issue_fingerprint,
)


def _msg(
    mid: str,
    *,
    space: str = "spaces/S",
    thread_id: str = "t1",
    sender: str = "users/staff-1",
    sender_type: SenderType = SenderType.HUMAN,
    text: str = "hello",
    create_time: str = "2026-06-13T00:00:00Z",
) -> Message:
    return Message(
        id=mid,
        space=space,
        thread_id=thread_id,
        sender=sender,
        sender_type=sender_type,
        text=text,
        create_time=create_time,
    )


class MessageRoundTripTest(unittest.TestCase):
    def test_to_dict_serializes_enum_to_string(self) -> None:
        m = _msg("m1", sender_type=SenderType.APP)
        data = m.to_dict()
        # Enum stored as its plain string value, JSON-friendly.
        self.assertEqual(data["sender_type"], "app")
        self.assertIsInstance(data["sender_type"], str)
        self.assertEqual(
            set(data),
            {"id", "space", "thread_id", "sender", "sender_type", "text",
             "create_time", "annotations"},
        )

    def test_round_trip_preserves_all_fields(self) -> None:
        m = _msg("m1", sender_type=SenderType.HUMAN, text="we have an outage")
        again = Message.from_dict(m.to_dict())
        self.assertEqual(again, m)
        self.assertIsInstance(again.sender_type, SenderType)
        self.assertEqual(again.sender_type, SenderType.HUMAN)

    def test_from_dict_rehydrates_enum_from_string(self) -> None:
        again = Message.from_dict(_msg("m1", sender_type=SenderType.APP).to_dict())
        self.assertIs(again.sender_type, SenderType.APP)


class IssueRoundTripTest(unittest.TestCase):
    def _issue(self) -> Issue:
        return Issue(
            id="i1",
            fingerprint="fp-abc",
            title="Payouts stuck",
            summary="Withdrawals are not processing.",
            category="payments",
            severity=Severity.HIGH,
            status=Status.CLARIFYING,
            thread_id="t1",
            root_message_id="m1",
            source_message_ids=["m1", "m2"],
            missing_info=["which provider"],
            questions_asked=["Which payment provider?"],
            qa=[
                QAPair(
                    question="Which payment provider?",
                    answer_message_ids=["m3"],
                    text="Provider X",
                ),
                QAPair(question="Since when?"),
            ],
            last_bot_message_id="m9",
            last_question_at="2026-06-13T01:00:00Z",
            rounds=2,
            idle_cycles=1,
            report_written_at=None,
            created_at="2026-06-13T00:00:00Z",
            updated_at="2026-06-13T01:00:00Z",
        )

    def test_to_dict_coerces_enums_to_strings(self) -> None:
        data = self._issue().to_dict()
        self.assertEqual(data["severity"], "high")
        self.assertEqual(data["status"], "clarifying")
        self.assertIsInstance(data["severity"], str)
        self.assertIsInstance(data["status"], str)
        # Nested QAPairs serialize to plain dicts (asdict already does this).
        self.assertIsInstance(data["qa"], list)
        self.assertEqual(data["qa"][0]["question"], "Which payment provider?")
        self.assertEqual(data["qa"][0]["answer_message_ids"], ["m3"])

    def test_round_trip_preserves_qa_list_and_enums(self) -> None:
        issue = self._issue()
        again = Issue.from_dict(issue.to_dict())
        self.assertEqual(again, issue)
        self.assertIsInstance(again.severity, Severity)
        self.assertIsInstance(again.status, Status)
        # QAPairs come back as QAPair instances, not bare dicts.
        self.assertTrue(all(isinstance(q, QAPair) for q in again.qa))
        self.assertEqual(again.qa[0].text, "Provider X")
        self.assertEqual(again.qa[1].answer_message_ids, [])

    def test_round_trip_preserves_none_optionals(self) -> None:
        issue = self._issue()
        self.assertIsNone(issue.report_written_at)
        again = Issue.from_dict(issue.to_dict())
        self.assertIsNone(again.report_written_at)

    def test_from_dict_applies_defaults_for_missing_optionals(self) -> None:
        minimal = {"id": "i9", "fingerprint": "fp-9"}
        issue = Issue.from_dict(minimal)
        self.assertEqual(issue.id, "i9")
        self.assertEqual(issue.fingerprint, "fp-9")
        # Defaulted enums.
        self.assertIs(issue.severity, Severity.MEDIUM)
        self.assertIs(issue.status, Status.OPEN)
        # Defaulted collections / scalars / None optionals.
        self.assertEqual(issue.source_message_ids, [])
        self.assertEqual(issue.qa, [])
        self.assertEqual(issue.rounds, 0)
        self.assertEqual(issue.idle_cycles, 0)
        self.assertIsNone(issue.last_bot_message_id)
        self.assertIsNone(issue.created_at)

    def test_from_dict_coerces_stringy_counters(self) -> None:
        issue = Issue.from_dict(
            {"id": "i", "fingerprint": "f", "rounds": "3", "idle_cycles": "2"}
        )
        self.assertEqual(issue.rounds, 3)
        self.assertEqual(issue.idle_cycles, 2)


class QAPairRoundTripTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        q = QAPair(question="Why?", answer_message_ids=["a", "b"], text="because")
        again = QAPair.from_dict(q.to_dict())
        self.assertEqual(again, q)

    def test_from_dict_defaults(self) -> None:
        q = QAPair.from_dict({})
        self.assertEqual(q.question, "")
        self.assertEqual(q.answer_message_ids, [])
        self.assertEqual(q.text, "")


class ClarityAssessmentTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        c = ClarityAssessment(
            is_clear=True,
            confidence=0.82,
            missing_info=["scope"],
            rationale="Enough detail.",
        )
        again = ClarityAssessment.from_dict(c.to_dict())
        self.assertEqual(again, c)

    def test_coerces_string_false_to_bool(self) -> None:
        # Raw LLM JSON can emit the *string* "false"; builtin bool() would
        # wrongly read that as True, so from_dict must coerce it.
        c = ClarityAssessment.from_dict(
            {"is_clear": "false", "confidence": 0.5}
        )
        self.assertIs(c.is_clear, False)
        # And string "true" coerces to True.
        c2 = ClarityAssessment.from_dict({"is_clear": "true", "confidence": 0.5})
        self.assertIs(c2.is_clear, True)

    def test_coerces_nonnumeric_confidence_to_float_default(self) -> None:
        # A non-numeric confidence like "high" falls back to the 0.0 default.
        c = ClarityAssessment.from_dict({"is_clear": True, "confidence": "high"})
        self.assertEqual(c.confidence, 0.0)
        self.assertIsInstance(c.confidence, float)
        # A numeric string is parsed.
        c2 = ClarityAssessment.from_dict({"is_clear": True, "confidence": "0.7"})
        self.assertAlmostEqual(c2.confidence, 0.7)

    def test_from_dict_defaults(self) -> None:
        c = ClarityAssessment.from_dict({})
        self.assertIs(c.is_clear, False)
        self.assertEqual(c.confidence, 0.0)
        self.assertEqual(c.missing_info, [])
        self.assertEqual(c.rationale, "")


class ResolutionReportTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        r = ResolutionReport(
            issue_id="i1",
            title="Payouts stuck",
            category="payments",
            severity=Severity.HIGH,
            summary="Withdrawals stuck.",
            resolution="Restarted provider X queue.",
            qa=[QAPair(question="Provider?", answer_message_ids=["m3"], text="X")],
            source_message_ids=["m1", "m2"],
            resolved_at="2026-06-13T02:00:00Z",
        )
        data = r.to_dict()
        self.assertEqual(data["severity"], "high")
        self.assertIsInstance(data["severity"], str)
        again = ResolutionReport.from_dict(data)
        self.assertEqual(again, r)
        self.assertIsInstance(again.severity, Severity)
        self.assertTrue(all(isinstance(q, QAPair) for q in again.qa))

    def test_round_trip_none_resolved_at(self) -> None:
        r = ResolutionReport(
            issue_id="i1",
            title="t",
            category="c",
            severity=Severity.LOW,
            summary="s",
            resolution="r",
        )
        self.assertIsNone(r.resolved_at)
        again = ResolutionReport.from_dict(r.to_dict())
        self.assertIsNone(again.resolved_at)
        self.assertEqual(again.qa, [])
        self.assertEqual(again.source_message_ids, [])


class ConversationRenderTest(unittest.TestCase):
    def _conv(self) -> Conversation:
        return Conversation(
            messages=[
                _msg(
                    "m1",
                    sender="users/staff-1",
                    text="outage",
                    create_time="2026-06-13T00:00:00Z",
                ),
                _msg(
                    "m2",
                    sender="users/bot",
                    text="which region?",
                    create_time="2026-06-13T00:01:00Z",
                ),
            ]
        )

    def test_render_with_ids(self) -> None:
        out = self._conv().render(with_ids=True)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("#m1 "))
        self.assertIn("[2026-06-13T00:00:00Z]", lines[0])
        self.assertIn("users/staff-1: outage", lines[0])

    def test_render_without_ids(self) -> None:
        out = self._conv().render(with_ids=False)
        self.assertNotIn("#m1", out)
        self.assertNotIn("#m2", out)
        self.assertIn("users/staff-1: outage", out)

    def test_render_unknown_sender_and_no_timestamp(self) -> None:
        conv = Conversation(messages=[_msg("m1", sender="", create_time="")])
        out = conv.render(with_ids=True)
        self.assertIn("(unknown):", out)
        self.assertNotIn("[", out)  # no timestamp bracket when create_time empty

    def test_render_empty(self) -> None:
        self.assertEqual(Conversation().render(), "")


class ConversationViewTest(unittest.TestCase):
    def _conv(self) -> Conversation:
        return Conversation(
            messages=[
                _msg("m1", thread_id="t1", sender="users/staff-1"),
                _msg("m2", thread_id="t1", sender="users/bot"),
                _msg("m3", thread_id="t2", sender="users/staff-2"),
                _msg("m4", thread_id="t1", sender="users/staff-1"),
            ]
        )

    def test_tail(self) -> None:
        ids = [m.id for m in self._conv().tail(2).messages]
        self.assertEqual(ids, ["m3", "m4"])

    def test_tail_larger_than_length(self) -> None:
        self.assertEqual(len(self._conv().tail(99).messages), 4)

    def test_tail_zero_or_negative_is_empty(self) -> None:
        self.assertEqual(self._conv().tail(0).messages, [])
        self.assertEqual(self._conv().tail(-3).messages, [])

    def test_for_thread(self) -> None:
        ids = [m.id for m in self._conv().for_thread("t1").messages]
        self.assertEqual(ids, ["m1", "m2", "m4"])

    def test_for_thread_no_match(self) -> None:
        self.assertEqual(self._conv().for_thread("nope").messages, [])

    def test_without_sender(self) -> None:
        ids = [m.id for m in self._conv().without_sender("users/bot").messages]
        self.assertEqual(ids, ["m1", "m3", "m4"])

    def test_after_known_message(self) -> None:
        ids = [m.id for m in self._conv().after("m2").messages]
        self.assertEqual(ids, ["m3", "m4"])

    def test_after_last_message_is_empty(self) -> None:
        self.assertEqual(self._conv().after("m4").messages, [])

    def test_after_unknown_returns_all(self) -> None:
        ids = [m.id for m in self._conv().after("missing").messages]
        self.assertEqual(ids, ["m1", "m2", "m3", "m4"])

    def test_views_return_new_independent_conversation(self) -> None:
        conv = self._conv()
        view = conv.for_thread("t1")
        view.add(_msg("m5"))
        # Mutating the view must not leak back into the source conversation.
        self.assertEqual(len(conv.messages), 4)

    def test_conversation_round_trip(self) -> None:
        conv = self._conv()
        again = Conversation.from_dict(conv.to_dict())
        self.assertEqual([m.id for m in again.messages], ["m1", "m2", "m3", "m4"])
        self.assertTrue(all(isinstance(m, Message) for m in again.messages))
        self.assertEqual(again.messages, conv.messages)


class AgentStateRoundTripTest(unittest.TestCase):
    def _state(self) -> AgentState:
        issue = Issue(
            id="i1",
            fingerprint="fp-1",
            title="t",
            summary="s",
            category="payments",
            severity=Severity.MEDIUM,
            status=Status.OPEN,
            thread_id="t1",
            root_message_id="m1",
            qa=[QAPair(question="q?", answer_message_ids=["m2"], text="a")],
        )
        return AgentState(
            cursor_message_name="spaces/S/messages/m4",
            bot_user_id="users/bot",
            seen_message_ids=["m3", "m4"],
            issues=[issue],
            tombstones=["fp-old"],
        )

    def test_round_trip_preserves_nested_issues_and_cursor(self) -> None:
        state = self._state()
        again = AgentState.from_dict(state.to_dict())
        self.assertEqual(again, state)
        self.assertEqual(again.bot_user_id, "users/bot")
        self.assertEqual(again.cursor_message_name, "spaces/S/messages/m4")
        self.assertEqual(len(again.issues), 1)
        self.assertIsInstance(again.issues[0], Issue)
        self.assertIsInstance(again.issues[0].qa[0], QAPair)
        self.assertEqual(again.tombstones, ["fp-old"])

    def test_to_dict_shape(self) -> None:
        data = self._state().to_dict()
        self.assertEqual(
            set(data),
            {
                "cursor_message_name",
                "bot_user_id",
                "seen_message_ids",
                "issues",
                "tombstones",
                "report_cursor_message_name",
                "report_seen_message_ids",
                "last_relayed_issue_id",
                "missed_calls_offered",
            },
        )
        # Nested issue is a plain dict with stringified enums.
        self.assertEqual(data["issues"][0]["severity"], "med")

    def test_from_dict_defaults(self) -> None:
        s = AgentState.from_dict({})
        self.assertIsNone(s.cursor_message_name)
        self.assertIsNone(s.bot_user_id)
        self.assertEqual(s.seen_message_ids, [])
        self.assertEqual(s.issues, [])
        self.assertEqual(s.tombstones, [])

    def test_empty_state_round_trip(self) -> None:
        empty = AgentState()
        self.assertEqual(AgentState.from_dict(empty.to_dict()), empty)


class IssueFingerprintTest(unittest.TestCase):
    def test_deterministic_and_16_hex(self) -> None:
        fp = issue_fingerprint("t1", "m1", "payments")
        self.assertEqual(fp, issue_fingerprint("t1", "m1", "payments"))
        self.assertEqual(len(fp), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in fp))

    def test_category_case_insensitive(self) -> None:
        self.assertEqual(
            issue_fingerprint("t1", "m1", "Payments"),
            issue_fingerprint("t1", "m1", "payments"),
        )

    def test_category_whitespace_insensitive(self) -> None:
        # Leading/trailing and collapsed internal whitespace are normalized.
        self.assertEqual(
            issue_fingerprint("t1", "m1", "  payment   delays "),
            issue_fingerprint("t1", "m1", "payment delays"),
        )

    def test_different_category_yields_different_fingerprint(self) -> None:
        self.assertNotEqual(
            issue_fingerprint("t1", "m1", "payments"),
            issue_fingerprint("t1", "m1", "login"),
        )

    def test_different_thread_or_root_yields_different_fingerprint(self) -> None:
        base = issue_fingerprint("t1", "m1", "payments")
        self.assertNotEqual(base, issue_fingerprint("t2", "m1", "payments"))
        self.assertNotEqual(base, issue_fingerprint("t1", "m2", "payments"))

    def test_handles_none_inputs(self) -> None:
        # Callers normally pass non-empty anchors, but the helper must not crash
        # on None (treated as empty strings).
        fp = issue_fingerprint(None, None, None)  # ty: ignore[invalid-argument-type]
        self.assertEqual(len(fp), 16)
        self.assertEqual(fp, issue_fingerprint("", "", ""))


if __name__ == "__main__":
    unittest.main()
