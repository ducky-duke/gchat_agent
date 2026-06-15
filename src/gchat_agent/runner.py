"""The orchestration loop + provider/adapter wiring (§4 / §5.7 / §6).

`Runner.run_cycle` is one iteration of the agent loop:

1. fetch new messages since the poll cursor (no history backfill on first run),
2. detect candidate issues over the recent transcript with the bot's *own*
   messages dropped (never a `sender_type` rule — staff post as HUMAN),
3. for each open issue, capture any new replies as Q&A, then either re-ask
   (under `MAX_CLARIFY_ROUNDS`, gated on a fresh reply — anti-spam), resolve
   (write the report once + post a confirmation), or go stale.

State is persisted atomically through the `IssueStore`; `run_forever` enforces a
single active runner via a lock file so two pollers can't race the cursor.

Stdlib only. The LLM / observability third-party deps stay behind their existing
lazy modules — this file imports neither `openai` nor `langfuse` directly.
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from . import observability
from .agent import report as report_mod
from .agent.analyzer import Analyzer
from .agent.state import IssueStore
from .config import Config
from .models import Conversation, Message, QAPair, Status

if TYPE_CHECKING:
    from .chat.base import ChatClient
    from .llm.base import LLMClient

# A generous cap on how many recent ids the cursor's `seen` set carries so an
# equal-`createTime` boundary message is never reprocessed (§5.4). The store
# bounds the persisted set too; this just limits what we hand it each cycle.
_SEEN_WINDOW = 500

# Re-fetch a small window before the cursor boundary so an equal-`createTime`
# message at the boundary is never dropped by the adapter's strict `createTime >`
# filter (§5.4/§7); the cursor's `seen` set dedups the replayed messages.
_CURSOR_SKEW_SECONDS = 2


def _now() -> str:
    """A UTC RFC-3339 timestamp string (consistent `now` across the cycle)."""
    return datetime.now(timezone.utc).isoformat()


def _mention(reporter_id: str | None) -> str:
    """A Google Chat text @mention for a `users/<id>` resource name.

    Chat renders `<users/{id}>` in a message's `text` as a real user mention
    (see docs/google_chat … spaces.messages `formattedText`), which notifies the
    user even when they aren't following the thread. Empty string if unknown."""
    return f"<{reporter_id}>" if reporter_id else ""


