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
import tempfile
from typing import Final

from ..models import AgentState, Issue, Status

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
# the fingerprint differs (e.g. the LLM flipped the category).
_SIMILARITY_THRESHOLD: Final[float] = 0.6

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
        ``fsync``, then ``os.replace`` over the target (``mkdir -p`` first)."""
        directory = os.path.dirname(self.state_file) or "."
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(self.state.to_dict(), indent=2, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".issues-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.state_file)
        except BaseException:
            # Best-effort cleanup of the temp file on any failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # --- issue dedup / merge ------------------------------------------------
    def upsert(self, candidate: Issue) -> Issue | None:
        """Add `candidate` or merge it into a matching open issue (§6).

        Dedup runs against open issues plus the tombstone set:
        - If `candidate.fingerprint` is tombstoned (a resolved/stale issue from
          the same root) it is **not** re-raised — returns ``TOMBSTONED`` (None).
        - If an open issue shares the fingerprint (or, as a tie-breaker, has a
          highly similar title/summary), the candidate's new `source_message_ids`
          (and any new `missing_info`) are merged in and the existing issue is
          returned.
        - Otherwise the candidate is tracked as a new open issue and returned.
        """
        fp = candidate.fingerprint
        if self.is_tombstoned(fp):
            return TOMBSTONED

        existing = self._by_fp.get(fp)
        if existing is None:
            existing = self._find_similar_open(candidate)
        if existing is not None and existing.status not in _CLOSED_STATUSES:
            self._merge(existing, candidate)
            return existing

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
        # Keep the freshest timestamp; never clobber an existing title/summary
        # with empty drift from a re-detection.
        if candidate.updated_at and (
            not target.updated_at or candidate.updated_at > target.updated_at
        ):
            target.updated_at = candidate.updated_at

    def _find_similar_open(self, candidate: Issue) -> Issue | None:
        """Secondary tie-breaker: an open issue in the same thread whose title or
        summary overlaps the candidate's strongly enough to be the same issue
        (guards against the LLM flipping `category` and minting a new
        fingerprint, §5.2/§6)."""
        best: Issue | None = None
        best_score = _SIMILARITY_THRESHOLD
        for issue in self.state.issues:
            if issue.status in _CLOSED_STATUSES:
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

    # --- queries ------------------------------------------------------------
    def open_issues(self) -> list[Issue]:
        """Issues still in the working set (not resolved/stale)."""
        return [i for i in self.state.issues if i.status not in _CLOSED_STATUSES]

    def all_issues(self) -> list[Issue]:
        """Every tracked issue, open or closed, in insertion order."""
        return list(self.state.issues)

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
