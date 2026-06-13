"""Tests for the deterministic `MockLLM` stand-in (§5.3/§12).

`MockLLM` never parses prompts semantically: it branches on which `MARK_*`
token (from `agent.prompts`) appears in the combined system+user text, then
applies keyword/pattern heuristics over the transcript to emit contract-shaped
JSON. These tests drive it through the *real* `agent.prompts` builders for all
four tasks and assert:

* each builder's marker reaches the mock and the right branch fires;
* every response matches its JSON CONTRACT shape exactly;
* the mock is deterministic (same input -> identical output);
* `assess_clarity` flips to clear only once the thread supplies an owner +
  a date + a number, and otherwise reports the still-missing facts.

No network, no real LLM — stdlib `unittest` only.
"""
from __future__ import annotations

import json
import unittest

from gchat_agent.agent import prompts
from gchat_agent.llm.base import LLMClient
from gchat_agent.llm.mock import MockLLM
from gchat_agent.models import (
    Conversation,
    Issue,
    Message,
    Severity,
    SenderType,
    Status,
)


# --- fixture builders --------------------------------------------------------
def _msg(mid: str, sender: str, text: str, thread_id: str = "t1") -> Message:
    """A timestamp-free `Message` so rendered transcripts contain no incidental
    digits/dates — the date/number markers come only from the text we choose."""
    return Message(
        id=mid,
        space="spaces/FAKE",
        thread_id=thread_id,
        sender=sender,
        sender_type=SenderType.HUMAN,
        text=text,
        create_time="",  # no stamp -> render() omits the "[time]" prefix
    )


def _issue(**overrides: object) -> Issue:
    """A minimal `Issue` for the clarity/questions/resolution prompts. The
    defaults avoid owner-hint substrings, dates, and digits so they never leak a
    clarity marker into the brief block embedded in the user prompt."""
    base: dict[str, object] = dict(
        id="i1",
        fingerprint="fp1",
        title="Payments page failing",
        summary="Players report the cashier page errors.",
        category="incident",
        severity=Severity.MEDIUM,
        status=Status.OPEN,
        thread_id="t1",
        root_message_id="a",
        source_message_ids=["a", "b"],
        missing_info=["scope", "deadline"],
    )
    base.update(overrides)
    return Issue(**base)  # type: ignore[arg-type]


# A transcript with issue signals but NO owner/date/number -> clarity "not clear".
_VAGUE = Conversation(
    messages=[
        _msg("a", "users/staff-one", "The payments page is broken for some players."),
        _msg("b", "users/staff-two", "Looks bad, can someone please look?"),
    ]
)

# A transcript that supplies owner ("I'll own"), date ("by friday"), and a number
# ("250") -> clarity flips to clear.
_CLEAR = Conversation(
    messages=[
        _msg("a", "users/staff-one", "The KYC queue has 250 pending verifications and it is blocked."),
        _msg("b", "users/staff-two", "I'll own this and resolve it by friday."),
    ]
)

# Pure small talk, no issue signals -> detection returns an empty list.
_QUIET = Conversation(
    messages=[_msg("z", "users/staff-one", "Morning team, the coffee was great today.")]
)


class MockLLMProtocolTest(unittest.TestCase):
    def test_implements_llmclient_protocol(self) -> None:
        self.assertIsInstance(MockLLM(), LLMClient)

    def test_chat_is_deterministic_and_echoes_last_user(self) -> None:
        llm = MockLLM()
        messages = [{"role": "user", "content": "hello world"}]
        first = llm.chat("system", messages)
        again = llm.chat("system", messages)
        self.assertEqual(first, again)
        self.assertIn("hello world", first)

    def test_chat_handles_empty_message_list(self) -> None:
        self.assertIsInstance(MockLLM().chat("system", []), str)


