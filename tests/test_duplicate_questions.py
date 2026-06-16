"""Regression tests for the duplicate-question fix (§ loop-breaker).

The bot used to re-ask the same clarifying questions when the reporter could not
answer them (e.g. replied "I don't know"): the clarity assessment kept reporting
the same facts missing, so it kept generating questions for them. Two guards fix
this and are pinned here:

* **Prompt contract** — `clarity_prompt` / `questions_prompt` now show the model
  every question already asked and instruct it never to repeat one, and to treat
  a fact the reporter declined ("I don't know") as unobtainable rather than
  re-asking. This is verified by asserting the rendered prompt content (the live
  model is not exercised offline).
* **Runner loop-breaker** — a deterministic, model-agnostic backstop: when the
  reporter replies but the missing-facts set does not shrink, the runner stops
  re-asking and closes the issue WITH the gaps documented, rather than repeating
  the question. A "I don't know" reply closes it immediately; an unproductive but
  non-decline exchange closes it after `MAX_NO_PROGRESS_ROUNDS`.

Stdlib `unittest`; offline (MockLLM + FakeChatClient); no network.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace

from gchat_agent.agent.analyzer import Analyzer
from gchat_agent.agent.prompts import clarity_prompt, questions_prompt
from gchat_agent.agent.state import IssueStore
from gchat_agent.config import load_config
from gchat_agent.llm.mock import MockLLM
from gchat_agent.models import (
    ClarityAssessment,
    Issue,
    Severity,
    Status,
)
from gchat_agent.runner import Runner, _looks_like_decline
from tests.fakes import FakeChatClient

BOT_ID = "users/bot"
STAFF_ID = "users/staff-ops"
SEED_TEXT = "The homepage is down with a 404 in production, blocking players asap."


def _config(tmp: str, **over):
    cfg = replace(
        load_config(env_file=os.path.join(tmp, "no-such.env")),
        STATE_FILE=os.path.join(tmp, "state", "issues.json"),
        REPORTS_DIR=os.path.join(tmp, "reports"),
        POLL_BACKFILL_SINCE="2020-01-01T00:00:00Z",
        ESCALATE_AFTER_SECONDS=-1,  # isolate the loop-breaker from escalation
    )
    return replace(cfg, **over) if over else cfg


def _issue_with_questions() -> Issue:
    return Issue(
        id="i1", fingerprint="i1", title="Homepage 404", summary="prod outage",
        category="incident", severity=Severity.HIGH, status=Status.CLARIFYING,
        thread_id="spaces/x/threads/t1", root_message_id="r1",
        source_message_ids=["r1"], missing_info=["a named owner"],
        # Two batches, the second multi-line — flattened into individual questions.
        questions_asked=[
            "Which exact URL returns the 404?",
            "Who is the assigned owner?\nWhat is the target time to mitigate?",
        ],
    )


class PromptAntiRepeatContractTest(unittest.TestCase):
    """The clarity / questions prompts carry the asked-questions block and the
    don't-repeat / "I don't know" instructions."""

    def test_clarity_prompt_lists_asked_and_forbids_repeats(self) -> None:
        issue = _issue_with_questions()
        system, user = clarity_prompt(issue, "transcript")
        # Every previously-asked question is surfaced to the model…
        self.assertIn("Which exact URL returns the 404?", user)
        self.assertIn("Who is the assigned owner?", user)
        self.assertIn("What is the target time to mitigate?", user)
        self.assertIn("Questions ALREADY asked", user)
        # …and the contract forbids re-asking + handles declines as unobtainable.
        self.assertIn("UNOBTAINABLE", system)
        self.assertIn("NEVER repeat a question already asked of the reporter", system)
        self.assertIn("I don't know", system)

    def test_questions_prompt_lists_asked_and_forbids_repeats(self) -> None:
        issue = _issue_with_questions()
        system, user = questions_prompt(issue, "transcript", ["a named owner"])
        self.assertIn("Questions ALREADY asked", user)
        self.assertIn("Who is the assigned owner?", user)
        self.assertIn("NEVER repeat a question already asked of the reporter", system)
        self.assertIn("unobtainable", system.lower())

    def test_first_contact_has_no_asked_block(self) -> None:
        # An issue with nothing asked yet must not carry the asked block.
        issue = replace(_issue_with_questions(), questions_asked=[])
        _system, user = clarity_prompt(issue, "transcript")
        self.assertNotIn("Questions ALREADY asked", user)


class DeclineDetectionTest(unittest.TestCase):
    """`_looks_like_decline` flags short "I can't answer" replies, not long ones."""

    def test_flags_short_declines(self) -> None:
        for text in ("I don't know", "no idea", "not sure", "idk", "Dunno, sorry"):
            self.assertTrue(_looks_like_decline(text), text)

    def test_ignores_substantive_replies_even_with_decline_words(self) -> None:
        # Contains "not sure" but is a long, informative answer — not a decline.
        text = (
            "I'm not sure of the exact commit but Jane owns it and the fix lands "
            "by tomorrow affecting 12 accounts on the homepage route."
        )
        self.assertFalse(_looks_like_decline(text))

    def test_empty_is_not_a_decline(self) -> None:
        self.assertFalse(_looks_like_decline(""))