def _minus_seconds(ts: str, seconds: int) -> str:
    """Shift an RFC-3339 timestamp earlier by `seconds` (best-effort). Widens the
    fetch boundary so equal-`createTime` messages aren't dropped by a strict
    `createTime >` filter; the seen-id set dedups the small replay. Returns `ts`
    unchanged if it can't be parsed."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ts
    return (dt - timedelta(seconds=seconds)).isoformat()


class Runner:
    """One issue-spotter bot driving a `ChatClient` + `Analyzer` + `IssueStore`."""

    def __init__(
        self,
        chat: "ChatClient",
        analyzer: Analyzer,
        store: IssueStore,
        config: Config,
        reports_dir: str | None = None,
        llm: "Optional[LLMClient]" = None,
    ) -> None:
        self.chat = chat
        self.analyzer = analyzer
        self.store = store
        self.config = config
        self.reports_dir = reports_dir or config.REPORTS_DIR
        # The report builder wants an LLM to tighten prose; reuse the analyzer's
        # unless one is injected. `None` is fine — the builder degrades.
        self._llm: "Optional[LLMClient]" = llm or getattr(analyzer, "llm", None)
        # The working conversation accumulates fetched messages across cycles so
        # threads keep their full context (detection is still windowed).
        self._conversation = Conversation()

    # --- one orchestration iteration ---------------------------------------
    def run_cycle(self) -> dict:
        """Run one fetch → detect → clarify/resolve iteration; return a summary."""
        self.store.load()
        own_id = self._resolve_own_id()

        fetched = self._fetch_new_messages()
        detected = self._detect(own_id)
        asked, resolved, stale, escalated = self._process_open_issues(own_id)

        # Persist a freshly-learned bot id (the client may have learned its own
        # users/<id> from a post this cycle) so a restart self-filters at once.
        live = self.chat.me()
        if live and not self.store.get_bot_user_id():
            self.store.set_bot_user_id(live)

        self.store.save()
        return {
            "fetched": fetched,
            "detected": detected,
            "asked": asked,
            "resolved": resolved,
            "stale": stale,
            "escalated": escalated,
        }

    # --- step 1: identity ---------------------------------------------------
    def _resolve_own_id(self) -> str | None:
        """The bot's own `users/<id>` for self-filtering. Prefer the live client;
        persist it the first time the client knows it."""
        live = self.chat.me()
        if live is not None:
            if not self.store.get_bot_user_id():
                self.store.set_bot_user_id(live)
            return live
        return self.store.get_bot_user_id()

    # --- step 2: fetch ------------------------------------------------------
    def _fetch_new_messages(self) -> int:
        """Fetch messages after the cursor, drop already-seen ids, append to the
        working conversation, and advance the cursor. On a true first run (no
        cursor, no backfill) seed `since` to *now* so there is no history
        backfill (§5.1)."""
        cursor_name, seen = self.store.get_cursor()
        seen_set = set(seen)
        since = self._since(cursor_name)

        fetched = self.chat.fetch_messages(since)
        new = [m for m in fetched if m.id and m.id not in seen_set]

        for m in new:
            self._conversation.add(m)

        if new:
            latest = new[-1]
            updated_seen = list(seen) + [m.id for m in new]
            self.store.set_cursor(
                self._cursor_anchor(latest, cursor_name),
                updated_seen[-_SEEN_WINDOW:],
            )
        elif cursor_name is None and not seen:
            # True first run with nothing new: pin the cursor to "now" so the
            # next cycle only sees genuinely new traffic (no history backfill).
            self.store.set_cursor(_now(), [])
        return len(new)

    def _since(self, cursor_name: str | None) -> str | None:
        """The `fetch_messages` boundary, in precedence order: the latest
        create_time we hold this process, else the persisted cursor pin (so a
        restart resumes where we left off — NOT the backfill), else the configured
        backfill (true first run only), else *now* (no history backfill). The
        chosen boundary is shifted back by `_CURSOR_SKEW_SECONDS` so an
        equal-timestamp message at the boundary isn't dropped (the seen-id set
        dedups the replay)."""
        latest_time = self._latest_create_time()
        if latest_time:
            return _minus_seconds(latest_time, _CURSOR_SKEW_SECONDS)
        if cursor_name:  # persisted pin wins after a restart, before any backfill
            return _minus_seconds(cursor_name, _CURSOR_SKEW_SECONDS)
        if self.config.POLL_BACKFILL_SINCE:  # only on a true first run (no cursor)
            return self.config.POLL_BACKFILL_SINCE
        return _now()  # first run, no backfill

    def _latest_create_time(self) -> str | None:
        """The latest non-empty create_time among working-conversation messages
        (RFC-3339 sorts lexicographically), or None."""
        times = [m.create_time for m in self._conversation.messages if m.create_time]
        return max(times) if times else None

    @staticmethod
    def _cursor_anchor(latest: Message, prev: str | None) -> str | None:
        """The cursor's stored `name`: the latest message's RFC-3339 `create_time`
        (a valid `since` for the next fetch), else keep the previous valid anchor.

        Never the message *resource name*: `_since` feeds this value straight
        into the Chat `createTime > "{since}"` filter, where a
        `spaces/…/messages/…` value yields HTTP 400 / a silent mis-fetch. Falling
        back to `prev` (a known-good timestamp, possibly None) only widens the
        next fetch slightly; the seen-id set dedups the small replay."""
        return latest.create_time or prev

    # --- step 3 + 4: detection ----------------------------------------------
    def _detect(self, own_id: str | None) -> int:
        """Detect candidates over the recent window with the bot's own messages
        dropped, then upsert all that aren't tombstoned."""
        window = self._conversation.tail(self.config.DETECT_WINDOW_MESSAGES)
        detection_conv = window.without_sender(own_id) if own_id else window

        detected = 0
        for candidate in self.analyzer.detect_issues(detection_conv):
            if self.store.is_tombstoned(candidate.fingerprint):
                continue
            self.store.upsert(candidate)
            detected += 1
        return detected

    # --- step 5: the clarify / resolve loop ---------------------------------
    def _process_open_issues(self, own_id: str | None) -> tuple[int, int, int, int]:
        """Drive each open issue one step; return (asked, resolved, stale, escalated)."""
        asked = resolved = stale = escalated = 0
        for issue in self.store.open_issues():
            with observability.trace("issue", issue_id=issue.id):
                outcome = self._step_issue(issue, own_id)
            if outcome == "asked":
                asked += 1
            elif outcome == "resolved":
                resolved += 1
            elif outcome == "stale":
                stale += 1
            elif outcome == "escalated":
                escalated += 1
        return asked, resolved, stale, escalated

    def _step_issue(self, issue, own_id: str | None) -> str | None:
        """Advance one open issue by a single cycle. Returns a short outcome tag
        ("asked"|"resolved"|"stale"|"escalated"|None) for the summary counts.

        `thread_conv` is the issue's *effective* conversation — its own thread,
        the nudge thread the escalation opened, and the reporter's answers that
        landed in some other fresh thread (§ out-of-thread capture) — so a reply
        typed in the nudge thread or at the space top level still resolves the
        issue. Posting still targets the real thread (every message in the
        effective view carries the issue's thread_id)."""
        thread_conv = self._effective_conversation(issue, own_id)
        replies = self._new_replies(issue, thread_conv, own_id)

        # Follow the reporter: the next question / confirmation is posted into
        # whatever thread they last replied in (the issue thread, the nudge
        # thread, or any other). The replies in `thread_conv` are re-tagged copies,
        # so resolve the latest reply's REAL thread from `self._conversation`.
        if replies:
            by_id = {m.id: m for m in self._conversation.messages}
            latest = max(replies, key=lambda m: (m.create_time or "", m.id))
            real = by_id.get(latest.id)
            if real and real.thread_id:
                issue.active_thread_id = real.thread_id

        # Capture the latest Q→A before re-assessing (report evidence, §6).
        # Dedupe by message id across prior Q&A so a reply isn't recorded twice if
        # it is re-seen on a later cycle (e.g. an `_ask` that produced no questions
        # left the anchor unadvanced, so `_new_replies` returns the same message).
        if replies and issue.questions_asked:
            already = {mid for pair in issue.qa for mid in pair.answer_message_ids}
            fresh = [r for r in replies if r.id not in already]
            if fresh:
                issue.qa.append(
                    QAPair(
                        question=issue.questions_asked[-1],
                        answer_message_ids=[r.id for r in fresh],
                        text=" ".join(r.text for r in fresh),
                    )
                )

        # Anti-spam: while clarifying with no fresh reply, wait (don't re-ask).
        if issue.status == Status.CLARIFYING and not replies:
            issue.idle_cycles += 1
            issue.updated_at = _now()
            # Before giving up, surface the unanswered question at the space top
            # level with an @mention — the reporter may simply not be watching the
            # thread — and grant a fresh idle budget. Escalates at most once.
            if self._should_escalate(issue):
                return self._escalate(issue)
            if issue.idle_cycles >= self.config.STALE_AFTER_IDLE_CYCLES:
                return self._mark_stale(issue)
            return None

        # First contact: a freshly detected issue (no questions asked, no replies)
        # is definitionally not "clear" yet — skip the `assess_clarity` LLM call and
        # open with the first questions straight away. Saves one frontier-model
        # round-trip on every issue's first reply (the dominant latency cost). Only
        # when we will actually ask (rounds < cap), so the degenerate cap=0 config
        # still falls through to the original assess→stale path below.
        if (
            not issue.questions_asked
            and not replies
            and issue.rounds < self.config.MAX_CLARIFY_ROUNDS
        ):
            if self._ask(issue, thread_conv, []):
                return "asked"
            # Model produced no questions (rare transient empty) — idle and retry
            # rather than assessing an empty discussion; the idle cap still bounds it.
            issue.idle_cycles += 1
            issue.updated_at = _now()
            if issue.idle_cycles >= self.config.STALE_AFTER_IDLE_CYCLES:
                return self._mark_stale(issue)
            return None

        assessment = self.analyzer.assess_clarity(issue, thread_conv)
        if (
            assessment.is_clear
            and assessment.confidence >= self.config.RESOLVE_CONFIDENCE_THRESHOLD
            and not assessment.missing_info
        ):
            self._resolve(issue, thread_conv)
            return "resolved"

        if issue.rounds < self.config.MAX_CLARIFY_ROUNDS:
            if self._ask(issue, thread_conv, assessment.missing_info):
                return "asked"
            # No questions this cycle — usually a transient empty LLM reply, not a
            # genuinely unanswerable issue. Treat as idle and retry next cycle
            # rather than staling at once; the idle cap still bounds it so a truly
            # stuck issue eventually goes stale instead of spinning forever.
            issue.idle_cycles += 1
            issue.updated_at = _now()
            if issue.idle_cycles >= self.config.STALE_AFTER_IDLE_CYCLES:
                return self._mark_stale(issue)
            return None

        return self._mark_stale(issue)

    def _mark_stale(self, issue) -> str:
        """Transition an issue to STALE and tombstone its fingerprint so the same
        closed issue is not re-detected/re-raised from its root (§6) — the dedup
        set covers resolved *and* stale fingerprints."""
        issue.status = Status.STALE
        issue.updated_at = _now()
        self.store.tombstone(issue)
        return "stale"

    # --- escalation ---------------------------------------------------------
    def _should_escalate(self, issue) -> bool:
        """Whether to post a top-level @mention nudge this cycle: an idle
        CLARIFYING issue (the caller already incremented `idle_cycles`) that has
        a known reporter, hasn't been escalated yet, and has reached the
        escalation threshold but not yet the stale threshold. Disabled when
        `ESCALATE_AFTER_IDLE_CYCLES <= 0`."""
        cfg = self.config
        return (
            not issue.escalated
            and cfg.ESCALATE_AFTER_IDLE_CYCLES > 0
            and bool(issue.reporter_id)
            and bool(issue.questions_asked)
            and issue.idle_cycles >= cfg.ESCALATE_AFTER_IDLE_CYCLES
            and issue.idle_cycles < cfg.STALE_AFTER_IDLE_CYCLES
        )

    def _escalate(self, issue) -> str:
        """Post one top-level (NOT threaded) @mention nudge so the reporter, who
        may not be watching the thread, is pulled back to the unanswered
        question; then keep the issue CLARIFYING with a fresh idle budget.

        Idempotent: a stable `request_id` means a crash between the post and the
        state save never double-nudges (the same post is returned), and
        `escalated` (persisted) bars a second escalation once saved."""
        title = issue.title or "an issue"
        text = (
            f"{_mention(issue.reporter_id)} I asked a couple of clarifying "
            f"questions about “{title}” in a thread above — could "
            f"you take a look when you get a chance? \U0001f64f"
        ).strip()
        posted = self.chat.post_message(
            text, thread_id=None, request_id=f"client-issue-{issue.id}-escalate"
        )
        # Remember the thread the nudge opened: it is a second unambiguous home for
        # this issue, so a reporter reply there is collected even when they have
        # several open issues (§ out-of-thread capture, section A). Stable across a
        # retry (same `request_id` ⇒ same Message ⇒ same thread_id).
        issue.escalation_thread_id = posted.thread_id or None
        issue.escalated = True
        issue.idle_cycles = 0  # grant a fresh stale window after the nudge
        issue.updated_at = _now()
        return "escalated"

    # --- effective (thread + out-of-thread) conversation --------------------
    def _effective_conversation(self, issue, own_id: str | None) -> Conversation:
        """The issue's own thread PLUS replies that landed outside it but still
        belong to this issue (§ out-of-thread capture). Two sources, in order of
        confidence:

        **(A) The issue's "home" threads beyond its own — an unambiguous source.**
        Two threads belong 1:1 to this issue: the nudge thread the escalation
        opened, and the thread the conversation has since moved to (where the bot
        now follows the reporter — `active_thread_id`). *Any* non-bot reply in
        either attributes cleanly — even when the reporter has several open issues
        — so both are collected without the ambiguity guard below. (This is the
        "reply in the issue thread OR the nudge thread, collect both" contract,
        plus the follow-the-reporter thread.)

        **(B) A reporter reply that landed in some OTHER fresh thread.**
        A top-level reply with no nudge to anchor it can't be tied to a specific
        issue by thread, so this source is conservative:

        - **reporter only** — never other in-thread senders; staff personas reply
          in-thread, so widening the author set would just ingest their unrelated
          top-level chatter;
        - **unambiguous only** — if the reporter has more than one open issue
          awaiting a reply, a bare top-level message can't be attributed to one of
          them; we fall back to in-thread + nudge-thread replies, which are;
        - **newer than the last bot question**, and never from a thread owned by
          another open issue (nor the nudge thread, already covered by A).

        Out-of-thread messages are returned as COPIES re-tagged to the issue's
        thread_id so the analyzer's thread-scoped clarity check treats them as
        part of this discussion; originals in `self._conversation` are untouched,
        and posting always resolves the real thread via `_thread_anchor`."""
        strict = self._conversation.for_thread(issue.thread_id)
        in_thread_ids = {m.id for m in strict.messages}
        extra: list[Message] = []
        seen_extra: set[str] = set()

        # (A) The issue's home threads beyond its own (nudge thread + the thread
        # the conversation moved to): each belongs 1:1 to this issue, so collect
        # every non-bot message in them, re-tagged, with no ambiguity guard (the
        # bot's own posts are dropped as their author == own_id).
        home_threads = {
            t for t in (issue.escalation_thread_id, issue.active_thread_id)
            if t and t != issue.thread_id
        }
        for tid in home_threads:
            for m in self._conversation.for_thread(tid).messages:
                if m.sender == own_id or m.id in in_thread_ids or m.id in seen_extra:
                    continue
                extra.append(replace(m, thread_id=issue.thread_id))
                seen_extra.add(m.id)

        # (B) The reporter's answers in some other fresh thread (guarded).
        cutoff = issue.last_bot_create_time
        reporter = issue.reporter_id
        if cutoff and reporter and reporter != own_id:
            awaiting = [
                i for i in self.store.open_issues()
                if i.reporter_id == reporter and i.last_bot_create_time
            ]
            if len(awaiting) <= 1:  # else ambiguous: which issue does it answer?
                other_threads = {
                    i.thread_id
                    for i in self.store.open_issues()
                    if i.thread_id and i.thread_id != issue.thread_id
                }
                for m in self._conversation.messages:
                    if m.thread_id == issue.thread_id or m.id in in_thread_ids:
                        continue  # already in the strict thread
                    if m.id in seen_extra:
                        continue  # already pulled from a home thread (A)
                    if m.thread_id and (m.thread_id in home_threads or m.thread_id in other_threads):
                        continue  # home thread (A) / another open issue's thread
                    if m.sender != reporter:
                        continue  # only the reporter's own out-of-thread answers
                    if not m.create_time or m.create_time <= cutoff:
                        continue  # not newer than the last bot question
                    extra.append(replace(m, thread_id=issue.thread_id))
                    seen_extra.add(m.id)

        if not extra:
            return strict
        merged = sorted(strict.messages + extra, key=lambda m: (m.create_time or "", m.id))
        return Conversation(messages=merged)

    def _new_replies(self, issue, thread_conv: Conversation, own_id: str | None) -> list[Message]:
        """Messages in the issue's thread after the last bot question, authored by
        anyone but the bot (any sender ≠ bot, §6).

        If the bot has not asked yet (no `last_bot_message_id`) there is no "reply
        since a bot question" to gate on, so return `[]`.

        Normally the anchor message is present in the working view and we take the
        messages strictly after it. After a *restart*, though, the working
        conversation is rebuilt from only the *unseen* messages, so the
        already-seen anchor is absent — falling through to `[]` there would idle a
        live clarification straight to stale. In that one case we fall back to the
        anchor's persisted `create_time` and treat thread messages newer than it as
        the replies. The gate stays conservative: with neither the anchor message
        nor a recorded timestamp, it still returns `[]` and never mistakes
        pre-existing thread text for a fresh reply."""
        anchor = issue.last_bot_message_id
        if not anchor:
            return []
        if anchor in {m.id for m in thread_conv.messages}:
            candidates = thread_conv.after(anchor).messages
        elif issue.last_bot_create_time:
            cutoff = issue.last_bot_create_time
            candidates = [
                m for m in thread_conv.messages
                if m.create_time and m.create_time > cutoff
            ]
        else:
            return []
        if own_id:
            candidates = [m for m in candidates if m.sender != own_id]
        return list(candidates)

    def _ask(self, issue, thread_conv: Conversation, missing_info: list[str]) -> bool:
        """Generate + post the next clarifying-question batch into the thread.
        Returns True if a batch was posted, False if the model produced none.

        `thread_conv` (the effective view) feeds question *generation* only;
        the post is routed to the issue's real thread via `_post_to_thread`."""
        questions = self.analyzer.generate_questions(issue, thread_conv, missing_info)
        if not questions:
            return False
        text = "\n".join(questions)
        request_id = f"client-issue-{issue.id}-r{issue.rounds + 1}"
        posted = self._post_to_thread(issue, text, request_id)

        now = _now()
        issue.questions_asked.append(text)
        issue.last_bot_message_id = posted.id
        # Persist the question's server create_time so a reply is still detectable
        # after a restart (the anchor message itself won't be in the rebuilt view).
        issue.last_bot_create_time = posted.create_time or None
        issue.last_question_at = now
        issue.rounds += 1
        issue.idle_cycles = 0
        issue.status = Status.CLARIFYING
        issue.updated_at = now
        return True

    def _resolve(self, issue, thread_conv: Conversation) -> None:
        """Resolve once: build + write the report, post the confirmation, mark
        resolved, tombstone.

        Idempotency: the whole block is gated on `report_written_at` (persisted
        state) so a state reload can't redo it. Inside the block the file write is
        skipped when `reports/issue-<id>.md` already exists, and the confirmation
        is posted with a stable `request_id` — both individually idempotent — so a
        crash *after* the write but *before* the post still lets the next cycle
        post the confirmation rather than skip it forever (§5.7)."""
        now = _now()
        if not issue.report_written_at:
            report = report_mod.build_resolution_report(issue, self._llm)
            report_path = os.path.join(self.reports_dir, f"issue-{issue.id}.md")
            if not os.path.exists(report_path):
                report_mod.write_report(report, self.reports_dir)
            self._post_to_thread(
                issue,
                report_mod.confirmation_line(report),
                request_id=f"client-issue-{issue.id}-report",
            )
            issue.report_written_at = now
        issue.status = Status.RESOLVED
        issue.updated_at = now
        self.store.tombstone(issue)

    # --- posting helper -----------------------------------------------------
    def _post_to_thread(self, issue, text: str, request_id: str) -> Message:
        """Post `text` into the issue's *active* thread — where the reporter last
        replied (`active_thread_id`, else the issue thread). Prefer a threaded
        reply to a real Message we hold there; fall back to
        `post_message(thread_id=...)` when we have no Message object to reply to.

        The anchor is resolved from `self._conversation` (a real thread), never
        the effective view — a re-tagged out-of-thread copy must never become the
        reply target and redirect the post (§ out-of-thread capture)."""
        anchor = self._thread_anchor(issue)
        if anchor is not None:
            return self.chat.post_reply(anchor, text, request_id=request_id)
        return self.chat.post_message(
            text, thread_id=issue.active_thread_id or issue.thread_id, request_id=request_id
        )

    def _thread_anchor(self, issue) -> Message | None:
        """A real Message in the issue's *active* thread to reply to (where the
        reporter last spoke — `active_thread_id`, else the issue thread): prefer
        the most recent message there, else the issue's root/source message, else
        None. Resolved from `self._conversation` so it is always a genuine
        in-thread parent, never a re-tagged effective-view copy."""
        target = issue.active_thread_id or issue.thread_id
        in_target = self._conversation.for_thread(target)
        if in_target.messages:
            return in_target.messages[-1]
        by_id = {m.id: m for m in self._conversation.messages}
        for mid in (issue.last_bot_message_id, issue.root_message_id, *issue.source_message_ids):
            if mid and mid in by_id:
                return by_id[mid]
        return None

    # --- lock-guarded entrypoints -------------------------------------------
    def _lock_path(self) -> str:
        return self.config.STATE_FILE + ".lock"

    def run_once(self) -> dict:
        """Run exactly one cycle under the single-runner lock, then release it.

        Mirrors `run_forever`'s mutual exclusion so a manual `--once` invocation
        can't race a running daemon (or a second `--once`) on the shared state
        file. Fail-fast: a cycle error propagates to the caller (unlike
        `run_forever`, which logs it and retries). Returns the cycle summary."""
        lock_path = self._lock_path()
        _acquire_lock_or_raise(lock_path)
        try:
            return self.run_cycle()
        finally:
            _release_lock(lock_path)
            observability.flush()

    # --- the long-running loop ----------------------------------------------
    def run_forever(self) -> None:
        """Loop `run_cycle` forever under a single-runner lock, sleeping
        `POLL_INTERVAL_SECONDS` between cycles. Releases the lock on exit and
        flushes observability.

        A single cycle's failure (a network/API/LLM error that survived its own
        retries) is logged and swallowed so one transient hiccup never kills the
        long-running daemon — the loop sleeps and tries again next cycle.
        `KeyboardInterrupt`/`SystemExit` are *not* caught (they subclass
        `BaseException`, not `Exception`), so Ctrl-C still shuts down cleanly via
        the `finally`. `--once` (via `run_once`) keeps its fail-fast behavior."""
        import sys
        import time
        import traceback

        lock_path = self._lock_path()
        _acquire_lock_or_raise(lock_path)
        try:
            while True:
                try:
                    self.run_cycle()
                except Exception:  # noqa: BLE001 — daemon must outlive any one cycle
                    traceback.print_exc()
                    print(
                        "cycle failed (see traceback above); continuing after "
                        f"{max(1, self.config.POLL_INTERVAL_SECONDS)}s",
                        file=sys.stderr,
                    )
                time.sleep(max(1, self.config.POLL_INTERVAL_SECONDS))
        finally:
            _release_lock(lock_path)
            observability.flush()


