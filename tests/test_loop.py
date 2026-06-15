"""End-to-end clarification round-trip — the crown-jewel offline loop (§12).

This proves the whole demo loop with zero network: a :class:`StaffAgent` seeds an
issue-laden message into a shared in-memory space, the bot (a real :class:`Runner`
driven by :class:`MockLLM` + :class:`FakeChatClient`) detects it and asks a
clarifying question, the staff persona answers with the facts the mock needs to
judge the thread *clear*, and a second cycle resolves the issue — writing
``reports/issue-<id>.md`` and posting a confirmation reply.

Everything is deterministic: the :class:`MockLLM` calls a thread clear only once
its transcript carries an owner hint + a date + a number, and the
:class:`FakeChatClient` mints ids/timestamps from a monotonic counter. The test
also pins the two safety invariants the loop must never violate:

* **anti-spam** — the bot never posts a second clarifying question while it is
  still waiting on a staff reply;
* **no self-detection** — the bot's own questions/confirmations are never raised
  as fresh issues.

One faithful-to-intent test seam (:class:`_DemoAnalyzer`): :class:`MockLLM`'s
``assess_clarity`` keys off the *whole* clarity prompt — which includes the issue
brief's ``already-noted missing info`` line — so a freshly-detected issue whose
``missing_info`` lists ``"owner"`` would be judged clear on the very first cycle
(the brief itself supplies the "owner" token, and a date + number always fall out
of the rendered ``[create_time]`` stamps). That collapses the multi-round loop the
mock *documents* (clear only once the **transcript** shows an owner). The seam
simply clears each detected candidate's ``missing_info`` so clarity depends purely
on the transcript's owner hint — exactly the documented contract — while every
other decision (detection, clarity, question wording, resolution) stays the real
:class:`Analyzer` + :class:`MockLLM`.

Stdlib ``unittest`` only; injects the fakes; touches no real Google/OpenRouter.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace

from gchat_agent.agent.analyzer import Analyzer
from gchat_agent.agent.staff import StaffAgent
from gchat_agent.agent.state import IssueStore
from gchat_agent.config import load_config
from gchat_agent.llm.mock import MockLLM
from gchat_agent.models import Message, Status
from gchat_agent.runner import Runner
from tests.fakes import FakeChatClient, StaffChatView

BOT_ID = "users/bot"
STAFF_ID = "users/staff-ops"

# A single seed message carrying multiple issue signals (fail/blocked/need/asap)
# so MockLLM._detect flags exactly one issue line and mints one OPEN issue.
SEED_TEXT = (
    "Payments are failing in production and it is blocking checkout. "
    "We need help on this asap."
)

# One persona fact whose *value* (posted verbatim offline, since MockLLM.chat
# returns a generic ack) satisfies all three of the mock's clarity gates at
# once: owner hint ("I'll"/"own"), a date ("tomorrow"), and a number ("12").
RICH_DISCLOSURE = "I'll own this; the fix lands by tomorrow and it affects 12 accounts."

PERSONA = {
    "role": "You are the Ops lead in an iGaming work chat.",
    "facts": {"owner": RICH_DISCLOSURE},
    "withholding_policy": "Reveal details only when directly asked.",
    "seed_messages": [SEED_TEXT],
}


class _DemoAnalyzer(Analyzer):
    """The real :class:`Analyzer`, with detected candidates' ``missing_info``
    cleared (see the module docstring).

    :class:`MockLLM` resolves a thread as soon as its clarity *prompt* shows an
    owner token; because that prompt embeds the issue brief, a detected issue
    that carries ``missing_info=["owner"]`` would resolve on its first cycle. We
    drop the candidate's ``missing_info`` so the clarity decision keys purely on
    the transcript's owner hint — the mock's *documented* behavior — letting the
    genuine clarify → answer → resolve loop run. Every LLM call is untouched.
    """

    def detect_issues(self, conversation):  # type: ignore[override]
        issues = super().detect_issues(conversation)
        for issue in issues:
            issue.missing_info = []
        return issues


class ClarificationRoundTripTest(unittest.TestCase):
    """The full seed → detect → ask → answer → resolve loop, end to end."""

    def setUp(self) -> None:
        # Isolated temp state file + reports dir per test (no shared on-disk state).
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        state_file = os.path.join(self._tmp.name, "state", "issues.json")
        self.reports_dir = os.path.join(self._tmp.name, "reports")

        # Real Config from defaults, with only the paths redirected to temp. A
        # tight clarify cap keeps the loop within bounds; the mock resolves at
        # 0.9 ≥ the default RESOLVE_CONFIDENCE_THRESHOLD (0.8).
        self.config = replace(
            load_config(env_file=os.path.join(self._tmp.name, "no-such.env")),
            STATE_FILE=state_file,
            REPORTS_DIR=self.reports_dir,
            MAX_CLARIFY_ROUNDS=3,
            # Backfill from before the FakeChatClient's fixed epoch (2026-01-01)
            # so the first cycle actually fetches the seed instead of pinning the
            # cursor to wall-clock "now" and skipping all history.
            POLL_BACKFILL_SINCE="2020-01-01T00:00:00Z",
        )

        # The bot's chat client + a staff view over the same backing store.
        self.chat = FakeChatClient(me=BOT_ID)
        self.staff_chat = StaffChatView(self.chat, me=STAFF_ID)

        llm = MockLLM()
        # retriever=None → direct-context bypass (no KB needed offline).
        self.analyzer = _DemoAnalyzer(llm, retriever=None, top_k=0)
        self.store = IssueStore(state_file)
        self.runner = Runner(self.chat, self.analyzer, self.store, self.config)

        # Staff persona shares the LLM but posts through its own staff view.
        self.staff = StaffAgent(llm, self.staff_chat, PERSONA)

    # --- helpers ------------------------------------------------------------
    def _bot_messages(self) -> list[Message]:
        return [m for m in self.chat.messages if m.sender == BOT_ID]

    def _open_issue(self):
        issues = self.store.open_issues()
        self.assertEqual(len(issues), 1, "expected exactly one open issue")
        return issues[0]

    # --- the round-trip -----------------------------------------------------
    def test_full_clarification_round_trip(self) -> None:
        # --- staff seeds the issue ------------------------------------------
        seeded = self.staff.seed()
        self.assertEqual(len(seeded), 1)
        self.assertEqual(seeded[0].sender, STAFF_ID)
        thread_id = seeded[0].thread_id
        self.assertTrue(thread_id)

        # --- cycle 1: detect + ask one clarifying question ------------------
        summary1 = self.runner.run_cycle()
        self.assertGreaterEqual(summary1["fetched"], 1)
        self.assertEqual(summary1["detected"], 1)
        self.assertEqual(summary1["asked"], 1)
        self.assertEqual(summary1["resolved"], 0)

        issue = self._open_issue()
        self.assertEqual(issue.status, Status.CLARIFYING)
        self.assertEqual(issue.rounds, 1)
        self.assertEqual(issue.thread_id, thread_id)
        # The detected issue must anchor to the *staff* seed, never a bot post.
        self.assertEqual(issue.root_message_id, seeded[0].id)

        bot_after_ask = self._bot_messages()
        self.assertEqual(len(bot_after_ask), 1, "bot should have asked exactly once")
        question_msg = bot_after_ask[0]
        self.assertEqual(question_msg.thread_id, thread_id)
        question_text = issue.questions_asked[-1]
        self.assertTrue(question_text.strip())

        # --- anti-spam: a cycle with no staff reply must NOT re-ask ---------
        summary_idle = self.runner.run_cycle()
        self.assertEqual(summary_idle["asked"], 0)
        self.assertEqual(summary_idle["resolved"], 0)
        self.assertEqual(
            len(self._bot_messages()),
            1,
            "bot must not post a second question without a fresh staff reply",
        )
        # Still clarifying, still one round, but an idle cycle was recorded.
        idle_issue = self._open_issue()
        self.assertEqual(idle_issue.status, Status.CLARIFYING)
        self.assertEqual(idle_issue.rounds, 1)
        self.assertEqual(idle_issue.idle_cycles, 1)

        # --- staff answers, revealing owner + date + number ------------------
        answer = self.staff.answer_question(thread_id, question_text)
        self.assertIsNotNone(answer)
        assert answer is not None  # for type-checkers
        self.assertEqual(answer.sender, STAFF_ID)
        self.assertEqual(answer.thread_id, thread_id)
        self.assertEqual(answer.text, RICH_DISCLOSURE)

        # --- cycle 2: clarity now passes → resolve + report + confirmation ---
        summary2 = self.runner.run_cycle()
        self.assertEqual(summary2["resolved"], 1)
        self.assertEqual(summary2["stale"], 0)

        # Issue is resolved (and tombstoned) in the store.
        self.assertEqual(self.store.open_issues(), [])
        resolved = next(i for i in self.store.all_issues() if i.id == issue.id)
        self.assertEqual(resolved.status, Status.RESOLVED)
        self.assertTrue(self.store.is_tombstoned(resolved.fingerprint))
        self.assertTrue(resolved.report_written_at)
        # The captured Q&A links the bot's question to the staff answer.
        self.assertEqual(len(resolved.qa), 1)
        self.assertIn(answer.id, resolved.qa[0].answer_message_ids)

        # The report file exists on disk at reports/issue-<id>.md.
        report_path = os.path.join(self.reports_dir, f"issue-{resolved.id}.md")
        self.assertTrue(
            os.path.isfile(report_path), f"missing report file: {report_path}"
        )
        report_body = self._read(report_path)
        self.assertIn("Resolved", report_body)

        # A confirmation reply was posted by the bot into the thread.
        bot_after_resolve = self._bot_messages()
        self.assertEqual(
            len(bot_after_resolve), 2, "bot posts exactly one question + one confirmation"
        )
        confirmation = bot_after_resolve[-1]
        self.assertEqual(confirmation.thread_id, thread_id)
        self.assertIn("resolved", confirmation.text.lower())
        self.assertIn(f"issue-{resolved.id}.md", confirmation.text)

        # --- anti-spam + no self-detection on a trailing cycle ---------------
        # Run once more with nothing new from staff: a tombstoned issue must not
        # be re-raised, and the bot's own question/confirmation must not be
        # detected as a fresh issue.
        bot_count_before = len(self._bot_messages())
        summary3 = self.runner.run_cycle()
        self.assertEqual(summary3["detected"], 0)
        self.assertEqual(summary3["asked"], 0)
        self.assertEqual(summary3["resolved"], 0)
        self.assertEqual(self.store.open_issues(), [])
        self.assertEqual(
            len(self._bot_messages()),
            bot_count_before,
            "bot must not post again after resolution",
        )

        # Belt-and-braces: no tracked issue ever anchored to a bot-authored
        # message (the bot never detected its own messages as issues).
        bot_msg_ids = {m.id for m in self._bot_messages()}
        for tracked in self.store.all_issues():
            self.assertNotIn(tracked.root_message_id, bot_msg_ids)
            self.assertFalse(set(tracked.source_message_ids) & bot_msg_ids)

    # --- file helper --------------------------------------------------------
    @staticmethod
    def _read(path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()


class StaffViewSanityTest(unittest.TestCase):
    """Guards the test's own shared-backend fake: staff posts are visible to the
    bot's client and authored as the staff user (not the bot)."""

    def test_staff_posts_are_visible_and_attributed(self) -> None:
        chat = FakeChatClient(me=BOT_ID)
        staff_chat = StaffChatView(chat, me=STAFF_ID)
        posted = staff_chat.post_message("hello from staff")
        self.assertEqual(posted.sender, STAFF_ID)
        # Visible through the bot's own client (shared backend).
        fetched = chat.fetch_messages(None)
        self.assertEqual([m.text for m in fetched], ["hello from staff"])
        self.assertEqual(fetched[0].sender, STAFF_ID)

    def test_staff_view_request_id_idempotent(self) -> None:
        chat = FakeChatClient(me=BOT_ID)
        staff_chat = StaffChatView(chat, me=STAFF_ID)
        a = staff_chat.post_message("once", request_id="r1")
        b = staff_chat.post_message("again", request_id="r1")
        self.assertIs(a, b)
        self.assertEqual(len(chat.fetch_messages(None)), 1)


if __name__ == "__main__":
    unittest.main()
