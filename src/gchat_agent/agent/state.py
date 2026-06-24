"""Persistent issue store (§5.6 + §6).

`IssueStore` is the durable memory of the agent loop: it loads/saves the whole
`models.AgentState` blob (poll cursor + live issues + a tombstone set), dedups
new detections against the open issues by a *stable fingerprint*, and merges new
evidence into a matching issue instead of re-raising it. Resolved/stale issues
are tombstoned so a closed issue is never re-raised from the same root message
(§6). Persistence is atomic (temp file + ``os.replace`` after ``mkdir -p``) so a
crash mid-write can never corrupt the state file.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from typing import Callable, Final, Optional

from ..models import AgentState, Issue, Status

# A caller-supplied cross-thread duplicate decider: given a candidate and the open
# issues (in OTHER threads) that share at least a lexical hint with it, return the
# one it duplicates, or None. The live runner backs this with an LLM call; it is
# None on the offline path and in tests, keeping the store deterministic + pure.
SemanticMatch = Callable[[Issue, "list[Issue]"], Optional[Issue]]

# A closed candidate that hits a tombstoned fingerprint is neither stored nor
# raised; `upsert` returns this sentinel so callers can distinguish "merged /
# suppressed" from "newly tracked" without re-adding a duplicate.
TOMBSTONED: Final = None

# Bound the seen-id set so the state file does not grow without limit. The
# cursor only needs enough recent ids to disambiguate equal-`createTime`
# messages around the boundary (§5.4/§7); a few hundred is ample.
_MAX_SEEN_IDS: Final[int] = 500

# Statuses that take an issue out of the "open / live" working set.
_CLOSED_STATUSES: Final[frozenset[Status]] = frozenset({Status.RESOLVED, Status.STALE})

# Title/summary similarity tie-breaker: a candidate whose normalized title or
# summary overlaps an open issue this much is treated as the same issue even if
# the fingerprint differs (e.g. the LLM flipped the category). Used SAME-THREAD,
# where thread locality is itself corroborating evidence of "the same issue".
_SIMILARITY_THRESHOLD: Final[float] = 0.6

# Cross-thread near-duplicate threshold: a SECOND reporter independently raising
# the same incident in their OWN thread yields a different thread_id (so a
# different fingerprint AND no same-thread `_find_similar` hit). To still fold it
# into the one open issue, match on title/summary overlap across threads — but at
# a slightly HIGHER bar than `_SIMILARITY_THRESHOLD`, because cross-thread we lose
# thread locality as a corroborating signal, so we demand more lexical overlap
# before merging. This is the CONFIDENT fast path; the ambiguous band below it is
# left to the optional LLM decider (see `upsert`'s `semantic_match`).
_CROSS_THREAD_SIMILARITY_THRESHOLD: Final[float] = 0.65

# Lexical *hint* floor for the LLM cross-thread decider: raw-token jaccard scores
# obvious paraphrases low (e.g. "API gateway timing out … 504 errors" vs "API
# gateway 504 timeouts" ≈ 0.5), and can even rank a genuinely-distinct pair
# ("payouts failing" vs "deposits failing") ABOVE a real dup — so no lexical bar
# separates them. The LLM decides instead, but only for open issues sharing at
# least this much overlap, so we never spend an LLM call on clearly-unrelated
# issues. Sits below `_CROSS_THREAD_SIMILARITY_THRESHOLD` (≥ that auto-merges).
_SEMANTIC_DEDUP_HINT: Final[float] = 0.2

_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Lower-cased word tokens for the similarity tie-breaker."""
    return set(_WORD_RE.findall((text or "").lower()))


