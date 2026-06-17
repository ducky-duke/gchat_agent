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
    from .llm.tts import TTSClient

# A generous cap on how many recent ids the cursor's `seen` set carries so an
# equal-`createTime` boundary message is never reprocessed (§5.4). The store
# bounds the persisted set too; this just limits what we hand it each cycle.
_SEEN_WINDOW = 500

# Re-fetch a small window before the cursor boundary so an equal-`createTime`
# message at the boundary is never dropped by the adapter's strict `createTime >`
# filter (§5.4/§7); the cursor's `seen` set dedups the replayed messages.
_CURSOR_SKEW_SECONDS = 2

# Ceiling on the cross-cycle exponential backoff `run_forever` applies after
# consecutive cycle failures, so a sustained outage settles to one retry every
# few minutes instead of hammering a dead endpoint every poll interval.
_CYCLE_BACKOFF_CAP_SECONDS = 300.0


def _now() -> str:
    """A UTC RFC-3339 timestamp string (consistent `now` across the cycle)."""
    return datetime.now(timezone.utc).isoformat()


def _normalize_user_id(raw: str | None) -> str | None:
    """Normalize a configured bot id to the Chat `users/<id>` resource name —
    the exact form a message's `sender` carries, so self-filtering compares
    like-for-like. Accepts a bare id (`1234567890`) or the full `users/1234567890`
    form; blank/`None` ⇒ `None` (no configured id)."""
    val = (raw or "").strip()
    if not val:
        return None
    return val if val.startswith("users/") else f"users/{val}"


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


def _seconds_since(ts: str | None) -> float:
    """Seconds elapsed between an RFC-3339 timestamp and now (UTC). Returns 0.0
    when `ts` is missing or unparseable — a safe floor, so a missing anchor never
    trips a time-gated reminder early."""
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds()


# Reporter replies that mean "I can't answer this." When the latest clarify reply
# is essentially one of these AND core facts are still missing, re-asking is
# pointless — the runner closes the issue with the gap documented instead of
# repeating the question (the "duplicate question" failure). Matched only on a
# SHORT reply so a long, substantive answer that merely contains "not sure"
# somewhere isn't misread as a decline.
_DECLINE_PHRASES: tuple[str, ...] = (
    "i don't know", "i dont know", "don't know", "dont know", "idk",
    "no idea", "not sure", "unsure", "dunno", "no clue", "can't say",
    "cant say", "not certain", "no information", "who knows", "beats me",
)
_DECLINE_MAX_WORDS = 8

# Politeness / hedge tokens that pad a refusal without answering anything, so
# "no, I don't know" and "Dunno, sorry" still read as pure declines. A reply that
# carries ANY word outside this set (and outside the decline phrase) answered part
# of the question — e.g. "it's in production. Else I don't know" answered the
# environment — so it is a PARTIAL answer, not a refusal. Kept deliberately small:
# when in doubt we treat a reply as a partial answer (not a decline), which only
# costs one more clarify round, never a premature close.
_DECLINE_FILLER: frozenset[str] = frozenset({
    "sorry", "no", "nope", "well", "honestly", "really", "just", "um", "uh",
    "oh", "hmm", "yeah", "afraid", "totally", "completely", "else", "but",
    "otherwise", "however", "though",
})