class DetectIssuesContractTest(unittest.TestCase):
    """`detect_prompt` -> {"issues": [ ... ]} with the per-issue contract shape."""

    def test_detects_signalled_issues_and_matches_shape(self) -> None:
        llm = MockLLM()
        system, user = prompts.detect_prompt(_VAGUE.render(with_ids=True))
        # The marker must reach the mock so the right branch fires.
        self.assertIn(prompts.MARK_DETECT, system)

        result = llm.complete_json(system, user)
        self.assertIsInstance(result, dict)
        self.assertEqual(list(result.keys()), ["issues"])
        issues = result["issues"]
        self.assertIsInstance(issues, list)
        self.assertGreaterEqual(len(issues), 1)

        for issue in issues:
            self.assertEqual(
                set(issue.keys()),
                {"title", "summary", "category", "severity", "source_message_ids", "missing_info"},
            )
            self.assertIsInstance(issue["title"], str)
            self.assertIsInstance(issue["summary"], str)
            self.assertIsInstance(issue["category"], str)
            self.assertIn(issue["severity"], ("low", "med", "high"))
            self.assertIsInstance(issue["source_message_ids"], list)
            self.assertTrue(all(isinstance(s, str) for s in issue["source_message_ids"]))
            self.assertIsInstance(issue["missing_info"], list)
            self.assertTrue(all(isinstance(s, str) for s in issue["missing_info"]))
            # Only ids that appear in the transcript may be cited.
            for sid in issue["source_message_ids"]:
                self.assertIn(sid, {"a", "b"})

    def test_no_signal_transcript_returns_empty_issue_list(self) -> None:
        llm = MockLLM()
        system, user = prompts.detect_prompt(_QUIET.render(with_ids=True))
        result = llm.complete_json(system, user)
        self.assertEqual(result, {"issues": []})

    def test_detection_is_deterministic(self) -> None:
        llm = MockLLM()
        system, user = prompts.detect_prompt(_VAGUE.render(with_ids=True))
        first = json.dumps(llm.complete_json(system, user), sort_keys=True)
        again = json.dumps(MockLLM().complete_json(system, user), sort_keys=True)
        self.assertEqual(first, again)


class AssessClarityContractTest(unittest.TestCase):
    """`clarity_prompt` -> {is_clear, confidence, missing_info, rationale}."""

    def _assert_shape(self, result: dict[str, object]) -> None:
        self.assertEqual(
            set(result.keys()), {"is_clear", "confidence", "missing_info", "rationale"}
        )
        self.assertIsInstance(result["is_clear"], bool)
        self.assertIsInstance(result["confidence"], float)
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)
        self.assertIsInstance(result["missing_info"], list)
        self.assertTrue(all(isinstance(m, str) for m in result["missing_info"]))
        self.assertIsInstance(result["rationale"], str)

    def test_marker_reaches_mock(self) -> None:
        system, _user = prompts.clarity_prompt(_issue(), _VAGUE.render(with_ids=True))
        self.assertIn(prompts.MARK_CLARITY, system)

    def test_not_clear_when_owner_date_number_absent(self) -> None:
        llm = MockLLM()
        system, user = prompts.clarity_prompt(_issue(), _VAGUE.render(with_ids=True))
        result = llm.complete_json(system, user)
        self._assert_shape(result)
        self.assertFalse(result["is_clear"])
        # All three markers are missing, in this fixed order.
        self.assertEqual(
            result["missing_info"], ["owner", "deadline", "specific scope or numbers"]
        )
        self.assertGreater(len(result["missing_info"]), 0)

    def test_clear_once_owner_date_number_present(self) -> None:
        llm = MockLLM()
        # The issue's own missing_info still lists everything; clarity must key off
        # the *transcript*, not the brief, and flip to clear.
        issue = _issue(
            category="compliance",
            missing_info=["owner", "deadline", "specific scope or numbers"],
        )
        system, user = prompts.clarity_prompt(issue, _CLEAR.render(with_ids=True))
        result = llm.complete_json(system, user)
        self._assert_shape(result)
        self.assertTrue(result["is_clear"])
        self.assertEqual(result["confidence"], 0.9)
        self.assertEqual(result["missing_info"], [])

    def test_clarity_is_deterministic(self) -> None:
        system, user = prompts.clarity_prompt(_issue(), _CLEAR.render(with_ids=True))
        first = json.dumps(MockLLM().complete_json(system, user), sort_keys=True)
        again = json.dumps(MockLLM().complete_json(system, user), sort_keys=True)
        self.assertEqual(first, again)