class _StuckClarityAnalyzer(Analyzer):
    """Real detection (so first contact opens with inline questions), but clarity
    NEVER passes and always reports the SAME missing facts — so without the
    loop-breaker the bot would re-ask the identical questions every cycle."""

    MISSING = ["a named owner", "the target time to mitigate"]

    def assess_clarity(self, issue, conversation):  # type: ignore[override]
        return ClarityAssessment(
            is_clear=False, confidence=0.0, missing_info=list(self.MISSING),
            rationale="still missing core facts",
            questions=["Who is the assigned owner?", "What is the target time?"],
        )


class LoopBreakerTest(unittest.TestCase):
    """The bot stops re-asking and closes WITH gaps instead of repeating."""

    def _bot_questions(self, chat) -> list[str]:
        """Bot messages that are clarifying questions (not the confirmation)."""
        return [
            m.text for m in chat.messages
            if m.sender == BOT_ID and "recorded" not in m.text.lower()
        ]

    def test_i_dont_know_closes_with_gaps_without_re_asking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, MAX_CLARIFY_ROUNDS=5, MAX_NO_PROGRESS_ROUNDS=2)
            chat = FakeChatClient(me=BOT_ID)
            chat.inject(STAFF_ID, SEED_TEXT)
            store = IssueStore(config.STATE_FILE)
            runner = Runner(
                chat, _StuckClarityAnalyzer(MockLLM(), retriever=None, top_k=0),
                store, config,
            )

            # Cycle 1: detect + first-contact ask.
            s1 = runner.run_cycle()
            self.assertEqual(s1["detected"], 1)
            self.assertEqual(s1["asked"], 1)
            issue = store.open_issues()[0]
            thread = issue.thread_id

            # Reporter gives a real (non-decline) partial answer → baseline.
            chat.inject(STAFF_ID, "It is the homepage at igaming.com.", thread_id=thread)
            s2 = runner.run_cycle()
            self.assertEqual(s2["asked"], 1, "a productive reply earns a re-ask")
            self.assertEqual(s2["resolved"], 0)

            asked_before_decline = len(self._bot_questions(chat))

            # Reporter now declines → the bot must NOT re-ask; it closes with gaps.
            chat.inject(STAFF_ID, "I don't know.", thread_id=thread)
            s3 = runner.run_cycle()
            self.assertEqual(s3["asked"], 0, "must not re-ask after 'I don't know'")
            self.assertEqual(s3["resolved"], 1, "must close the issue with gaps")

            # No new clarifying question was posted on the declining cycle.
            self.assertEqual(
                len(self._bot_questions(chat)), asked_before_decline,
                "the bot re-asked after a decline (the duplicate-question bug)",
            )

            # The issue is closed, tombstoned, and documents the open questions.
            self.assertEqual(store.open_issues(), [])
            closed = next(i for i in store.all_issues() if i.id == issue.id)
            self.assertEqual(closed.status, Status.RESOLVED)
            self.assertTrue(store.is_tombstoned(closed.fingerprint))
            self.assertTrue(closed.report_written_at)

            # The Markdown report names the unanswered facts.
            report_path = os.path.join(config.REPORTS_DIR, f"issue-{closed.id}.md")
            with open(report_path, encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("Open questions", body)
            self.assertIn("a named owner", body)

            # The in-thread confirmation is honest about the gaps.
            confirmation = next(
                m for m in chat.messages
                if m.sender == BOT_ID and "recorded" in m.text.lower()
            )
            self.assertIn("open questions", confirmation.text.lower())
            self.assertIn("still needs", confirmation.text.lower())

    def test_no_progress_closes_after_limit_without_decline(self) -> None:
        # Even without an explicit "I don't know", an exchange that never shrinks
        # the gap is closed after MAX_NO_PROGRESS_ROUNDS replies (no infinite loop).
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, MAX_CLARIFY_ROUNDS=9, MAX_NO_PROGRESS_ROUNDS=2)
            chat = FakeChatClient(me=BOT_ID)
            chat.inject(STAFF_ID, SEED_TEXT)
            store = IssueStore(config.STATE_FILE)
            runner = Runner(
                chat, _StuckClarityAnalyzer(MockLLM(), retriever=None, top_k=0),
                store, config,
            )

            runner.run_cycle()  # detect + ask
            issue = store.open_issues()[0]
            thread = issue.thread_id

            # Distinct, substantive-looking but unhelpful replies (no decline word).
            outcomes = []
            for text in ("Some context one here.", "More context two here.",
                         "Even more context three.", "Yet more context four."):
                chat.inject(STAFF_ID, text, thread_id=thread)
                outcomes.append(runner.run_cycle())
                if store.open_issues() == []:
                    break

            self.assertEqual(store.open_issues(), [], "no-progress loop never closed")
            closed = next(i for i in store.all_issues() if i.id == issue.id)
            self.assertEqual(closed.status, Status.RESOLVED)
            # Closed via the no-progress backstop, not the MAX_CLARIFY_ROUNDS cap
            # (cap is 9; it must close well before that).
            self.assertLessEqual(closed.rounds, 4)
            self.assertTrue(any(o["resolved"] for o in outcomes))


if __name__ == "__main__":
    unittest.main()
