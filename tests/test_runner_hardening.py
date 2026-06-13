"""Regression tests for the runner/state hardening (review-driven, §5.7/§6).

These pin behaviors the happy-path end-to-end test (`test_loop`) does not exercise:

* a STALE issue is tombstoned and never re-detected (not just resolved ones);
* the anti-spam reply gate (`_new_replies`) is conservative when the bot's last
  question isn't anchored in the working view (returns no "fresh replies");
* resolution is idempotent across a crash *between* the report write and the
  confirmation post — the confirmation is still posted, the file isn't rewritten;
* the fetch boundary `_since` prefers the persisted cursor over the backfill and
  is widened by a small skew so equal-`createTime` messages aren't dropped;
* a structurally-valid-but-malformed state file loads as fresh, not a crash.

Stdlib `unittest`; offline (MockLLM + FakeChatClient); no network.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace

from gchat_agent.agent.analyzer import Analyzer
from gchat_agent.agent.state import IssueStore
from gchat_agent.config import load_config
from gchat_agent.llm.mock import MockLLM
from gchat_agent.models import (
    ClarityAssessment,
    Conversation,
    Issue,
    Message,
    Severity,
    SenderType,
    Status,
    issue_fingerprint,
)
from gchat_agent.runner import Runner, _minus_seconds
from tests.fakes import FakeChatClient

BOT_ID = "users/bot"
STAFF_ID = "users/staff-ops"
SEED_TEXT = "Payments are failing in production and blocking checkout, need help asap."


def _config(tmp: str, **over):
    """A real Config off the defaults, paths redirected to a temp dir, with an
    early backfill so the first cycle actually fetches the seed."""
    cfg = replace(
        load_config(env_file=os.path.join(tmp, "no-such.env")),
        STATE_FILE=os.path.join(tmp, "state", "issues.json"),
        REPORTS_DIR=os.path.join(tmp, "reports"),
        POLL_BACKFILL_SINCE="2020-01-01T00:00:00Z",
    )
    return replace(cfg, **over) if over else cfg


def _analyzer():
    return Analyzer(MockLLM(), retriever=None, top_k=0)


def _msg(mid: str, thread: str, sender: str, text: str,
         t: str = "2026-06-13T10:00:00.000000Z") -> Message:
    return Message(id=mid, space="spaces/x", thread_id=thread, sender=sender,
                   sender_type=SenderType.HUMAN, text=text, create_time=t)


def _issue(thread: str = "spaces/x/threads/t1", last_bot: str | None = None) -> Issue:
    fp = issue_fingerprint(thread, "r1", "incident")
    return Issue(
        id=fp, fingerprint=fp, title="Payments failing", summary="prod outage",
        category="incident", severity=Severity.HIGH, status=Status.CLARIFYING,
        thread_id=thread, root_message_id="r1", source_message_ids=["r1"],
        missing_info=[], questions_asked=["What is the owner?"],
        last_bot_message_id=last_bot,
    )


class _NoResolveAnalyzer(Analyzer):
    """Real detect/question path, but clarity never passes — drives an issue to
    STALE deterministically regardless of transcript content."""

    def assess_clarity(self, issue, conversation):  # type: ignore[override]
        return ClarityAssessment(
            is_clear=False, confidence=0.0, missing_info=["owner"], rationale="test"
        )


class StaleTombstoneTest(unittest.TestCase):
    """A stale issue must be tombstoned and never re-detected (§6)."""

    def test_idle_issue_goes_stale_tombstoned_and_not_reraised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, STALE_AFTER_IDLE_CYCLES=1, MAX_CLARIFY_ROUNDS=3)
            chat = FakeChatClient(me=BOT_ID)
            chat.inject(STAFF_ID, SEED_TEXT)
            store = IssueStore(config.STATE_FILE)
            runner = Runner(
                chat, _NoResolveAnalyzer(MockLLM(), retriever=None, top_k=0), store, config
            )

            s1 = runner.run_cycle()  # detect + ask
            self.assertEqual(s1["detected"], 1)
            self.assertEqual(s1["asked"], 1)
            fp = store.open_issues()[0].fingerprint

            s2 = runner.run_cycle()  # no reply -> idle>=1 -> stale + tombstone
            self.assertEqual(s2["stale"], 1)
            self.assertEqual(store.open_issues(), [])
            self.assertTrue(store.is_tombstoned(fp), "stale issue must be tombstoned")

            s3 = runner.run_cycle()  # same seed present -> must NOT re-raise
            self.assertEqual(s3["detected"], 0, "tombstoned stale issue was re-raised")
            self.assertEqual(len(store.all_issues()), 1, "duplicate issue created")


class NewRepliesGuardTest(unittest.TestCase):
    """`_new_replies` is conservative unless the bot's question is anchored."""

    def _runner(self, tmp: str) -> Runner:
        config = _config(tmp)
        return Runner(FakeChatClient(me=BOT_ID), _analyzer(),
                      IssueStore(config.STATE_FILE), config)

    def test_no_anchor_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = self._runner(tmp)
            issue = _issue(last_bot=None)
            conv = Conversation([_msg("m1", issue.thread_id, STAFF_ID, "hi")])
            self.assertEqual(r._new_replies(issue, conv, BOT_ID), [])

    def test_missing_anchor_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = self._runner(tmp)
            issue = _issue(last_bot="not-in-thread")
            conv = Conversation([_msg("m1", issue.thread_id, STAFF_ID, "hi")])
            self.assertEqual(r._new_replies(issue, conv, BOT_ID), [])

    def test_present_anchor_returns_following_non_bot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = self._runner(tmp)
            issue = _issue(last_bot="b1")
            conv = Conversation([
                _msg("b1", issue.thread_id, BOT_ID, "question?", "2026-06-13T10:00:01.0Z"),
                _msg("s1", issue.thread_id, STAFF_ID, "answer", "2026-06-13T10:00:02.0Z"),
            ])
            out = r._new_replies(issue, conv, BOT_ID)
            self.assertEqual([m.id for m in out], ["s1"])