# --- single-runner lock (stdlib only) ---------------------------------------
def _acquire_lock_or_raise(lock_path: str) -> None:
    """Acquire the single-runner lock or raise — shared by `run_once` and
    `run_forever` so both refuse to run alongside another live runner."""
    if not _acquire_lock(lock_path):
        raise RuntimeError(
            f"another runner holds {lock_path} (a live process). Refusing to "
            f"start a second poller — stop the other one or remove a stale lock."
        )


def _acquire_lock(lock_path: str) -> bool:
    """Create `lock_path` exclusively, writing our PID. If it exists, refuse
    unless it is stale (the recorded PID is no longer alive), in which case we
    reclaim it. Returns True on success."""
    directory = os.path.dirname(lock_path) or "."
    os.makedirs(directory, exist_ok=True)
    if _write_lock(lock_path):
        return True
    # Lock exists — is the holder alive?
    if _lock_is_stale(lock_path):
        try:
            os.unlink(lock_path)
        except OSError:
            return False
        return _write_lock(lock_path)
    return False


def _write_lock(lock_path: str) -> bool:
    """Atomically create the lock file with our PID; False if it already exists."""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


def _lock_is_stale(lock_path: str) -> bool:
    """True if the PID recorded in `lock_path` is no longer a live process (so
    the lock can be reclaimed). A malformed/empty lock is treated as stale."""
    try:
        with open(lock_path, encoding="ascii") as fh:
            pid_text = fh.read().strip()
    except OSError:
        return True
    if not pid_text.isdigit():
        return True
    pid = int(pid_text)
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)  # signal 0: existence check, no signal delivered
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # alive but owned by another user
    except OSError:
        return True
    return False