class GenerateQuestionsContractTest(unittest.TestCase):
    """`questions_prompt` -> {"questions": [str, ...]}."""

    def test_marker_reaches_mock(self) -> None:
        system, _user = prompts.questions_prompt(
            _issue(), _VAGUE.render(with_ids=True), ["owner", "deadline"]
        )
        self.assertIn(prompts.MARK_QUESTIONS, system)

    def test_questions_shape_and_targeting(self) -> None:
        llm = MockLLM()
        missing = ["owner", "deadline", "specific scope or numbers"]
        system, user = prompts.questions_prompt(
            _issue(), _VAGUE.render(with_ids=True), missing
        )
        result = llm.complete_json(system, user)
        self.assertEqual(list(result.keys()), ["questions"])
        questions = result["questions"]
        self.assertIsInstance(questions, list)
        self.assertTrue(all(isinstance(q, str) and q for q in questions))
        # The contract asks for 2-3 questions.
        self.assertGreaterEqual(len(questions), 2)
        self.assertLessEqual(len(questions), 3)
        # No duplicates.
        self.assertEqual(len(questions), len(set(questions)))

    def test_questions_fallback_when_no_missing_block(self) -> None:
        # Even with an empty missing_info list the mock still asks >=2 questions.
        llm = MockLLM()
        system, user = prompts.questions_prompt(_issue(), _VAGUE.render(with_ids=True), [])
        result = llm.complete_json(system, user)
        self.assertGreaterEqual(len(result["questions"]), 2)

    def test_questions_are_deterministic(self) -> None:
        missing = ["owner", "deadline"]
        system, user = prompts.questions_prompt(_issue(), _VAGUE.render(with_ids=True), missing)
        first = json.dumps(MockLLM().complete_json(system, user), sort_keys=True)
        again = json.dumps(MockLLM().complete_json(system, user), sort_keys=True)
        self.assertEqual(first, again)


class SummarizeResolutionContractTest(unittest.TestCase):
    """`resolution_prompt` -> {"summary": str, "resolution": str}."""

    def test_marker_reaches_mock(self) -> None:
        system, _user = prompts.resolution_prompt(_issue(), _CLEAR.render(with_ids=True))
        self.assertIn(prompts.MARK_RESOLUTION, system)

    def test_resolution_shape(self) -> None:
        llm = MockLLM()
        system, user = prompts.resolution_prompt(_issue(), _CLEAR.render(with_ids=True))
        result = llm.complete_json(system, user)
        self.assertEqual(set(result.keys()), {"summary", "resolution"})
        self.assertIsInstance(result["summary"], str)
        self.assertIsInstance(result["resolution"], str)
        self.assertTrue(result["summary"])
        self.assertTrue(result["resolution"])

    def test_resolution_is_deterministic(self) -> None:
        system, user = prompts.resolution_prompt(_issue(), _CLEAR.render(with_ids=True))
        first = json.dumps(MockLLM().complete_json(system, user), sort_keys=True)
        again = json.dumps(MockLLM().complete_json(system, user), sort_keys=True)
        self.assertEqual(first, again)


class UnknownTaskTest(unittest.TestCase):
    def test_no_marker_degrades_to_empty_issues_object(self) -> None:
        # A prompt carrying none of the MARK_* tokens still returns a valid object
        # (so the foundation's object-only extract_json never sees a bare array).
        result = MockLLM().complete_json("system with no marker", "user with no marker")
        self.assertEqual(result, {"issues": []})


class AllFourMarkersDistinctTest(unittest.TestCase):
    def test_four_markers_route_to_four_distinct_branches(self) -> None:
        """Drive all four builders and confirm each returns its own contract key
        set — i.e. the mock routed to a different branch for each marker."""
        llm = MockLLM()
        issue = _issue()
        transcript = _CLEAR.render(with_ids=True)

        detect = llm.complete_json(*prompts.detect_prompt(transcript))
        clarity = llm.complete_json(*prompts.clarity_prompt(issue, transcript))
        questions = llm.complete_json(
            *prompts.questions_prompt(issue, transcript, ["owner"])
        )
        resolution = llm.complete_json(*prompts.resolution_prompt(issue, transcript))

        self.assertIn("issues", detect)
        self.assertIn("is_clear", clarity)
        self.assertIn("questions", questions)
        self.assertEqual(set(resolution.keys()), {"summary", "resolution"})

        # The four markers are distinct tokens.
        markers = {
            prompts.MARK_DETECT,
            prompts.MARK_CLARITY,
            prompts.MARK_QUESTIONS,
            prompts.MARK_RESOLUTION,
        }
        self.assertEqual(len(markers), 4)


if __name__ == "__main__":
    unittest.main()
