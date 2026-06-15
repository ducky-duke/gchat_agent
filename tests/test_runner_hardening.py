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
from unittest import mock

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
from gchat_agent.runner import (
    Runner,
    _acquire_lock,
    _minus_seconds,
    _release_lock,
)
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


class _NoQuestionsAnalyzer(Analyzer):
    """Clarity never passes and question generation yields nothing — exercises the
    'no questions this cycle' path (e.g. a transient empty LLM reply)."""

    def assess_clarity(self, issue, conversation):  # type: ignore[override]
        return ClarityAssessment(
            is_clear=False, confidence=0.0, missing_info=["owner"], rationale="t"
        )

    def generate_questions(self, issue, conversation, missing_info):  # type: ignore[override]  # noqa: ARG002
        return []


class NoQuestionsIdleTest(unittest.TestCase):
    """A no-questions cycle is treated as idle and retried, only staling after
    STALE_AFTER_IDLE_CYCLES — not immediately (resilience to transient empties)."""

    def test_no_questions_idles_then_stales(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, STALE_AFTER_IDLE_CYCLES=2, MAX_CLARIFY_ROUNDS=3)
            chat = FakeChatClient(me=BOT_ID)
            chat.inject(STAFF_ID, SEED_TEXT)
            store = IssueStore(config.STATE_FILE)
            runner = Runner(
                chat, _NoQuestionsAnalyzer(MockLLM(), retriever=None, top_k=0), store, config
            )

            s1 = runner.run_cycle()  # detect; no questions -> idle=1, NOT stale
            self.assertEqual(s1["detected"], 1)
            self.assertEqual(s1["asked"], 0)
            self.assertEqual(s1["stale"], 0)
            self.assertEqual(len(store.open_issues()), 1, "must not stale on the first empty")

            s2 = runner.run_cycle()  # idle reaches the cap -> stale
            self.assertEqual(s2["stale"], 1)
            self.assertEqual(store.open_issues(), [])