def _looks_like_decline(text: str) -> bool:
    """Whether a reply is essentially "I don't know" — a refusal of the asked
    questions that supplies NO real answer. It must be short (≤ `_DECLINE_MAX_WORDS`
    words), carry a decline phrase, AND have nothing substantive beyond that phrase
    plus padding. A reply that also answers part (e.g. "it's in production. Else I
    don't know") is a PARTIAL answer, not a decline: the reporter declining the
    asked questions does not mean they can't answer the still-open facts, so the
    bot keeps clarifying rather than closing the whole issue as unanswerable."""
    low = " ".join((text or "").lower().split())
    if not low or len(low.split()) > _DECLINE_MAX_WORDS:
        return False
    if not any(p in low for p in _DECLINE_PHRASES):
        return False
    residue = low
    for phrase in _DECLINE_PHRASES:
        residue = residue.replace(phrase, " ")
    leftover = [w.strip(".,!?;:'\"()") for w in residue.split()]
    substantive = [w for w in leftover if w and w not in _DECLINE_FILLER]
    return not substantive


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
        tts: "Optional[TTSClient]" = None,
    ) -> None:
        self.chat = chat
        self.analyzer = analyzer
        self.store = store
        self.config = config
        self.reports_dir = reports_dir or config.REPORTS_DIR
        # The report builder wants an LLM to tighten prose; reuse the analyzer's
        # unless one is injected. `None` is fine — the builder degrades.
        self._llm: "Optional[LLMClient]" = llm or getattr(analyzer, "llm", None)
        # Optional TTS for voice-report delivery (REPORT_DELIVERY=voice|both).
        # `None` ⇒ the disk path; voice delivery degrades to disk if it's absent.
        self._tts: "Optional[TTSClient]" = tts
        # The working conversation accumulates fetched messages across cycles so
        # threads keep their full context (detection is still windowed).
        self._conversation = Conversation()

    # --- one orchestration iteration ---------------------------------------
    def run_cycle(self) -> dict:
        """Run one fetch → detect → clarify/resolve iteration; return a summary."""
        self.store.load()
        tokens_before = self._llm_total_tokens()
        own_id = self._resolve_own_id()

        new_messages = self._fetch_new_messages()
        # Detection is the dominant per-cycle cost (a full DETECT_WINDOW tail
        # through the frontier model), so it fires only when this cycle brought
        # genuinely new traffic that could be a *new* issue — see `_should_detect`.
        # A pure clarification cycle (the reporter only answered in an open issue's
        # thread) skips it entirely: that reply is handled by `assess_clarity`, not
        # a re-detect that would just re-derive the same candidates (Lever B).
        detected = self._detect(own_id) if self._should_detect(new_messages, own_id) else 0
        asked, resolved, stale, escalated, redirected = self._process_open_issues(own_id)

        # Persist a freshly-learned bot id (the client may have learned its own
        # users/<id> from a post this cycle) so a restart self-filters at once.
        self._remember_bot_id(self.chat.me())

        self.store.save()
        return {
            "fetched": len(new_messages),
            "detected": detected,
            "asked": asked,
            "resolved": resolved,
            "stale": stale,
            "escalated": escalated,
            "redirected": redirected,
            # LLM tokens this cycle billed across all calls (0 when the model
            # doesn't report usage; an estimate on the mock path). Surfaced in the
            # cycle log so quota spend is visible at a glance.
            "tokens": max(0, self._llm_total_tokens() - tokens_before),
        }

    def _llm_total_tokens(self) -> int:
        """Cumulative `total_tokens` reported by the LLM client, or 0 if it doesn't
        track usage. Read before/after a cycle to compute per-cycle spend."""
        snap = getattr(self._llm, "usage_snapshot", None)
        if not callable(snap):
            return 0
        try:
            return int(snap().get("total_tokens", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return 0

    # --- step 1: identity ---------------------------------------------------
    def _resolve_own_id(self) -> str | None:
        """The bot's own `users/<id>` for self-filtering. Prefer the live client;
        persist it the first time the client knows it."""
        live = self.chat.me()
        if live is not None:
            self._remember_bot_id(live)
            return live
        return self.store.get_bot_user_id()

    def _remember_bot_id(self, live: str | None) -> None:
        """Persist a freshly-resolved bot `users/<id>` and log it ONCE. Fires only
        when the id was NOT pinned via `GOOGLE_BOT_USER_ID` (build_runner seeds the
        store from that, so this is a no-op then) — i.e. when it was auto-resolved
        from the OAuth tokeninfo endpoint or learned from the first post. The
        persisted guard makes the log fire exactly once per resolved id."""
        if not live or self.store.get_bot_user_id():
            return
        self.store.set_bot_user_id(live)
        import sys

        print(
            f"[issue-spotter] bot self-id resolved: {live} (self-filtering "
            f"active). Optionally set GOOGLE_BOT_USER_ID={live} in .env to skip "
            "the startup lookup.",
            file=sys.stderr,
        )

    # --- step 2: fetch ------------------------------------------------------
    def _fetch_new_messages(self) -> list[Message]:
        """Fetch messages after the cursor, drop already-seen ids, append to the
        working conversation, and advance the cursor. Returns the new messages
        (so the caller can tell whether any non-bot content arrived). On a true
        first run (no cursor, no backfill) seed `since` to *now* so there is no
        history backfill (§5.1)."""
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
        return new

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
    def _should_detect(self, new_messages: list[Message], own_id: str | None) -> bool:
        """Whether this cycle's new traffic warrants a (costly) re-detection.

        Detection renders the whole `DETECT_WINDOW_MESSAGES` tail through the
        frontier model — the dominant per-cycle cost — so it must not fire on
        traffic that cannot be a *new* issue. Two filters gate it:

        * **non-bot** — a cycle that only re-saw the bot's own post brings no new
          foreign content (`_detect` drops the bot's messages anyway), so skip; and
        * **out-of-thread** — a reply landing inside an already-open issue's thread
          (its own thread, its escalation/nudge thread, or the follow-the-reporter
          `active_thread_id`) is an answer to a clarifying question, handled by
          `assess_clarity`. Re-detecting over it would just re-derive the same
          candidates at the price of one big LLM round-trip (Lever B). Detection
          therefore fires only when at least one new foreign message landed
          OUTSIDE every open issue's threads — genuinely new top-level traffic,
          where a new issue is plausible.

        A new issue mentioned *inside* a clarification thread is deferred, not
        lost: detection still uses the flat `tail(N)` window, so the next time it
        fires (any out-of-thread traffic) it re-scans those recent in-thread
        messages too, as long as they are still within the window.

        Conservative fallbacks: with the bot id not yet known (bootstrap) we can't
        self-filter, so detect on any new traffic; a message carrying no thread_id
        counts as top-level (outside the open-issue threads), so it triggers
        detection.
        """
        if not new_messages:
            return False
        if own_id is None:
            # Bootstrap: self-id unknown — stay conservative and detect on any new
            # traffic (matches the pre-Lever-B behavior for this one case).
            return True
        foreign = [m for m in new_messages if m.sender != own_id]
        if not foreign:
            return False
        open_threads = self._open_issue_threads()
        return any((m.thread_id or "") not in open_threads for m in foreign)

    def _open_issue_threads(self) -> set[str]:
        """Every thread bound to a currently-open issue — its own thread plus the
        escalation/nudge and follow-the-reporter (`active_thread_id`) threads — so
        a reply in any of them reads as a clarification answer, not new-issue
        traffic. Read from the store, which `run_cycle` loaded at the cycle's
        start, so it reflects the issues open BEFORE this cycle's detection."""
        threads: set[str] = set()
        for issue in self.store.open_issues():
            for tid in (
                issue.thread_id,
                issue.active_thread_id,
                issue.escalation_thread_id,
            ):
                if tid:
                    threads.add(tid)
        return threads

    def _detect(self, own_id: str | None) -> int:
        """Detect candidates over the recent window with the bot's own messages
        dropped, then upsert all that aren't tombstoned."""
        window = self._conversation.tail(self.config.DETECT_WINDOW_MESSAGES)
        detection_conv = window.without_sender(own_id) if own_id else window

        # Episodic recall: surface the few most recently closed issues so detection
        # has memory of what was already handled (self-gating — empty on a fresh
        # start). Off ⇒ pass nothing, identical to the pre-recall behavior.
        prior = self.store.recent_closed() if self.config.EPISODIC_RECALL else None

        detected = 0
        for candidate in self.analyzer.detect_issues(detection_conv, prior_issues=prior):
            if self.store.is_tombstoned(candidate.fingerprint):
                continue
            self.store.upsert(candidate)
            detected += 1
        return detected

    # --- step 5: the clarify / resolve loop ---------------------------------
    def _process_open_issues(self, own_id: str | None) -> tuple[int, int, int, int, int]:
        """Drive each open issue one step, then post any due reminders as a single
        consolidated nudge per reporter; return (asked, resolved, stale, escalated,
        redirected)."""
        asked = resolved = stale = redirected = 0
        for issue in self.store.open_issues():
            with observability.trace("issue", issue_id=issue.id):
                outcome = self._step_issue(issue, own_id)
            if outcome == "asked":
                asked += 1
            elif outcome == "resolved":
                resolved += 1
            elif outcome == "stale":
                stale += 1
            elif outcome == "redirected":
                redirected += 1
        # Escalation is BATCHED across the whole open set, not decided per issue:
        # a reporter with several overdue clarifications gets ONE consolidated
        # @mention, not one ping per issue (§ escalate).
        escalated = self._escalate_due()
        return asked, resolved, stale, escalated, redirected

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
            # Production (REDIRECT_OUT_OF_THREAD_REPLY): the reporter may have
            # answered OUTSIDE the issue thread. Redirect them back in-thread with
            # one templated, LLM-free nudge (never trusting/echoing the outside
            # text) BEFORE the top-level escalation. The nudge resets idle_cycles
            # (the reporter is engaged, just in the wrong place), which also defers
            # escalation and avoids a double-nag in the same idle window.
            if self._should_redirect(issue) and self._redirect_out_of_thread(issue, own_id):
                return "redirected"
            # The top-level @mention reminder is posted AFTER the per-issue loop so
            # several overdue issues collapse into one nudge (_escalate_due). Defer
            # staleness while that reminder is still owed, so the bot always nudges
            # before giving up on an unanswered clarification.
            if self._escalation_pending(issue):
                return None
            if issue.idle_cycles >= self.config.STALE_AFTER_IDLE_CYCLES:
                return self._mark_stale(issue)
            return None

        # First contact: a freshly detected issue (no questions asked, no replies)
        # is definitionally not "clear" yet — skip the `assess_clarity` LLM call and
        # open with the first questions straight away. With Lever 1 the detection
        # call already produced those opening questions inline (`pending_questions`),
        # so this first ask usually costs ZERO extra round-trips — detect+ask is one
        # call, not two (a generate_questions fallback fires only if detection
        # produced none). Only when we will actually ask (rounds < cap), so the
        # degenerate cap=0 config still falls through to the assess→stale path below.
        if (
            not issue.questions_asked
            and not replies
            and issue.rounds < self.config.MAX_CLARIFY_ROUNDS
        ):
            if self._ask(issue, thread_conv, [], questions=issue.pending_questions):
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

        # Loop-breaker — never re-ask questions the reporter can't answer (the
        # "duplicate question" failure). When the reporter has replied but the
        # missing-facts set did not shrink, re-asking just repeats the same
        # questions. Two triggers close the issue with the remaining facts
        # documented as open questions, instead of nagging:
        #   * the reply is essentially "I don't know" (a decline) AND it made no
        #     progress — the SAME facts are still missing, so the reporter has
        #     genuinely been asked and cannot answer; or
        #   * the gap hasn't shrunk for MAX_NO_PROGRESS_ROUNDS consecutive replies.
        # The FIRST clarify reply only establishes the baseline (the reporter has
        # not seen the refined questions yet), so it never trips the counter.
        #
        # Crucially, a decline closes the issue ONLY when it made no progress. When
        # the reply progressed — it answered part, OR (as on the first clarify
        # reply) it surfaced NEW core facts like owner / root-cause that were never
        # asked — "I don't know" means "I can't answer THESE questions", not "I know
        # nothing about the issue". Closing then would wrongly record never-asked
        # facts as unanswerable open questions; instead we keep clarifying the
        # still-open facts. "Progress" = the gap changed at all (it shrank, or new
        # facts surfaced); only an identical gap two rounds running is stuck.
        if replies and assessment.missing_info:
            prev = set(issue.last_missing_info)
            curr = set(assessment.missing_info)
            progressed = (not prev) or (curr != prev)
            issue.no_progress_rounds = 0 if progressed else issue.no_progress_rounds + 1
            issue.last_missing_info = list(assessment.missing_info)
            declined = any(_looks_like_decline(r.text) for r in replies)
            if (
                (declined and not progressed)
                or issue.no_progress_rounds >= self.config.MAX_NO_PROGRESS_ROUNDS
            ):
                self._resolve(issue, thread_conv, gaps=assessment.missing_info)
                return "resolved"
        elif replies:
            # Reporter replied and nothing core is missing, but confidence was too
            # low to resolve above — count it as progress so a later stall starts
            # its no-progress tally fresh.
            issue.no_progress_rounds = 0
            issue.last_missing_info = []

        if issue.rounds < self.config.MAX_CLARIFY_ROUNDS:
            # Lever 1: the clarity call already drafted the next batch inline
            # (`assessment.questions`); prefer it, falling back to a dedicated
            # generation only when it produced none — so assess+ask is one call.
            if self._ask(
                issue, thread_conv, assessment.missing_info,
                questions=assessment.questions,
            ):
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

    # --- escalation (batched: one consolidated nudge per reporter) ----------
    def _escalation_enabled(self) -> bool:
        """Escalation is on unless `ESCALATE_AFTER_SECONDS` is negative."""
        return self.config.ESCALATE_AFTER_SECONDS >= 0

    def _escalation_pending(self, issue) -> bool:
        """An idle CLARIFYING issue still owed its one reminder: escalation is
        enabled, the reporter is known, we've asked at least one question, and this
        issue hasn't been escalated yet. While this holds, the issue does NOT go
        stale — its reminder always gets its chance first; afterwards it stales
        normally (every pending issue is escalated within the grace window, so this
        never defers staleness forever)."""
        return (
            self._escalation_enabled()
            and not issue.escalated
            and issue.status == Status.CLARIFYING
            and bool(issue.reporter_id)
            and bool(issue.questions_asked)
        )

    def _escalation_ready(self, issue) -> bool:
        """An idle awaiting issue that may be folded into a reminder: pending AND
        it has actually sat idle for at least one cycle (so an issue we asked or
        the reporter answered THIS cycle is never swept in). No time gate here —
        the wall-clock grace is checked once per reporter via `_escalation_due`."""
        return self._escalation_pending(issue) and issue.idle_cycles >= 1

    def _escalation_due(self, issue) -> bool:
        """The trigger: a ready issue whose last clarifying question has gone
        unanswered for at least `ESCALATE_AFTER_SECONDS` wall-clock. When any of a
        reporter's issues is due, the reporter is reminded this cycle."""
        return (
            self._escalation_ready(issue)
            and _seconds_since(issue.last_question_at) >= self.config.ESCALATE_AFTER_SECONDS
        )

    def _escalate_due(self) -> int:
        """Post a top-level @mention reminder for issues past the grace window,
        consolidating a reporter's several due-this-cycle issues into ONE nudge so
        they aren't pinged once per issue at the same moment. Each issue is reminded
        at most ONCE (`Issue.escalated`); a reporter whose issues go overdue at
        different times gets one reminder per issue, over time. Returns the number
        of issues folded into a reminder this cycle (the summary's `escalated`
        count)."""
        if not self._escalation_enabled():
            return 0
        open_issues = self.store.open_issues()
        # Reporters with at least one issue past the grace window get a reminder
        # now. Build the list in open-issue order (deterministic), deduped.
        reporters: list[str] = []
        for issue in open_issues:
            rid = issue.reporter_id
            if rid and self._escalation_due(issue) and rid not in reporters:
                reporters.append(rid)
        if not reporters:
            return 0

        escalated = 0
        for rid in reporters:
            issues = [
                i for i in open_issues
                if i.reporter_id == rid and self._escalation_ready(i)
            ]
            if not issues:
                continue
            self._post_escalation(rid, issues)
            escalated += len(issues)
        return escalated

    def _post_escalation(self, reporter_id: str, issues: list) -> None:
        """Post the single consolidated @mention nudge for one reporter and mark
        each folded issue escalated with a fresh idle budget.

        Idempotent: the `request_id` is derived from the sorted issue ids, so a
        crash between the post and the state save replays the same batch as the
        same Chat message (the client dedups), and `Issue.escalated` (persisted)
        bars a second nudge for each issue once saved.

        A single-issue nudge keeps the old phrasing and records the thread it
        opened as a 1:1 home for out-of-thread capture (§A). A multi-issue nudge
        lists the titles and leaves `escalation_thread_id` unset: a reply in the
        shared nudge thread can't be attributed to one of several issues, so the
        reporter is pointed back to the original threads instead."""
        mention = _mention(reporter_id)
        if len(issues) == 1:
            title = issues[0].title or "an issue"
            text = (
                f"{mention} I asked a couple of clarifying questions about "
                f"“{title}” in a thread above — could you take a look when you "
                f"get a chance? \U0001f64f"
            ).strip()
        else:
            bullets = "\n".join(f"• “{i.title or 'an issue'}”" for i in issues)
            text = (
                f"{mention} I asked clarifying questions about a few issues in the "
                f"threads above — could you take a look when you get a chance? "
                f"\U0001f64f\n{bullets}"
            ).strip()

        request_id = "client-escalate-" + "-".join(sorted(i.id for i in issues))
        posted = self.chat.post_message(text, thread_id=None, request_id=request_id)

        now = _now()
        set_home = len(issues) == 1
        for issue in issues:
            if set_home:
                issue.escalation_thread_id = posted.thread_id or None
            issue.escalated = True
            issue.idle_cycles = 0  # grant a fresh stale window after the nudge
            issue.updated_at = now

    # --- redirect-on-capture (production: REDIRECT_OUT_OF_THREAD_REPLY) ------
    def _should_redirect(self, issue) -> bool:
        """Cheap pre-check for a templated in-thread redirect this cycle: the
        production flag is on, we have already asked (so an out-of-thread reply is
        plausibly an answer), the reporter is known, and we have not nudged yet
        (one-shot). The evidence lookup + post happen in `_redirect_out_of_thread`."""
        return (
            self.config.REDIRECT_OUT_OF_THREAD_REPLY
            and not issue.redirect_nudged
            and bool(issue.questions_asked)
            and bool(issue.reporter_id)
        )

    def _redirect_out_of_thread(self, issue, own_id: str | None) -> bool:
        """Production redirect-on-capture: the reporter answered OUTSIDE the issue
        thread. We never trust that text to resolve — instead record it as evidence
        (ids only) and post ONE templated, LLM-free nudge into the issue's OWN
        thread asking them to confirm there. Returns True iff a nudge was posted.

        Leak-safe by construction: the nudge is a fixed template + the reporter
        @mention + the issue title (which derives from the reporter's ORIGINAL
        in-thread report — exactly what the escalation nudge already posts). The
        out-of-thread message text is never quoted, paraphrased, fed to an LLM, or
        written to the Q&A / report / voice.

        Idempotent and non-spammy: one-shot via `redirect_nudged` (persisted) plus
        a stable `request_id`; pinned to `issue.thread_id` (never
        `active_thread_id`, so it can't be dragged into the unrelated thread the
        outside reply lives in); it advances the bot anchor so the reporter's
        in-thread confirmation is then captured as a normal reply; and it resets
        `idle_cycles` (the reporter is engaged) which defers escalation."""
        evidence = self._out_of_thread_reporter_messages(issue, own_id)
        already = set(issue.out_of_thread_evidence_ids)
        new_ids = [m.id for m in evidence if m.id not in already]
        if not new_ids:
            return False

        title = issue.title or "an issue"
        text = (
            f"{_mention(issue.reporter_id)} thanks! I think I saw a reply about "
            f"“{title}” outside this thread. To keep the record clear, "
            f"could you confirm the key details right here in this thread? \U0001f64f"
        ).strip()
        posted = self._post_to_thread(
            issue, text,
            request_id=f"client-issue-{issue.id}-redirect",
            target_thread=issue.thread_id,
        )
        # Record evidence ids ONLY after a successful post. State is saved once at
        # end-of-cycle, so a post exception here rolls the whole step back; recording
        # strictly post-success also means a partial state (evidence noted but nudge
        # unsent) can never suppress a later retry of the redirect.
        issue.out_of_thread_evidence_ids.extend(new_ids)
        # Treat the redirect like a bot question for anchoring: a later in-thread
        # reply is then a fresh reply (advances `last_bot_*`), so the reporter's
        # confirmation is captured by the strict-thread source and resolves
        # normally. It is NOT appended to `questions_asked` — not a clarify round,
        # so it neither consumes MAX_CLARIFY_ROUNDS nor is seen by assess_clarity.
        issue.last_bot_message_id = posted.id
        issue.last_bot_create_time = posted.create_time or issue.last_bot_create_time
        issue.redirect_nudged = True
        issue.idle_cycles = 0  # the reporter is engaged — grant a fresh window
        issue.updated_at = _now()
        return True

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
        Fed into the resolution view only in the DEFAULT mode; disabled when
        either `config.REQUIRE_IN_THREAD_REPLY` (drop it) or
        `config.REDIRECT_OUT_OF_THREAD_REPLY` (collect it as evidence-only for a
        templated in-thread redirect — see `_redirect_out_of_thread`) is set, so
        only A and the strict thread then advance the issue. Otherwise, a
        top-level reply with no nudge to anchor it can't be tied to a specific
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

        # (B) The reporter's answers in some other fresh thread (guarded). Merged
        # into the resolution view only in the DEFAULT mode; disabled when either
        # REQUIRE_IN_THREAD_REPLY (drop the reply entirely) or
        # REDIRECT_OUT_OF_THREAD_REPLY (collect it as evidence-only and post a
        # templated in-thread redirect instead — `_redirect_out_of_thread`) is
        # set, so a bare top-level / off-topic message can then never be mistaken
        # for an answer here (the "barge into unrelated discussion" risk).
        if self._source_b_feeds_resolution():
            for m in self._out_of_thread_reporter_messages(issue, own_id):
                if m.id in in_thread_ids or m.id in seen_extra:
                    continue  # already in the strict thread / a home thread (A)
                extra.append(replace(m, thread_id=issue.thread_id))
                seen_extra.add(m.id)

        if not extra:
            return strict
        merged = sorted(strict.messages + extra, key=lambda m: (m.create_time or "", m.id))
        return Conversation(messages=merged)

    def _source_b_feeds_resolution(self) -> bool:
        """Whether a reporter's OUT-OF-THREAD reply (source B) may directly advance
        an issue — feed clarity/resolve/Q&A and move `active_thread_id`. False when
        either guard is set: REQUIRE_IN_THREAD_REPLY (drop it) or
        REDIRECT_OUT_OF_THREAD_REPLY (collect it only as evidence for a templated
        in-thread redirect). When False, the issue advances solely from the strict
        thread + its home threads (A)."""
        return not (
            self.config.REQUIRE_IN_THREAD_REPLY
            or self.config.REDIRECT_OUT_OF_THREAD_REPLY
        )

    def _out_of_thread_reporter_messages(
        self, issue, own_id: str | None
    ) -> list[Message]:
        """The reporter's qualifying OUT-OF-THREAD messages — the source-B
        candidate set, shared by `_effective_conversation` (default mode merges
        them into the resolution view) and `_redirect_out_of_thread` (production
        mode treats them as evidence). Returns the REAL messages from the working
        conversation (never re-tagged copies), in conversation order.

        Guards (all must hold), the long-standing source-B contract: a known
        reporter distinct from the bot; a recorded last-bot question to gate
        "newer than"; the reporter has at most ONE open awaiting issue (else a
        bare message is ambiguous — which issue does it answer?); and the message
        is the reporter's own, newer than the last bot question, and not in the
        strict thread, a home thread (A), or another open issue's thread."""
        cutoff = issue.last_bot_create_time
        reporter = issue.reporter_id
        if not (cutoff and reporter and reporter != own_id):
            return []
        awaiting = [
            i for i in self.store.open_issues()
            if i.reporter_id == reporter and i.last_bot_create_time
        ]
        if len(awaiting) > 1:  # ambiguous: which issue does a bare reply answer?
            return []
        strict_ids = {
            m.id for m in self._conversation.for_thread(issue.thread_id).messages
        }
        home_threads = {
            t for t in (issue.escalation_thread_id, issue.active_thread_id)
            if t and t != issue.thread_id
        }
        other_threads = {
            i.thread_id for i in self.store.open_issues()
            if i.thread_id and i.thread_id != issue.thread_id
        }
        out: list[Message] = []
        for m in self._conversation.messages:
            if m.thread_id == issue.thread_id or m.id in strict_ids:
                continue  # already in the strict thread
            if m.thread_id and (m.thread_id in home_threads or m.thread_id in other_threads):
                continue  # home thread (A) / another open issue's thread
            if m.sender != reporter:
                continue  # only the reporter's own out-of-thread answers
            if not m.create_time or m.create_time <= cutoff:
                continue  # not newer than the last bot question
            out.append(m)
        return out

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

    def _ask(
        self,
        issue,
        thread_conv: Conversation,
        missing_info: list[str],
        questions: list[str] | None = None,
    ) -> bool:
        """Post the next clarifying-question batch into the thread. Returns True if
        a batch was posted, False if no questions were available.

        Lever 1: `questions` are the ones the *detection* or *clarity* call already
        produced inline — preferred so we skip a dedicated `generate_questions`
        round-trip. They fall back to a `generate_questions` call when empty (the
        model emitted none, or this is the degenerate cap=0 path), so question
        quality never regresses. `thread_conv` (the effective view) feeds that
        fallback generation only; the post is routed to the issue's real thread via
        `_post_to_thread`."""
        batch = [q for q in (questions or []) if q.strip()]
        if not batch:
            batch = self.analyzer.generate_questions(issue, thread_conv, missing_info)
        if not batch:
            return False
        text = "\n".join(batch)
        request_id = f"client-issue-{issue.id}-r{issue.rounds + 1}"
        posted = self._post_to_thread(issue, text, request_id)

        now = _now()
        # The inline detection suggestions are now spent (asked or superseded).
        issue.pending_questions = []
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

    def _resolve(self, issue, thread_conv: Conversation, gaps: list[str] | None = None) -> None:
        """Resolve once: build the report, deliver it (disk and/or voice), post
        the in-thread confirmation, mark resolved, tombstone.

        `gaps` are the core facts still missing when the issue is closed WITHOUT
        them (the loop-breaker path: the reporter said "I don't know" or the
        exchange stopped making progress). They flow into the report as documented
        "open questions" and make the confirmation honest ("closed with open
        questions") instead of claiming a clean resolution; `None` ⇒ a clean
        resolve, unchanged.

        Delivery follows `REPORT_DELIVERY` (`disk` | `voice` | `both`). Voice
        delivery is best-effort: if it is unavailable or fails, the disk report is
        written as a safety net so a resolution is never lost. The confirmation
        always lands in the issue thread; its trailing reference names where the
        report actually went.

        Idempotency: the whole block is gated on `report_written_at` (persisted
        state) so a state reload can't redo it. The file write is skipped when the
        report already exists, the voice post carries a stable `request_id`, and
        the confirmation a second one — each individually idempotent — so a crash
        mid-delivery lets the next cycle finish rather than skip it forever (§5.7)."""
        now = _now()
        if not issue.report_written_at:
            report = report_mod.build_resolution_report(issue, self._llm, open_questions=gaps)
            delivery = (self.config.REPORT_DELIVERY or "disk").strip().lower()
            want_voice = delivery in ("voice", "both")
            want_disk = delivery in ("disk", "both")

            voice_target = self._deliver_voice(issue, report) if want_voice else None
            # Disk write for disk/both, and as a safety net when voice was wanted
            # but could not be delivered — so a report is never silently lost.
            disk_written = want_disk or (want_voice and voice_target is None)
            if disk_written:
                report_path = os.path.join(self.reports_dir, f"issue-{issue.id}.md")
                if not os.path.exists(report_path):
                    report_mod.write_report(
                        report, self.reports_dir, redact=self.config.REDACT_REPORTS
                    )

            self._post_to_thread(
                issue,
                report_mod.confirmation_line(
                    report, self._report_ref(report, disk_written)
                ),
                request_id=f"client-issue-{issue.id}-report",
            )
            issue.report_written_at = now
        issue.status = Status.RESOLVED
        issue.updated_at = now
        self.store.tombstone(issue)

    def _deliver_voice(self, issue, report) -> str | None:
        """Best-effort voice delivery: narrate the report, synthesize speech, and
        post it as an audio attachment. Returns a short phrase of where it went
        (the target space, or "this thread" on fallback) on success, else None so
        the caller writes the disk report instead. Never raises — voice is a
        delivery channel, not a correctness requirement."""
        if self._tts is None:
            return None
        try:
            narration = report_mod.build_narration(report, self._llm)
            audio = self._tts.synthesize(narration)
            if not audio:
                return None
            target_space = (self.config.GOOGLE_VOICE_SPACE or "").strip() or None
            # A separate space is posted top-level; the fallback threads into the
            # issue's own space so the voice still reaches the discussion.
            thread_id = None if target_space else issue.thread_id
            self.chat.post_voice(
                audio,
                filename=f"issue-{issue.id}.mp3",
                # Carry the spoken transcript in the message body — a Chat audio
                # attachment is a download-only file card, so the transcript is
                # what keeps the report readable in-thread (and accessible).
                text=report_mod.voice_message_text(report, narration),
                space=target_space,
                thread_id=thread_id,
                request_id=f"client-issue-{issue.id}-voice",
            )
            return target_space or "this thread"
        except Exception as exc:  # noqa: BLE001 — fall back to disk, never crash
            import sys

            print(
                f"voice report delivery failed for issue {issue.id}: {exc}; "
                "falling back to the on-disk report",
                file=sys.stderr,
            )
            return None

    @staticmethod
    def _report_ref(report, disk_written: bool) -> str:
        """The confirmation's trailing report reference. Names the on-disk report
        when one was written; voice-only delivery carries no trailing reference
        (the spoken transcript travels in the voice message body, so the
        confirmation never announces where the audio went). Returns `""` when
        nothing was written to disk, so the confirmation has no dangling
        `Report: …` clause pointing at a file that does not exist."""
        if disk_written:
            return f"Report: {report_mod.report_disk_ref(report)}"
        return ""  # voice-only: no on-disk file to point at

    # --- posting helper -----------------------------------------------------
    def _post_to_thread(
        self, issue, text: str, request_id: str, *, target_thread: str | None = None
    ) -> Message:
        """Post `text` into a thread for the issue. By default the issue's *active*
        thread — where the reporter last replied (`active_thread_id`, else the issue
        thread). Pass `target_thread` to pin the post to a specific thread instead
        (the redirect nudge pins to `issue.thread_id` so it can never be dragged
        into the unrelated thread an out-of-thread reply lives in). Prefer a
        threaded reply to a real Message we hold there; fall back to
        `post_message(thread_id=...)` when we have no Message object to reply to.

        The anchor is resolved from `self._conversation` (a real thread), never
        the effective view — a re-tagged out-of-thread copy must never become the
        reply target and redirect the post (§ out-of-thread capture)."""
        target = target_thread or issue.active_thread_id or issue.thread_id
        anchor = self._thread_anchor(issue, target)
        if anchor is not None:
            return self.chat.post_reply(anchor, text, request_id=request_id)
        return self.chat.post_message(text, thread_id=target, request_id=request_id)

    def _thread_anchor(self, issue, target: str | None = None) -> Message | None:
        """A real Message in `target` (default: the issue's *active* thread —
        `active_thread_id`, else the issue thread) to reply to: prefer the most
        recent message there, else the issue's root/source message *if it is in
        `target`*, else None. Resolved from `self._conversation` so it is always a
        genuine in-thread parent, never a re-tagged effective-view copy. The
        in-`target` check keeps a pinned post (e.g. the redirect) from threading
        onto a fallback message that lives in some other thread."""
        target = target or issue.active_thread_id or issue.thread_id
        in_target = self._conversation.for_thread(target)
        if in_target.messages:
            return in_target.messages[-1]
        by_id = {m.id: m for m in self._conversation.messages}
        for mid in (issue.last_bot_message_id, issue.root_message_id, *issue.source_message_ids):
            cand = by_id.get(mid) if mid else None
            if cand is not None and cand.thread_id == target:
                return cand
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
        `POLL_INTERVAL_SECONDS` between successful cycles. Releases the lock on
        exit and flushes observability.

        A single cycle's failure (a network/API/LLM error that survived its own
        retries) is logged and swallowed so one transient hiccup never kills the
        long-running daemon. Crucially, CONSECUTIVE failures back off
        exponentially (the poll interval doubled per failure, capped, plus jitter)
        instead of hammering a dead endpoint every `POLL_INTERVAL_SECONDS`; the
        first success resets the backoff. `KeyboardInterrupt`/`SystemExit` are
        *not* caught (they subclass `BaseException`, not `Exception`), so Ctrl-C
        still shuts down cleanly via the `finally`. `--once` (via `run_once`)
        keeps its fail-fast behavior."""
        import random
        import sys
        import time
        import traceback

        base = max(1, self.config.POLL_INTERVAL_SECONDS)
        cap = max(base, _CYCLE_BACKOFF_CAP_SECONDS)
        consecutive_failures = 0

        lock_path = self._lock_path()
        _acquire_lock_or_raise(lock_path)
        try:
            while True:
                started = time.monotonic()
                try:
                    summary = self.run_cycle()
                except Exception:  # noqa: BLE001 — daemon must outlive any one cycle
                    consecutive_failures += 1
                    traceback.print_exc()
                    # Exponential backoff with jitter so a sustained outage isn't
                    # hammered once per poll interval (the old flat sleep).
                    delay = min(base * (2 ** (consecutive_failures - 1)), cap)
                    delay += random.uniform(0, base)
                    print(
                        "cycle failed (see traceback above); continuing after "
                        f"{delay:.0f}s (consecutive failure #{consecutive_failures})",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                consecutive_failures = 0
                self._log_cycle(summary, time.monotonic() - started)
                time.sleep(base)
        finally:
            _release_lock(lock_path)
            observability.flush()

    @staticmethod
    def _log_cycle(summary: dict, elapsed: float) -> None:
        """Print a one-line trace for any cycle that *did* something — fetched a
        message or moved an issue — with wall-clock elapsed so a slow LLM
        round-trip is visible at a glance (a cycle is otherwise near-instant
        stdlib work, so `elapsed` is essentially the LLM time). Quiet polls stay
        silent so a tight poll interval doesn't flood the log."""
        if not any(summary.values()):
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        parts = " ".join(f"{k}={v}" for k, v in summary.items() if v)
        print(f"[{stamp}] cycle {parts} ({elapsed:.1f}s)", flush=True)


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
    from .config import validate_config
    from .llm.openrouter import build_llm
    from .llm.tts import build_tts
    from .rag.store import build_retriever

    # Fail fast on a bad enum/range before any network wiring (also covers a
    # Config built directly, bypassing load_config's validation).
    validate_config(config)

    store = IssueStore(config.STATE_FILE)
    store.load()
    # Prefer an explicitly configured bot id so the client knows its own
    # users/<id> from the FIRST cycle — even on a fresh start with no persisted
    # state, before it has posted once (otherwise `me()` is None on cycle 1 and
    # detection can't drop the bot's own messages → a self-loop). Fall back to
    # the persisted id learned on a previous run.
    # Self-id precedence: a pinned GOOGLE_BOT_USER_ID, else the id persisted from a
    # prior run, else (when both are absent — a true fresh start) the client
    # auto-resolves it from the OAuth tokeninfo endpoint on its first `me()` call,
    # so self-filtering works from cycle 1 without pinning or posting. Pinning just
    # skips that one lookup.
    configured_id = _normalize_user_id(config.GOOGLE_BOT_USER_ID)
    bot_id = configured_id or store.get_bot_user_id()
    # Persist a configured id up front so it survives as the known self id and the
    # runner's self-id log stays quiet (the user already pinned it).
    if configured_id:
        store.set_bot_user_id(configured_id)

    chat = GoogleChatClient(config, user_id=bot_id)
    llm = build_llm(config)
    tts = build_tts(config)  # None unless REPORT_DELIVERY needs voice
    retriever = build_retriever(config.KB_DIR, history=None, dense=config.RAG_DENSE)
    analyzer = Analyzer(llm, retriever, config.RAG_TOP_K)

    return Runner(
        chat, analyzer, store, config,
        reports_dir=config.REPORTS_DIR, llm=llm, tts=tts,
    )