class ResolveIdempotencyTest(unittest.TestCase):
    """Resolution survives a crash between the report write and the post."""

    def _setup(self, tmp: str):
        config = _config(tmp)
        chat = FakeChatClient(me=BOT_ID)
        store = IssueStore(config.STATE_FILE)
        runner = Runner(chat, _analyzer(), store, config)
        issue = _issue()
        thread_conv = Conversation([_msg("s1", issue.thread_id, STAFF_ID, "context")])
        return config, chat, store, runner, issue, thread_conv

    def test_confirmation_posted_even_if_report_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, chat, store, runner, issue, thread_conv = self._setup(tmp)
            issue.report_written_at = None
            # Simulate a crash AFTER write_report but BEFORE the confirmation post.
            os.makedirs(config.REPORTS_DIR, exist_ok=True)
            report_path = os.path.join(config.REPORTS_DIR, f"issue-{issue.id}.md")
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write("# pre-existing report — must not be overwritten\n")

            runner._resolve(issue, thread_conv)

            bot_msgs = [m for m in chat.messages if m.sender == BOT_ID]
            self.assertEqual(len(bot_msgs), 1, "confirmation must still be posted")
            self.assertIn("resolved", bot_msgs[0].text.lower())
            self.assertTrue(issue.report_written_at)
            self.assertEqual(issue.status, Status.RESOLVED)
            self.assertTrue(store.is_tombstoned(issue.fingerprint))
            with open(report_path, encoding="utf-8") as fh:
                self.assertIn("pre-existing", fh.read(), "existing report was overwritten")

    def test_no_repost_when_already_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _config_, chat, store, runner, issue, thread_conv = self._setup(tmp)
            issue.report_written_at = "2026-01-01T00:00:00Z"  # already done
            runner._resolve(issue, thread_conv)
            self.assertEqual(
                [m for m in chat.messages if m.sender == BOT_ID], [],
                "must not re-post a confirmation when already recorded",
            )
            self.assertEqual(issue.status, Status.RESOLVED)


class SinceBoundaryTest(unittest.TestCase):
    """`_since` precedence (cursor > backfill) and equal-timestamp skew."""

    def test_minus_seconds(self) -> None:
        self.assertEqual(
            _minus_seconds("2026-06-13T10:00:05Z", 2), "2026-06-13T10:00:03+00:00"
        )
        self.assertEqual(_minus_seconds("not-a-time", 2), "not-a-time")

    def test_persisted_cursor_beats_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, POLL_BACKFILL_SINCE="2020-01-01T00:00:00Z")
            r = Runner(FakeChatClient(me=BOT_ID), _analyzer(),
                       IssueStore(config.STATE_FILE), config)
            since = r._since("2026-06-13T12:00:00Z")  # a persisted pin
            self.assertTrue(
                since.startswith("2026-06-13"),
                f"persisted cursor must win over backfill, got {since!r}",
            )

    def test_first_run_uses_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, POLL_BACKFILL_SINCE="2020-01-01T00:00:00Z")
            r = Runner(FakeChatClient(me=BOT_ID), _analyzer(),
                       IssueStore(config.STATE_FILE), config)
            self.assertEqual(r._since(None), "2020-01-01T00:00:00Z")

    def test_first_run_no_backfill_pins_to_now(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, POLL_BACKFILL_SINCE="")
            r = Runner(FakeChatClient(me=BOT_ID), _analyzer(),
                       IssueStore(config.STATE_FILE), config)
            since = r._since(None)
            self.assertIsNotNone(since)
            self.assertTrue(since.startswith("20"), "should pin to an RFC-3339 'now'")


class LoadCorruptStateTest(unittest.TestCase):
    """A valid-JSON-but-malformed state file loads as fresh, never crashes."""

    def test_issue_missing_required_keys_yields_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "issues.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"issues": [{"title": "no id or fingerprint"}]}, fh)
            store = IssueStore(path)
            store.load()  # must not raise
            self.assertEqual(store.all_issues(), [])
            self.assertIsNone(store.get_cursor()[0])

    def test_non_dict_json_yields_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "issues.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([1, 2, 3], fh)
            store = IssueStore(path)
            store.load()
            self.assertEqual(store.all_issues(), [])


if __name__ == "__main__":
    unittest.main()