class _ClaritySpyAnalyzer(Analyzer):
    """Counts assess_clarity / generate_questions calls so a test can prove the
    first-contact fast path opens with questions WITHOUT a clarity round-trip."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.assess_calls = 0
        self.question_calls = 0

    def assess_clarity(self, issue, conversation):  # type: ignore[override]
        self.assess_calls += 1
        return ClarityAssessment(
            is_clear=False, confidence=0.0, missing_info=["owner"], rationale="t"
        )

    def generate_questions(self, issue, conversation, missing_info):  # type: ignore[override]  # noqa: ARG002
        self.question_calls += 1
        return ["Who is the owner?"]


class FirstContactSkipsClarityTest(unittest.TestCase):
    """A freshly detected issue opens with questions WITHOUT an `assess_clarity`
    call (it is definitionally not clear yet — skipping that LLM round-trip is the
    latency win); once a reply arrives, clarity is assessed normally."""

    def test_first_contact_skips_assess_then_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, STALE_AFTER_IDLE_CYCLES=3, MAX_CLARIFY_ROUNDS=3)
            chat = FakeChatClient(me=BOT_ID)
            chat.inject(STAFF_ID, SEED_TEXT)
            store = IssueStore(config.STATE_FILE)
            analyzer = _ClaritySpyAnalyzer(MockLLM(), retriever=None, top_k=0)
            runner = Runner(chat, analyzer, store, config)

            s1 = runner.run_cycle()  # detect -> first-contact ask, NO assess
            self.assertEqual(s1["detected"], 1)
            self.assertEqual(s1["asked"], 1)
            self.assertEqual(
                analyzer.assess_calls, 0,
                "first contact must not call assess_clarity",
            )
            self.assertEqual(analyzer.question_calls, 1)

            issue = store.open_issues()[0]
            chat.inject(STAFF_ID, "Jane on payments owns it.", thread_id=issue.thread_id)

            runner.run_cycle()  # a reply exists -> not first contact -> assess runs
            self.assertGreaterEqual(
                analyzer.assess_calls, 1,
                "a reply must re-enable assess_clarity",
            )


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


class RestartReplyRecoveryTest(unittest.TestCase):
    """A reply that arrives while the bot is *down* must still be captured after a
    restart. The working conversation is rebuilt from only *unseen* messages, so
    the already-seen bot question (the reply anchor) is absent — `_new_replies`
    falls back to the anchor's persisted `create_time` instead of idling the live
    clarification straight to stale (HIGH-3)."""

    def test_reply_after_restart_is_recovered_not_idled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, STALE_AFTER_IDLE_CYCLES=5, MAX_CLARIFY_ROUNDS=3)
            chat = FakeChatClient(me=BOT_ID)
            chat.inject(STAFF_ID, SEED_TEXT)

            # --- session 1: detect + ask, then *fetch* the bot question (so it
            # lands in `seen`), with no reply yet. ---
            store1 = IssueStore(config.STATE_FILE)
            spy1 = _ClaritySpyAnalyzer(MockLLM(), retriever=None, top_k=0)
            runner1 = Runner(chat, spy1, store1, config)
            runner1.run_cycle()                       # detect -> first-contact ask
            issue1 = store1.open_issues()[0]
            issue_id = issue1.id
            thread_id = issue1.thread_id
            self.assertTrue(
                issue1.last_bot_create_time,
                "the bot question's create_time must be persisted as the anchor",
            )
            runner1.run_cycle()                       # fetch the bot question -> seen; idle

            # A staff reply arrives *now* (between sessions).
            chat.inject(STAFF_ID, "Jane on payments owns it.", thread_id=thread_id)

            # --- restart: a brand-new store + runner (empty working view). ---
            store2 = IssueStore(config.STATE_FILE)
            spy2 = _ClaritySpyAnalyzer(MockLLM(), retriever=None, top_k=0)
            runner2 = Runner(chat, spy2, store2, config)
            runner2.run_cycle()

            issue2 = next(i for i in store2.all_issues() if i.id == issue_id)
            self.assertGreaterEqual(
                spy2.assess_calls, 1,
                "the reply must be seen (else the idle/first-contact path runs and "
                "assess_clarity is never called — the pre-fix behavior)",
            )
            self.assertTrue(issue2.qa, "the staff reply must be captured as Q&A")
            self.assertIn("Jane", " ".join(p.text for p in issue2.qa))
            self.assertNotEqual(
                issue2.status, Status.STALE, "a recovered reply must not go stale"
            )


class RunForeverResilienceTest(unittest.TestCase):
    """`run_forever` must outlive a single cycle's failure: a transient error is
    logged and swallowed so the daemon keeps polling; only `BaseException`
    (KeyboardInterrupt/SystemExit) breaks the loop for a clean shutdown (HIGH-2)."""

    def test_failed_cycle_is_swallowed_then_loop_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, POLL_INTERVAL_SECONDS=1)
            runner = Runner(FakeChatClient(me=BOT_ID), _analyzer(),
                            IssueStore(config.STATE_FILE), config)

            calls = {"n": 0}

            def flaky_cycle():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient API blip after retries")
                raise KeyboardInterrupt  # break out of the otherwise-infinite loop

            runner.run_cycle = flaky_cycle  # type: ignore[method-assign]

            with mock.patch("time.sleep") as sleep, \
                    mock.patch("traceback.print_exc") as print_exc, \
                    mock.patch("sys.stderr"):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run_forever()

            self.assertEqual(calls["n"], 2, "loop must run a second cycle after a failure")
            print_exc.assert_called_once()       # the failure was logged
            sleep.assert_called_once()           # slept once before retrying


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


class CursorAnchorTest(unittest.TestCase):
    """`_cursor_anchor` must never persist a message *resource name* as the
    cursor `since` — it feeds straight into the Chat `createTime > "{since}"`
    filter, where a `spaces/…/messages/…` value yields HTTP 400 (review MED)."""

    def test_uses_create_time_when_present(self) -> None:
        m = _msg("spaces/x/messages/m1", "spaces/x/threads/t1", STAFF_ID, "hi",
                 t="2026-06-13T10:00:00.000000Z")
        self.assertEqual(
            Runner._cursor_anchor(m, prev="2026-01-01T00:00:00Z"),
            "2026-06-13T10:00:00.000000Z",
        )

    def test_empty_create_time_falls_back_to_prev_not_message_name(self) -> None:
        m = _msg("spaces/x/messages/m1", "spaces/x/threads/t1", STAFF_ID, "hi", t="")
        anchor = Runner._cursor_anchor(m, prev="2026-01-01T00:00:00Z")
        self.assertEqual(anchor, "2026-01-01T00:00:00Z")
        self.assertNotIn("messages/", anchor or "")

    def test_no_create_time_no_prev_is_none(self) -> None:
        m = _msg("spaces/x/messages/m1", "spaces/x/threads/t1", STAFF_ID, "hi", t="")
        self.assertIsNone(Runner._cursor_anchor(m, prev=None))


class LockOwnershipTest(unittest.TestCase):
    """`_release_lock` must only delete a lock it owns (PID match), so a process
    that wakes after its lock was reclaimed can't unlink another runner's lock
    (review MED)."""

    def test_release_removes_lock_with_our_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "x.lock")
            self.assertTrue(_acquire_lock(lock))  # writes our PID
            _release_lock(lock)
            self.assertFalse(os.path.exists(lock))

    def test_release_leaves_foreign_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "x.lock")
            with open(lock, "w", encoding="ascii") as fh:
                fh.write(str(os.getpid() + 1))  # someone else's PID
            _release_lock(lock)
            self.assertTrue(os.path.exists(lock))

    def test_release_leaves_garbled_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "x.lock")
            with open(lock, "w", encoding="ascii") as fh:
                fh.write("")  # empty/garbled — not provably ours
            _release_lock(lock)
            self.assertTrue(os.path.exists(lock))


class RunOnceLockTest(unittest.TestCase):
    """`--once` (via `run_once`) takes the single-runner lock so a manual cycle
    can't race a running daemon (or a second `--once`) on the state file, and
    releases it afterward (review MED)."""

    def _runner(self, tmp: str) -> Runner:
        config = _config(tmp)
        return Runner(FakeChatClient(me=BOT_ID), _analyzer(),
                      IssueStore(config.STATE_FILE), config)

    def test_run_once_refuses_when_lock_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(tmp)
            lock = runner._lock_path()
            self.assertTrue(_acquire_lock(lock))  # a live "daemon" holds it
            try:
                with self.assertRaises(RuntimeError):
                    runner.run_once()
                # The holder's lock must survive the refused run.
                self.assertTrue(os.path.exists(lock))
            finally:
                _release_lock(lock)

    def test_run_once_acquires_runs_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(tmp)
            summary = runner.run_once()
            self.assertIsInstance(summary, dict)
            self.assertFalse(os.path.exists(runner._lock_path()))


if __name__ == "__main__":
    unittest.main()