def _release_lock(lock_path: str) -> None:
    """Remove the lock file, but only if it is *ours* — its recorded PID matches
    `os.getpid()`. Guards against deleting another runner's lock after ours was
    reclaimed as stale and recreated by that runner (an empty/garbled lock is
    likewise left alone, since it isn't provably ours). Best-effort."""
    try:
        with open(lock_path, encoding="ascii") as fh:
            pid_text = fh.read().strip()
    except OSError:
        return
    if pid_text != str(os.getpid()):
        return  # not ours — leave it for its owner
    try:
        os.unlink(lock_path)
    except OSError:
        pass


# --- wiring -----------------------------------------------------------------
def build_runner(config: Config) -> Runner:
    """Wire a live `Runner` from `config` (§5.7).

    Builds the Google REST chat client (seeded with the persisted bot id so
    self-filtering survives a restart), the configured LLM, an optional RAG
    retriever (None ⇒ direct-context bypass), the `Analyzer`, and the
    `IssueStore`. Used by `scripts/run_poller.py`.
    """
    from .chat.google_rest import GoogleChatClient
    from .llm.openrouter import build_llm
    from .rag.store import build_retriever

    store = IssueStore(config.STATE_FILE)
    store.load()
    bot_id = store.get_bot_user_id()

    chat = GoogleChatClient(config, user_id=bot_id)
    llm = build_llm(config)
    retriever = build_retriever(config.KB_DIR, history=None, dense=config.RAG_DENSE)
    analyzer = Analyzer(llm, retriever, config.RAG_TOP_K)

    return Runner(chat, analyzer, store, config, reports_dir=config.REPORTS_DIR, llm=llm)