def _jaccard(a: str, b: str) -> float:
    """Jaccard overlap of the word sets of two strings (0.0 when both empty)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


class IssueStore:
    """JSON-backed store of agent state: cursor, issues, tombstones (§5.6/§6).

    Call `load()` once at startup, mutate via `upsert`/`tombstone`/`set_cursor`,
    then `save()` (atomic) to persist. The in-memory `AgentState` is the single
    source of truth between load and save.
    """

    def __init__(self, state_file: str):
        self.state_file = state_file
        self.state: AgentState = AgentState()
        # fingerprint -> Issue, kept in lock-step with `state.issues` for O(1)
        # dedup; rebuilt on every load and after each structural mutation.
        self._by_fp: dict[str, Issue] = {}

    # --- persistence --------------------------------------------------------
    def load(self) -> None:
        """Load state from `state_file`. A missing/empty/corrupt file yields a
        fresh empty `AgentState` (first run needs no pre-existing file)."""
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            self.state = AgentState()
            self._reindex()
            return
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            data = {}
        try:
            self.state = AgentState.from_dict(data if isinstance(data, dict) else {})
        except (KeyError, ValueError, TypeError):
            # Structurally-valid JSON but malformed for our schema (e.g. a
            # hand-edited file missing required keys) — fall back to fresh state
            # rather than crash, honoring the "corrupt → fresh" promise above.
            self.state = AgentState()
        self._reindex()

    def save(self) -> None:
        """Persist state atomically: write a temp file in the same directory,
        ``fsync``, then ``os.replace`` over the target (``mkdir -p`` first).

        Before the replace, the current (last-known-good) state file is copied to
        ``<state_file>.bak`` — a one-deep rollback if a later save ever writes
        logically-bad-but-valid-JSON state (the atomic write already prevents a
        torn file). Best-effort: a backup failure never blocks the save."""
        directory = os.path.dirname(self.state_file) or "."
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(self.state.to_dict(), indent=2, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".issues-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            self._backup_existing()
            os.replace(tmp_path, self.state_file)
        except BaseException:
            # Best-effort cleanup of the temp file on any failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _backup_existing(self) -> None:
        """Copy the current state file to ``<state_file>.bak`` (last-known-good),
        best-effort — a missing file or copy error must not block the save."""
        try:
            shutil.copy2(self.state_file, self.state_file + ".bak")
        except (FileNotFoundError, OSError):
            pass

    # --- issue dedup / merge ------------------------------------------------
    def upsert(
        self, candidate: Issue, *, semantic_match: "SemanticMatch | None" = None
    ) -> Issue | None:
        """Add `candidate` or merge it into a matching open issue (§6).

        Dedup runs against open issues plus the tombstone set:
        - If `candidate.fingerprint` is tombstoned (a resolved/stale issue from
          the same root) it is **not** re-raised — returns ``TOMBSTONED`` (None).
        - If an open issue shares the fingerprint (or, as a same-thread tie-breaker,
          has a highly similar title/summary), the candidate's new
          `source_message_ids` (and any new `missing_info`) are merged in and the
          existing issue is returned.
        - If an open issue in a DIFFERENT thread is a near-duplicate (a second
          reporter independently raising the same incident — title/summary overlap
          at/above the stricter cross-thread bar), the candidate is folded into it
          too, so two reports of one incident become ONE issue.
        - Failing that, when `semantic_match` is supplied (the live runner's LLM
          decider), open issues in other threads sharing at least a lexical hint are
          offered to it; if it judges one the same incident, the candidate folds in.
          This catches paraphrases the lexical bar can't (and must not) merge.
        - Otherwise the candidate is tracked as a new open issue and returned.
        """
        fp = candidate.fingerprint
        if self.is_tombstoned(fp):
            return TOMBSTONED

        existing = self._by_fp.get(fp)
        if existing is None:
            existing = self._find_similar(candidate, closed=False)
        if existing is None:
            existing = self._find_cross_thread_duplicate(candidate)
        if existing is None and semantic_match is not None:
            existing = self._semantic_open_match(candidate, semantic_match)
        if existing is not None and existing.status not in _CLOSED_STATUSES:
            self._merge(existing, candidate)
            return existing

        # Category-drift guard: a candidate that closely matches a closed AND
        # tombstoned issue in the same thread is that resolved/stale issue
        # re-detected under a drifted category — a fresh fingerprint the
        # exact-match tombstone set misses. Suppress it like a tombstone rather
        # than re-raise. A genuinely new issue has a distinct title/summary and
        # stays below the similarity threshold, so it survives; a closed-but-not-
        # tombstoned issue (edge case) doesn't suppress either.
        if self._find_similar(candidate, closed=True) is not None:
            return TOMBSTONED

        # New issue: track it and index by fingerprint.
        self.state.issues.append(candidate)
        self._by_fp[fp] = candidate
        return candidate

    def _merge(self, target: Issue, candidate: Issue) -> None:
        """Merge a candidate's new evidence into an existing open issue."""
        target.source_message_ids = _merge_unique(
            target.source_message_ids, candidate.source_message_ids
        )
        target.missing_info = _merge_unique(target.missing_info, candidate.missing_info)
        # Backfill the reporter only if the tracked issue never had one (e.g. state
        # written before this field existed); never overwrite the original reporter.
        if not target.reporter_id and candidate.reporter_id:
            target.reporter_id = candidate.reporter_id
        # Keep the freshest timestamp; never clobber an existing title/summary
        # with empty drift from a re-detection.
        if candidate.updated_at and (
            not target.updated_at or candidate.updated_at > target.updated_at
        ):
            target.updated_at = candidate.updated_at

    def _find_similar(self, candidate: Issue, *, closed: bool) -> Issue | None:
        """Best same-thread issue (highest score) whose title or summary overlaps
        the candidate's at/above the similarity threshold, restricted to open or
        closed issues. Guards against the LLM flipping `category` and minting a
        new fingerprint (§5.2/§6): an *open* match is merged into; a *closed*
        match means the candidate is a re-detection of an already-closed issue
        and `upsert` suppresses it.

        For the closed branch the match is further gated on the tombstone set —
        only a *tombstoned* closed issue suppresses a re-detection (closed ⟹
        tombstoned in the normal runner flow), so a closed-but-not-tombstoned
        issue still allows a fresh detection, as before."""
        best: Issue | None = None
        best_score = _SIMILARITY_THRESHOLD
        for issue in self.state.issues:
            if (issue.status in _CLOSED_STATUSES) != closed:
                continue
            if closed and not self.is_tombstoned(issue.fingerprint):
                continue
            if issue.thread_id != candidate.thread_id:
                continue
            score = max(
                _jaccard(issue.title, candidate.title),
                _jaccard(issue.summary, candidate.summary),
            )
            if score >= best_score:
                best, best_score = issue, score
        return best

    def _find_cross_thread_duplicate(self, candidate: Issue) -> Issue | None:
        """Best OPEN issue in a DIFFERENT thread whose title or summary overlaps
        the candidate's at/above the stricter cross-thread threshold — a *second
        reporter* independently raising the same incident in their own thread (§6).

        Distinct from `_find_similar`, which only ever matches WITHIN a thread:
        here `thread_id` necessarily differs, so the fingerprint differs too and
        the same-thread path can't catch it. We require more lexical overlap than
        the same-thread bar (no thread locality to corroborate) and only ever fold
        into an OPEN issue, so a resolved incident is never silently extended by a
        late duplicate. A genuinely distinct issue stays below the threshold and is
        tracked separately, as before."""
        best: Issue | None = None
        best_score = _CROSS_THREAD_SIMILARITY_THRESHOLD
        for issue in self.state.issues:
            if issue.status in _CLOSED_STATUSES:
                continue
            if issue.thread_id == candidate.thread_id:
                continue  # same-thread is `_find_similar`'s job
            score = max(
                _jaccard(issue.title, candidate.title),
                _jaccard(issue.summary, candidate.summary),
            )
            if score >= best_score:
                best, best_score = issue, score
        return best

    def _semantic_open_match(
        self, candidate: Issue, semantic_match: "SemanticMatch"
    ) -> Issue | None:
        """Last-resort cross-thread dedup: offer the open issues in OTHER threads
        that share at least `_SEMANTIC_DEDUP_HINT` lexical overlap with `candidate`
        to a caller-supplied decider (the runner's LLM), and return the one it
        judges the same incident.

        The hint floor is purely a cost gate — it skips a decider call for issues
        with no lexical relationship at all, while still surfacing the paraphrase
        band the lexical bar can't safely merge. The decider's answer is trusted
        only if it is one of the issues actually offered (identity check), so a
        bad/forged return can never merge into an unrelated or closed issue."""
        candidates = [
            issue
            for issue in self.state.issues
            if issue.status not in _CLOSED_STATUSES
            and issue.thread_id != candidate.thread_id
            and max(
                _jaccard(issue.title, candidate.title),
                _jaccard(issue.summary, candidate.summary),
            )
            >= _SEMANTIC_DEDUP_HINT
        ]
        if not candidates:
            return None
        match = semantic_match(candidate, candidates)
        return match if match in candidates else None

    # --- queries ------------------------------------------------------------
    def open_issues(self) -> list[Issue]:
        """Issues still in the working set (not resolved/stale)."""
        return [i for i in self.state.issues if i.status not in _CLOSED_STATUSES]

    def all_issues(self) -> list[Issue]:
        """Every tracked issue, open or closed, in insertion order."""
        return list(self.state.issues)

    def recent_closed(self, limit: int = 3) -> list[Issue]:
        """The most recently-updated closed (resolved/stale) issues, newest first
        — detection's episodic recall (§ episodic memory). Bounded to `limit`;
        empty on a fresh start (no closed issues yet)."""
        closed = [i for i in self.state.issues if i.status in _CLOSED_STATUSES]
        closed.sort(key=lambda i: (i.updated_at or "", i.id), reverse=True)
        return closed[: max(0, limit)]

    def get(self, fingerprint: str) -> Issue | None:
        """The tracked issue with this fingerprint, or None."""
        return self._by_fp.get(fingerprint)

    # --- tombstones ---------------------------------------------------------
    def tombstone(self, issue: Issue) -> None:
        """Record `issue.fingerprint` so it is not re-raised after resolve/stale
        (§6). Idempotent; leaves the issue itself in `state.issues` (its closed
        status keeps it out of the open set) so history/reporting survives."""
        fp = issue.fingerprint
        if fp and fp not in self.state.tombstones:
            self.state.tombstones.append(fp)

    def is_tombstoned(self, fingerprint: str) -> bool:
        """Whether this fingerprint has been tombstoned (resolved/stale)."""
        return bool(fingerprint) and fingerprint in self.state.tombstones

    # --- cursor -------------------------------------------------------------
    def get_cursor(self) -> tuple[str | None, list[str]]:
        """The poll cursor: ``(cursor_message_name, seen_message_ids)`` (§5.4)."""
        return self.state.cursor_message_name, list(self.state.seen_message_ids)

    def set_cursor(self, name: str | None, seen_ids: list[str]) -> None:
        """Advance the poll cursor. `name` is the last processed message resource
        name; `seen_ids` is the recent-id set (bounded to the most recent
        ``_MAX_SEEN_IDS`` to keep the state file small)."""
        self.state.cursor_message_name = name
        deduped = _merge_unique([], seen_ids)
        if len(deduped) > _MAX_SEEN_IDS:
            deduped = deduped[-_MAX_SEEN_IDS:]
        self.state.seen_message_ids = deduped

    # --- report-DM assistant cursor + call bookkeeping (REPORT_ASSISTANT) ----
    def get_report_cursor(self) -> tuple[str | None, list[str]]:
        """The report-DM poll cursor: ``(report_cursor_message_name,
        report_seen_message_ids)`` — the assistant's own cursor over
        GOOGLE_CHAT_REPORT_SPACE, independent of the issue cursor (which tracks
        GOOGLE_SPACE)."""
        return self.state.report_cursor_message_name, list(
            self.state.report_seen_message_ids
        )

    def set_report_cursor(self, name: str | None, seen_ids: list[str]) -> None:
        """Advance the report-DM poll cursor (bounded seen-id set, like the issue
        cursor)."""
        self.state.report_cursor_message_name = name
        deduped = _merge_unique([], seen_ids)
        if len(deduped) > _MAX_SEEN_IDS:
            deduped = deduped[-_MAX_SEEN_IDS:]
        self.state.report_seen_message_ids = deduped

    def get_last_relayed_issue_id(self) -> str | None:
        """The issue id of the most recent outbound call the bot placed, so a
        later "call me back" can re-relay that incident. `None` if none yet."""
        return self.state.last_relayed_issue_id

    def set_last_relayed_issue_id(self, issue_id: str | None) -> None:
        """Record the issue id of the most recent outbound call (for call-back)."""
        self.state.last_relayed_issue_id = issue_id or None

    def has_offered_missed_call(self, message_id: str) -> bool:
        """Whether the assistant has already posted its one proactive call-back
        offer for this MISSED-call message (one-shot guard)."""
        return bool(message_id) and message_id in self.state.missed_calls_offered

    def mark_missed_call_offered(self, message_id: str) -> None:
        """Record that the proactive call-back offer for this MISSED-call message
        has been posted, so it never fires twice. Bounded like the seen-id set."""
        if not message_id or message_id in self.state.missed_calls_offered:
            return
        self.state.missed_calls_offered.append(message_id)
        if len(self.state.missed_calls_offered) > _MAX_SEEN_IDS:
            self.state.missed_calls_offered = self.state.missed_calls_offered[
                -_MAX_SEEN_IDS:
            ]

    # --- bot identity -------------------------------------------------------
    def get_bot_user_id(self) -> str | None:
        """The persisted bot `users/<id>`, learned from its first post and kept
        across restarts so self-filtering (§5.7/§6) survives before the bot posts
        again. `None` until learned."""
        return self.state.bot_user_id

    def set_bot_user_id(self, uid: str | None) -> None:
        """Persist the bot's own `users/<id>` resource name (§5.7/§6)."""
        self.state.bot_user_id = uid or None

    # --- internals ----------------------------------------------------------
    def _reindex(self) -> None:
        """Rebuild the fingerprint index from `state.issues`. On a fingerprint
        collision the first-seen issue wins (stable across reloads)."""
        self._by_fp = {}
        for issue in self.state.issues:
            self._by_fp.setdefault(issue.fingerprint, issue)


def _merge_unique(base: list[str], extra: list[str]) -> list[str]:
    """Concatenate two id/string lists preserving order, dropping duplicates and
    falsy entries."""
    seen: set[str] = set()
    out: list[str] = []
    for value in (*base, *extra):
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
