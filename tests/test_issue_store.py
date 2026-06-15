"""Tests for the persistent `IssueStore` (§12 + §6).

Covers the agent's durable memory: fingerprint dedup/merge on `upsert`, status
transitions + round counting, tombstone suppression of re-raised closed issues,
cursor + bot-identity round-trips, and atomic save/reload into a fresh store
preserving the whole `AgentState`. Stdlib `unittest` only; no network, no real
Google/OpenRouter. Each test uses its own temp `STATE_FILE`.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from gchat_agent.agent.state import TOMBSTONED, IssueStore
from gchat_agent.models import Issue, QAPair, Severity, Status, issue_fingerprint


def _make_issue(
    *,
    thread_id: str = "spaces/FAKE/threads/t1",
    root_message_id: str = "spaces/FAKE/messages/m1",
    category: str = "billing",
    title: str = "Payouts stuck",
    summary: str = "Withdrawals are not completing for VIP players.",
    status: Status = Status.OPEN,
    severity: Severity = Severity.HIGH,
    source_message_ids: list[str] | None = None,
    missing_info: list[str] | None = None,
    updated_at: str | None = None,
) -> Issue:
    """Build an `Issue` with a real fingerprint (analyzer sets id == fingerprint)."""
    fp = issue_fingerprint(thread_id, root_message_id, category)
    return Issue(
        id=fp,
        fingerprint=fp,
        title=title,
        summary=summary,
        category=category,
        severity=severity,
        status=status,
        thread_id=thread_id,
        root_message_id=root_message_id,
        source_message_ids=list(source_message_ids or [root_message_id]),
        missing_info=list(missing_info or []),
        updated_at=updated_at,
    )


class IssueStoreDedupTest(unittest.TestCase):
    """`upsert` dedup/merge by fingerprint (§6)."""

    def test_same_fingerprint_twice_is_one_issue(self) -> None:
        store = IssueStore(state_file="/unused")  # no I/O in this test
        first = _make_issue(source_message_ids=["m1"])
        second = _make_issue(source_message_ids=["m1", "m2"])
        self.assertEqual(first.fingerprint, second.fingerprint)

        a = store.upsert(first)
        b = store.upsert(second)

        # Second upsert merges into the first; only one tracked issue.
        self.assertIs(a, first)
        self.assertIs(b, first)
        self.assertEqual(len(store.all_issues()), 1)
        self.assertEqual(len(store.open_issues()), 1)
        self.assertIs(store.get(first.fingerprint), first)

    def test_new_source_ids_are_merged_uniquely(self) -> None:
        store = IssueStore(state_file="/unused")
        store.upsert(_make_issue(source_message_ids=["m1"]))
        merged = store.upsert(
            _make_issue(source_message_ids=["m1", "m2", "m3"], missing_info=["owner?"])
        )

        assert merged is not None  # not tombstoned
        # New ids appended, existing kept, order preserved, no duplicates.
        self.assertEqual(merged.source_message_ids, ["m1", "m2", "m3"])
        self.assertEqual(merged.missing_info, ["owner?"])

    def test_distinct_fingerprints_are_separate_issues(self) -> None:
        store = IssueStore(state_file="/unused")
        # Different threads + unrelated titles/summaries so neither the
        # fingerprint nor the title/summary similarity tie-breaker merges them.
        store.upsert(
            _make_issue(
                thread_id="spaces/FAKE/threads/t1",
                category="billing",
                title="Payouts stuck",
                summary="Withdrawals are not completing for VIP players.",
            )
        )
        store.upsert(
            _make_issue(
                thread_id="spaces/FAKE/threads/t2",
                category="compliance",
                title="KYC document upload broken",
                summary="New signups cannot submit identity verification photos.",
            )
        )
        self.assertEqual(len(store.open_issues()), 2)

    def test_merge_keeps_freshest_updated_at(self) -> None:
        store = IssueStore(state_file="/unused")
        target = store.upsert(_make_issue(updated_at="2026-01-01T00:00:00Z"))
        assert target is not None
        store.upsert(_make_issue(updated_at="2026-02-01T00:00:00Z"))
        self.assertEqual(target.updated_at, "2026-02-01T00:00:00Z")
        # Older timestamp must not clobber the fresher one.
        store.upsert(_make_issue(updated_at="2025-12-01T00:00:00Z"))
        self.assertEqual(target.updated_at, "2026-02-01T00:00:00Z")


class IssueStoreStatusTest(unittest.TestCase):
    """Status transitions + round counting drive the open/closed working set."""

    def test_open_then_closed_leaves_open_set(self) -> None:
        store = IssueStore(state_file="/unused")
        issue = store.upsert(_make_issue())
        assert issue is not None
        self.assertEqual(store.open_issues(), [issue])

        # clarifying is still "open"; resolved/stale leave the working set.
        issue.status = Status.CLARIFYING
        self.assertEqual(store.open_issues(), [issue])

        issue.status = Status.RESOLVED
        self.assertEqual(store.open_issues(), [])
        # Closed issue is retained for history/reporting.
        self.assertEqual(store.all_issues(), [issue])

        issue.status = Status.STALE
        self.assertEqual(store.open_issues(), [])

    def test_round_counting_increments(self) -> None:
        store = IssueStore(state_file="/unused")
        issue = store.upsert(_make_issue())
        assert issue is not None
        self.assertEqual(issue.rounds, 0)
        for expected in (1, 2, 3):
            issue.rounds += 1
            issue.questions_asked.append(f"Q{expected}?")
            self.assertEqual(issue.rounds, expected)
        self.assertEqual(len(issue.questions_asked), 3)

    def test_closed_issue_not_merged_into(self) -> None:
        store = IssueStore(state_file="/unused")
        issue = store.upsert(_make_issue(source_message_ids=["m1"]))
        assert issue is not None
        issue.status = Status.RESOLVED  # closed but NOT tombstoned

        # A re-detection with the same fingerprint should create a new open issue
        # rather than merge into the closed one (closed issues are out of scope).
        again = store.upsert(_make_issue(source_message_ids=["m9"]))
        self.assertIsNot(again, issue)
        self.assertEqual(len(store.all_issues()), 2)
        self.assertEqual(store.open_issues(), [again])


class IssueStoreTombstoneTest(unittest.TestCase):
    """Tombstone → not re-raised from the same root (§6)."""

    def test_tombstone_marks_fingerprint(self) -> None:
        store = IssueStore(state_file="/unused")
        issue = store.upsert(_make_issue())
        assert issue is not None
        self.assertFalse(store.is_tombstoned(issue.fingerprint))

        issue.status = Status.RESOLVED
        store.tombstone(issue)
        self.assertTrue(store.is_tombstoned(issue.fingerprint))

    def test_tombstone_is_idempotent(self) -> None:
        store = IssueStore(state_file="/unused")
        issue = store.upsert(_make_issue())
        assert issue is not None
        store.tombstone(issue)
        store.tombstone(issue)
        self.assertEqual(store.state.tombstones.count(issue.fingerprint), 1)

    def test_reupsert_of_tombstoned_is_suppressed(self) -> None:
        store = IssueStore(state_file="/unused")
        issue = store.upsert(_make_issue(source_message_ids=["m1"]))
        assert issue is not None
        issue.status = Status.RESOLVED
        store.tombstone(issue)

        # Re-detection of the same fingerprint must not be re-raised.
        result = store.upsert(_make_issue(source_message_ids=["m1", "m2"]))
        self.assertIs(result, TOMBSTONED)
        self.assertIsNone(result)
        # Nothing new added; the original (closed) issue is unchanged.
        self.assertEqual(len(store.all_issues()), 1)
        self.assertEqual(store.all_issues()[0].source_message_ids, ["m1"])

    def test_category_drift_after_tombstone_is_suppressed(self) -> None:
        """A resolved+tombstoned issue re-detected under a *drifted* category
        (so a fresh fingerprint the exact-match tombstone set misses) must still
        be suppressed via the closed-issue similarity guard (review MED)."""
        store = IssueStore(state_file="/unused")
        issue = store.upsert(_make_issue(category="billing"))
        assert issue is not None
        issue.status = Status.RESOLVED
        store.tombstone(issue)

        # Same thread/root/title/summary, but the LLM flipped the category, so
        # the fingerprint genuinely differs and isn't in the tombstone set.
        drifted = _make_issue(category="payments")
        self.assertNotEqual(drifted.fingerprint, issue.fingerprint)
        self.assertFalse(store.is_tombstoned(drifted.fingerprint))

        result = store.upsert(drifted)
        self.assertIs(result, TOMBSTONED)
        self.assertEqual(len(store.all_issues()), 1)

    def test_distinct_issue_same_thread_after_tombstone_still_raised(self) -> None:
        """The closed-similarity guard must not over-suppress: a genuinely new
        issue in the same thread (distinct title/summary, low overlap) is still
        tracked even though a different issue there was tombstoned."""
        store = IssueStore(state_file="/unused")
        issue = store.upsert(
            _make_issue(category="billing", title="Payouts stuck",
                        summary="Withdrawals are not completing for VIP players.")
        )
        assert issue is not None
        issue.status = Status.RESOLVED
        store.tombstone(issue)

        other = store.upsert(
            _make_issue(
                category="auth",
                root_message_id="spaces/FAKE/messages/m2",
                title="Login page times out",
                summary="Players cannot sign in; the auth service latency spiked.",
            )
        )
        self.assertIsNotNone(other)
        self.assertIsNot(other, TOMBSTONED)
        self.assertEqual(len(store.all_issues()), 2)


class IssueStoreCursorIdentityTest(unittest.TestCase):
    """Cursor + bot-identity get/set round-trips (§5.4/§5.7)."""

    def test_cursor_defaults_then_round_trip(self) -> None:
        store = IssueStore(state_file="/unused")
        name, seen = store.get_cursor()
        self.assertIsNone(name)
        self.assertEqual(seen, [])

        store.set_cursor("spaces/FAKE/messages/m42", ["m40", "m41", "m42"])
        name, seen = store.get_cursor()
        self.assertEqual(name, "spaces/FAKE/messages/m42")
        self.assertEqual(seen, ["m40", "m41", "m42"])
        # get_cursor returns a copy, not the live list.
        seen.append("mutated")
        self.assertEqual(store.get_cursor()[1], ["m40", "m41", "m42"])

    def test_cursor_seen_ids_deduped_and_bounded(self) -> None:
        store = IssueStore(state_file="/unused")
        store.set_cursor("c", ["a", "a", "b", "", "c", "b"])
        # Falsy entries dropped; order-preserving dedup.
        self.assertEqual(store.get_cursor()[1], ["a", "b", "c"])

        big = [f"m{i}" for i in range(700)]
        store.set_cursor("c", big)
        _, bounded = store.get_cursor()
        self.assertEqual(len(bounded), 500)  # _MAX_SEEN_IDS
        self.assertEqual(bounded[0], "m200")  # most-recent tail kept
        self.assertEqual(bounded[-1], "m699")

    def test_bot_user_id_round_trip(self) -> None:
        store = IssueStore(state_file="/unused")
        self.assertIsNone(store.get_bot_user_id())
        store.set_bot_user_id("users/12345")
        self.assertEqual(store.get_bot_user_id(), "users/12345")
        # Empty string normalizes back to None.
        store.set_bot_user_id("")
        self.assertIsNone(store.get_bot_user_id())


class IssueStorePersistenceTest(unittest.TestCase):
    """Atomic save → reload into a fresh store preserves everything."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self._tmpdir.name, "state", "issues.json")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_save_creates_dir_atomically_no_temp_leftovers(self) -> None:
        store = IssueStore(state_file=self.state_file)
        store.upsert(_make_issue())
        store.save()  # parent dir does not exist yet -> mkdir -p

        self.assertTrue(os.path.exists(self.state_file))
        # No stray temp files from the atomic write left behind.
        leftovers = [
            f for f in os.listdir(os.path.dirname(self.state_file))
            if f.startswith(".issues-")
        ]
        self.assertEqual(leftovers, [])

    def test_save_then_reload_preserves_full_state(self) -> None:
        store = IssueStore(state_file=self.state_file)
        issue = store.upsert(
            _make_issue(source_message_ids=["m1", "m2"], missing_info=["owner?"])
        )
        assert issue is not None
        issue.status = Status.CLARIFYING
        issue.rounds = 2
        issue.idle_cycles = 1
        issue.questions_asked = ["Who owns this?", "What is the deadline?"]
        issue.qa = [QAPair(question="Who owns this?", answer_message_ids=["m3"], text="Ops")]
        issue.last_bot_message_id = "spaces/FAKE/messages/m99"

        # A second, resolved-and-tombstoned issue in another thread.
        closed = store.upsert(
            _make_issue(thread_id="spaces/FAKE/threads/t2", category="compliance")
        )
        assert closed is not None
        closed.status = Status.RESOLVED
        store.tombstone(closed)

        store.set_cursor("spaces/FAKE/messages/m99", ["m97", "m98", "m99"])
        store.set_bot_user_id("users/bot-007")
        store.save()

        # Reload into a brand-new store with the same path.
        reloaded = IssueStore(state_file=self.state_file)
        reloaded.load()

        # Cursor + identity survive.
        name, seen = reloaded.get_cursor()
        self.assertEqual(name, "spaces/FAKE/messages/m99")
        self.assertEqual(seen, ["m97", "m98", "m99"])
        self.assertEqual(reloaded.get_bot_user_id(), "users/bot-007")

        # Both issues survive in order; open set excludes the closed one.
        self.assertEqual(len(reloaded.all_issues()), 2)
        self.assertEqual(len(reloaded.open_issues()), 1)

        # Tombstone survives -> closed issue is not re-raised.
        self.assertTrue(reloaded.is_tombstoned(closed.fingerprint))
        suppressed = reloaded.upsert(
            _make_issue(thread_id="spaces/FAKE/threads/t2", category="compliance")
        )
        self.assertIs(suppressed, TOMBSTONED)

        # The open issue's full state round-tripped, fingerprint index rebuilt.
        again = reloaded.get(issue.fingerprint)
        assert again is not None
        self.assertEqual(again.status, Status.CLARIFYING)
        self.assertEqual(again.rounds, 2)
        self.assertEqual(again.idle_cycles, 1)
        self.assertEqual(again.source_message_ids, ["m1", "m2"])
        self.assertEqual(again.missing_info, ["owner?"])
        self.assertEqual(again.questions_asked, ["Who owns this?", "What is the deadline?"])
        self.assertEqual(again.last_bot_message_id, "spaces/FAKE/messages/m99")
        self.assertEqual(len(again.qa), 1)
        self.assertEqual(again.qa[0].question, "Who owns this?")
        self.assertEqual(again.qa[0].answer_message_ids, ["m3"])
        self.assertEqual(again.qa[0].text, "Ops")

    def test_load_missing_file_is_empty_state(self) -> None:
        store = IssueStore(state_file=os.path.join(self._tmpdir.name, "nope.json"))
        store.load()
        self.assertEqual(store.all_issues(), [])
        self.assertEqual(store.get_cursor(), (None, []))
        self.assertIsNone(store.get_bot_user_id())

    def test_load_corrupt_file_is_empty_state(self) -> None:
        corrupt = os.path.join(self._tmpdir.name, "corrupt.json")
        with open(corrupt, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        store = IssueStore(state_file=corrupt)
        store.load()  # corrupt -> fresh empty state, no exception
        self.assertEqual(store.all_issues(), [])


if __name__ == "__main__":
    unittest.main()
