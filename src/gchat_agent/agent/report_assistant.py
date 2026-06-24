"""Two-way assistant in the report DM (REPORT_ASSISTANT).

`GOOGLE_CHAT_REPORT_SPACE` is the channel where the bot REPORTS incidents to a
human (resolution reports land there and the outbound voice call on resolve rings
there). This module makes it bidirectional: the human can chat with the AI in that
DM — ask about the incidents/reports the bot has filed, and request a call-back
(especially after a missed call).

It is driven by the SAME poller process: `Runner.run_cycle` calls
`ReportAssistant.step(own_id)` once per cycle, sharing the one `IssueStore` and the
one single-runner lock, so there is no concurrent-write race on the state file. The
assistant uses a SEPARATE `ChatClient` bound to the report DM, so report-DM traffic
never enters issue detection (which reads `GOOGLE_SPACE`).

Three things happen per step:
  1. **Missed-call heads-up** — a new message carrying a `meetSpaceLinkData`
     annotation with `huddleStatus=MISSED` is the bot's own outbound call that
     wasn't picked up; with `REPORT_MISSED_CALL_OFFER` on, the assistant posts ONE
     proactive offer to call back (one-shot per missed call).
  2. **Call-back** — a human message that asks to be called back (a deterministic,
     multilingual keyword check, so the critical action never depends on LLM
     phrasing) re-relays the most recent incident via the runner-supplied
     `call_back` callable.
  3. **Chat** — any other human message gets a concise LLM reply grounded in the
     tracked issues + their on-disk reports (the report content is framed as
     UNTRUSTED data, like every other transcript-bearing LLM call).

Stdlib only; the LLM stays behind its existing client.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Optional

from . import prompts

if TYPE_CHECKING:
    from ..config import Config
    from ..chat.base import ChatClient
    from ..llm.base import LLMClient
    from ..models import Issue, Message
    from .state import IssueStore

# Mirror the runner's cursor constants: a generous seen-id window and a small
# look-back skew so an equal-`createTime` message at the boundary isn't dropped.
_SEEN_WINDOW = 500
_CURSOR_SKEW_SECONDS = 2
# How many recent DM messages to carry as LLM chat context (both sides).
_HISTORY_WINDOW = 20
# How many full reports to inline into the assistant's context, and the per-report
# character cap, so a long backlog can't blow the prompt budget.
_MAX_FULL_REPORTS = 2
_MAX_REPORT_CHARS = 4000

# A call's terminal "not answered" lifecycle state (see call/huddle_watch.py).
_MISSED = "MISSED"

# Phrases (any language) that mean "call me back". Matched as a normalized
# substring so the deterministic call-back path never hinges on LLM phrasing.
_CALLBACK_PHRASES: tuple[str, ...] = (
    "call me", "call back", "callback", "call again", "ring me", "ring back",
    "phone me", "give me a call", "call me back",
    # Vietnamese
    "gọi lại", "gọi cho", "gọi tôi", "gọi mình", "gọi điện", "gọi em", "gọi anh",
    "call lại",
)


def _now() -> str:
    """A UTC RFC-3339 timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def _minus_seconds(ts: str, seconds: int) -> str:
    """Shift an RFC-3339 timestamp earlier by `seconds` (best-effort)."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ts
    return (dt - timedelta(seconds=seconds)).isoformat()


def looks_like_callback_request(text: str) -> bool:
    """Whether a message is asking the bot to call the human back. A normalized
    substring match on `_CALLBACK_PHRASES` (case-insensitive, whitespace
    collapsed) — deterministic and language-aware, so the call-back fires
    reliably regardless of the LLM."""
    low = " ".join((text or "").lower().split())
    if not low:
        return False
    return any(phrase in low for phrase in _CALLBACK_PHRASES)


def huddle_status(message: "Message") -> str | None:
    """The call `huddleStatus` carried by a message's `meetSpaceLinkData`
    annotation, or None for an ordinary message. Mirrors `call/huddle_watch.py`'s
    parse: `annotation.richLinkMetadata.meetSpaceLinkData.huddleStatus`."""
    for ann in getattr(message, "annotations", None) or []:
        if not isinstance(ann, dict):
            continue
        meta = ann.get("richLinkMetadata") or {}
        data = meta.get("meetSpaceLinkData") or {}
        status = data.get("huddleStatus")
        if status:
            return str(status)
    return None


class ReportAssistant:
    """Conversational assistant over the report DM (`GOOGLE_CHAT_REPORT_SPACE`)."""

    def __init__(
        self,
        chat: "ChatClient",
        store: "IssueStore",
        config: "Config",
        llm: "Optional[LLMClient]",
        reports_dir: str,
        call_back: "Callable[[], bool]",
    ) -> None:
        # `chat` is bound to the report DM (NOT the monitored GOOGLE_SPACE).
        self.chat = chat
        self.store = store
        self.config = config
        self._llm = llm
        self.reports_dir = reports_dir
        # Place an outbound call-back (re-relays the last incident); returns True
        # iff a call was actually launched. Supplied by the Runner so call spawning
        # + serialization stay in one place (the resolve-time call shares the same
        # single-call hardware).
        self._call_back = call_back
        # Recent DM messages (both sides), for LLM chat context across cycles.
        self._history: list["Message"] = []
        self._own_id: str | None = None

    # --- one step (called once per runner cycle) ---------------------------
    def step(self, own_id: str | None) -> dict:
        """Service the report DM for one cycle: fetch new messages, post any
        missed-call offer, then reply to / act on each new human message. Returns
        a small summary dict (replied / called / offered counts)."""
        # Without a known bot id we cannot tell our OWN posts (reports,
        # confirmations) from a human's, so a chat turn could answer our own
        # message and loop. The runner resolves the id via tokeninfo on cycle 1,
        # so this is only the rare bootstrap gap — skip the step until we know it.
        if own_id is None:
            return {"replied": 0, "called": 0, "offered": 0}
        self._own_id = own_id

        new = self._fetch_new()
        replied = called = offered = 0
        for m in new:
            # Missed-call detection runs for ANY sender (the lifecycle message may
            # be authored by the bot's own call), so check it before the self skip.
            if huddle_status(m) == _MISSED:
                if (
                    self.config.REPORT_MISSED_CALL_OFFER
                    and not self.store.has_offered_missed_call(m.id)
                ):
                    self._offer_missed_call(m)
                    self.store.mark_missed_call_offered(m.id)
                    offered += 1
                continue
            # Ordinary chat turns: human-authored text only.
            if m.sender == own_id or not (m.text or "").strip():
                continue
            if looks_like_callback_request(m.text):
                placed = bool(self._call_back())
                self.chat.post_reply(
                    m, self._callback_reply(placed),
                    request_id=f"assist-cb-{m.id}",
                )
                called += 1
            else:
                reply = self._generate_reply(m)
                if reply:
                    self.chat.post_reply(
                        m, reply, request_id=f"assist-{m.id}"
                    )
                    replied += 1
        return {"replied": replied, "called": called, "offered": offered}

    # --- fetch / cursor (own cursor over the report DM) --------------------
    def _fetch_new(self) -> list["Message"]:
        """Fetch report-DM messages after the report cursor, drop already-seen
        ids, append to the rolling history, and advance the cursor. First run pins
        the cursor to *now* (no backfill — old reports must not be replayed as
        chat)."""
        cursor_name, seen = self.store.get_report_cursor()
        seen_set = set(seen)
        since = self._since(cursor_name)

        fetched = self.chat.fetch_messages(since)
        new = [m for m in fetched if m.id and m.id not in seen_set]

        for m in new:
            self._history.append(m)
        if len(self._history) > _HISTORY_WINDOW:
            self._history = self._history[-_HISTORY_WINDOW:]

        if new:
            latest = new[-1]
            updated_seen = list(seen) + [m.id for m in new]
            self.store.set_report_cursor(
                latest.create_time or cursor_name,
                updated_seen[-_SEEN_WINDOW:],
            )
        elif cursor_name is None and not seen:
            self.store.set_report_cursor(_now(), [])
        return new

    def _since(self, cursor_name: str | None) -> str | None:
        """The fetch boundary: the latest create_time we hold, else the persisted
        pin, else *now* (no history backfill for the report DM)."""
        times = [m.create_time for m in self._history if m.create_time]
        latest = max(times) if times else None
        if latest:
            return _minus_seconds(latest, _CURSOR_SKEW_SECONDS)
        if cursor_name:
            return _minus_seconds(cursor_name, _CURSOR_SKEW_SECONDS)
        return _now()

    # --- replies -----------------------------------------------------------
    def _generate_reply(self, message: "Message") -> str:
        """Draft a concise LLM reply to a human DM message, grounded in the
        tracked incidents + their reports. Returns "" if no LLM is configured."""
        if self._llm is None:
            return ""
        system = (
            prompts.report_assistant_system_prompt()
            + "\n\n"
            + prompts.render_report_context(
                self.store.open_issues(),
                self.store.recent_closed(limit=5),
                self._relevant_reports(message.text or ""),
            )
        )
        chat_messages = self._chat_messages()
        try:
            reply = self._llm.chat(system, chat_messages)
        except Exception as exc:  # noqa: BLE001 — never crash the cycle on an LLM error
            import sys

            print(
                f"[report-assistant] reply generation failed: {exc}",
                file=sys.stderr,
            )
            return ""
        return (reply or "").strip()

    def _chat_messages(self) -> list[dict[str, str]]:
        """The recent DM as an LLM message list: the bot's own posts map to
        `assistant`, everyone else to `user`. Drives multi-turn continuity."""
        out: list[dict[str, str]] = []
        for m in self._history[-_HISTORY_WINDOW:]:
            text = (m.text or "").strip()
            if not text:
                continue
            role = "assistant" if m.sender == self._own_id else "user"
            out.append({"role": role, "content": text})
        return out

    def _relevant_reports(self, focus_text: str) -> list[tuple[str, str, str]]:
        """Full report Markdown to inline into the assistant's context: any issue
        whose id appears in the user's message, plus the most-recently-closed
        issue's report — capped at `_MAX_FULL_REPORTS`."""
        out: list[tuple[str, str, str]] = []
        included: set[str] = set()
        low = (focus_text or "").lower()

        def _add(issue: "Issue") -> None:
            if issue.id in included or len(out) >= _MAX_FULL_REPORTS:
                return
            md = self._read_report(issue.id)
            if md:
                out.append((issue.id, issue.title, md))
                included.add(issue.id)

        for issue in self.store.all_issues():
            if issue.id and issue.id.lower() in low:
                _add(issue)
        for issue in self.store.recent_closed(limit=1):
            _add(issue)
        return out

    def _read_report(self, issue_id: str) -> str | None:
        """The on-disk report Markdown for an issue (bounded), or None if absent."""
        if not issue_id:
            return None
        path = os.path.join(self.reports_dir, f"issue-{issue_id}.md")
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (FileNotFoundError, OSError):
            return None
        if len(text) > _MAX_REPORT_CHARS:
            text = text[:_MAX_REPORT_CHARS].rstrip() + "\n…(truncated)"
        return text

    # --- call-back + missed-call -------------------------------------------
    def _offer_missed_call(self, message: "Message") -> None:
        """Post ONE proactive heads-up that an outbound call was missed, offering
        to ring again. References the last relayed incident when known. Posted
        top-level (the trigger is a call-lifecycle message, not a chat turn)."""
        title = self._last_incident_title()
        about = f" about “{title}”" if title else ""
        text = (
            f"📞 I tried to call you{about} but the call was missed. "
            "Reply “call me” and I'll ring you again — or just ask me here and "
            "I'll summarize what's going on."
        )
        self.chat.post_message(
            text, thread_id=None, request_id=f"assist-missed-{message.id}"
        )

    @staticmethod
    def _callback_reply(placed: bool) -> str:
        """The confirmation posted in reply to a call-back request."""
        if placed:
            return (
                "📞 Calling you back now — pick up and I'll walk you through the "
                "latest incident."
            )
        return (
            "I couldn't place the call right now (one may already be in progress, "
            "or calling isn't set up on this machine). I can summarize the latest "
            "report right here instead — just ask."
        )

    def _last_incident_title(self) -> str | None:
        """The title of the most recently relayed incident, for the missed-call
        offer. Falls back to the most-recently-closed issue's title."""
        issue_id = self.store.get_last_relayed_issue_id()
        if issue_id:
            for issue in self.store.all_issues():
                if issue.id == issue_id:
                    return (issue.title or "").strip() or None
        recent = self.store.recent_closed(limit=1)
        if recent:
            return (recent[0].title or "").strip() or None
        return None
