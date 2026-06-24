"""Standalone two-way incident chat assistant (scripts/apigw_chat.py).

A focused, self-contained sibling of `report_assistant.ReportAssistant`. It lets a
human FREELY chat with the AI about ONE fixed incident (e.g. the `apigw` / API
gateway timeout scenario) in a Google Chat DM, and request a voice call by simply
texting — the "chat about it, then ask me to call" demo the `chat_apigw.sh`
launcher drives.

Two things differ from `ReportAssistant`:
  * **Grounding** — it is NOT tied to the `IssueStore`. It answers from a single
    pre-rendered incident brief (the system prompt), so it works as a one-shot demo
    against a scenario the bot never "resolved".
  * **State** — its poll cursor lives in memory (it is a manually-launched loop, not
    the restart-resilient poller), so there is nothing to persist.

Each step mirrors `ReportAssistant.step` so behavior stays consistent:
  1. **Missed-call heads-up** — a polled message carrying a `huddleStatus=MISSED`
     annotation (the bot's own unanswered outbound call) gets ONE proactive offer
     to call back (gated by `REPORT_MISSED_CALL_OFFER`).
  2. **Call-back** — a human "call me" message (deterministic, multilingual
     keyword check via `looks_like_callback_request`) invokes the caller-supplied
     `call_back` callable. Once a call is actually placed the incident is marked
     REPORTED: the assistant flips to a "handled/closed — nothing left to report"
     posture (a trusted status directive is prepended to the system prompt) while
     still answering about it from the brief as history. A later missed-call
     lifecycle message re-opens it, so the offer/re-ring path stays consistent.
  3. **Chat** — any other human message gets a concise LLM reply grounded in the
     fixed incident brief (the brief + the user's message are framed as UNTRUSTED
     data, like every other transcript-bearing LLM call).

Stdlib only; the LLM stays behind its existing client. The voice call is spawned by
the caller-supplied `call_back` (the entry script owns the subprocess), so this
module never imports the heavy `call/` subsystem — the same boundary the runner
keeps.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Callable, Optional

# Reuse the report-DM assistant's shared, already-tested intent/annotation logic
# and cursor constants so the two assistants can never drift apart.
from .report_assistant import (
    _CURSOR_SKEW_SECONDS,
    _HISTORY_WINDOW,
    _MISSED,
    _SEEN_WINDOW,
    _minus_seconds,
    _now,
    huddle_status,
    looks_like_callback_request,
)

if TYPE_CHECKING:
    from ..chat.base import ChatClient
    from ..config import Config
    from ..llm.base import LLMClient
    from ..models import Message


# Prepended (as a trusted, system-set directive) to the system prompt once the
# incident has been reported to the engineer by phone. It flips the assistant's
# posture from "an open incident we're discussing" to "handled/closed — answer
# from history". Prepended ahead of the whole prompt — i.e. BEFORE the UNTRUSTED
# brief block — so neither the brief nor a chat message can forge it.
_REPORTED_STATUS = (
    "SYSTEM STATUS (authoritative — set by the system, NOT from chat or the "
    "incident data below): The on-call engineer has now been called and walked "
    "through this incident by phone, so it is REPORTED and considered "
    "handled/closed. There is no open action and nothing left to report. If the "
    "engineer asks about it, recount it from the incident knowledge as history; "
    "do not re-open it or imply work is still pending unless they say it recurred."
)


class IncidentChatAssistant:
    """Conversational assistant over a DM, grounded in ONE fixed incident brief."""

    def __init__(
        self,
        chat: "ChatClient",
        config: "Config",
        llm: "Optional[LLMClient]",
        *,
        system_prompt: str,
        call_back: "Callable[[], bool]",
        incident_title: str = "",
        cursor: str | None = None,
    ) -> None:
        # `chat` is bound to the DM the assistant services (the report DM).
        self.chat = chat
        self.config = config
        self._llm = llm
        # The full system prompt: the incident-duty role + the rendered incident
        # brief (the only knowledge the AI answers from).
        self._system = system_prompt
        # Place an outbound call; returns True iff one was actually launched. Owned
        # by the entry script (subprocess spawn + one-call serialization).
        self._call_back = call_back
        self._title = (incident_title or "").strip()
        # In-memory poll state (no IssueStore): recent DM messages for LLM context,
        # the seen-id set, and the fetch cursor. `cursor=None` ⇒ first step pins to
        # *now* (no history backfill); a caller/test may seed a past cursor.
        self._history: list["Message"] = []
        self._seen: set[str] = set()
        self._cursor: str | None = cursor
        self._own_id: str | None = None
        self._offered_missed: set[str] = set()
        # Flips True once a call is actually placed (the incident has been
        # reported by phone) and back to False if that call is later MISSED. Drives
        # the "handled/closed — nothing left to report" posture; in-memory only.
        self._reported: bool = False

    # --- one step (called once per poll cycle) -----------------------------
    def step(self, own_id: str | None) -> dict:
        """Service the DM for one cycle: fetch new messages, post any missed-call
        offer, then reply to / act on each new human message. Returns a small
        summary dict (replied / called / offered counts)."""
        # Without a known bot id we cannot tell our OWN posts from a human's, so a
        # chat turn could answer our own message and loop. Skip until it's known.
        if own_id is None:
            return {"replied": 0, "called": 0, "offered": 0}
        self._own_id = own_id

        new = self._fetch_new()
        replied = called = offered = 0
        for m in new:
            # Missed-call detection runs for ANY sender (the lifecycle message is
            # authored by the bot's own call), so check it before the self skip.
            if huddle_status(m) == _MISSED:
                # The report didn't land — re-open it so the AI stops claiming it's
                # handled and the offer/re-ring path reads consistently.
                self._reported = False
                if (
                    self.config.REPORT_MISSED_CALL_OFFER
                    and m.id not in self._offered_missed
                ):
                    self._offer_missed_call(m)
                    self._offered_missed.add(m.id)
                    offered += 1
                continue
            # Ordinary chat turns: human-authored text only.
            if m.sender == own_id or not (m.text or "").strip():
                continue
            if looks_like_callback_request(m.text):
                placed = bool(self._call_back())
                # A placed call relays the incident → mark it reported/closed.
                if placed:
                    self._reported = True
                self.chat.post_reply(
                    m, self._callback_reply(placed), request_id=f"apigw-cb-{m.id}"
                )
                called += 1
            else:
                reply = self._generate_reply()
                if reply:
                    self.chat.post_reply(m, reply, request_id=f"apigw-{m.id}")
                    replied += 1
        return {"replied": replied, "called": called, "offered": offered}

    # --- fetch / cursor (in-memory, over the serviced DM) ------------------
    def _fetch_new(self) -> list["Message"]:
        """Fetch DM messages after the cursor, drop already-seen ids, append to the
        rolling history, and advance the cursor. First step pins the cursor to *now*
        (no backfill — old chatter must not be replayed)."""
        since = self._since()
        fetched = self.chat.fetch_messages(since)
        new = [m for m in fetched if m.id and m.id not in self._seen]

        for m in new:
            self._seen.add(m.id)
            self._history.append(m)
        if len(self._history) > _HISTORY_WINDOW:
            self._history = self._history[-_HISTORY_WINDOW:]

        if new:
            self._cursor = new[-1].create_time or self._cursor
        elif self._cursor is None:
            self._cursor = _now()

        # Bound the seen set: when it grows past the window, keep only the ids still
        # in the (cursor-advanced) history. The cursor prevents older ids re-fetching.
        if len(self._seen) > _SEEN_WINDOW:
            self._seen = {m.id for m in self._history if m.id}
        return new

    def _since(self) -> str | None:
        """The fetch boundary: the latest create_time we hold, else the cursor,
        else *now* (no history backfill). Shifted back by `_CURSOR_SKEW_SECONDS` so
        an equal-timestamp boundary message isn't dropped (the seen-id set dedups)."""
        times = [m.create_time for m in self._history if m.create_time]
        latest = max(times) if times else None
        if latest:
            return _minus_seconds(latest, _CURSOR_SKEW_SECONDS)
        if self._cursor:
            return _minus_seconds(self._cursor, _CURSOR_SKEW_SECONDS)
        return _now()

    # --- replies -----------------------------------------------------------
    def _generate_reply(self) -> str:
        """Draft a concise LLM reply grounded in the fixed incident brief, over the
        recent DM history. Returns "" if no LLM is configured or it errors."""
        if self._llm is None:
            return ""
        try:
            reply = self._llm.chat(self._effective_system(), self._chat_messages())
        except Exception as exc:  # noqa: BLE001 — never crash the loop on an LLM error
            print(f"[apigw-chat] reply generation failed: {exc}", file=sys.stderr)
            return ""
        return (reply or "").strip()

    def _effective_system(self) -> str:
        """The system prompt fed to the LLM. Once the incident has been reported
        (a call was placed), a trusted status directive is PREPENDED so the AI
        treats it as handled/closed while still answering from the brief as
        history. Prepended — not appended — so it sits ahead of the UNTRUSTED
        brief block and neither the brief nor a chat message can forge it."""
        if not self._reported:
            return self._system
        return _REPORTED_STATUS + "\n\n" + self._system

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

    # --- call-back + missed-call -------------------------------------------
    def _offer_missed_call(self, message: "Message") -> None:
        """Post ONE proactive heads-up that an outbound call was missed, offering to
        ring again. Posted top-level (the trigger is a call-lifecycle message)."""
        about = f" about “{self._title}”" if self._title else ""
        text = (
            f"📞 I tried to call you{about} but the call was missed. "
            "Reply “call me” and I'll ring you again — or just ask me here and "
            "I'll walk you through what's going on."
        )
        self.chat.post_message(
            text, thread_id=None, request_id=f"apigw-missed-{message.id}"
        )

    def _callback_reply(self, placed: bool) -> str:
        """The confirmation posted in reply to a call-back request. On a placed
        call the incident is marked reported/closed (see `step`), so the wording
        closes it out — while still inviting follow-up questions (history). On
        failure it offers to help in chat instead."""
        if placed:
            about = f" “{self._title}”" if self._title else " the incident"
            return (
                f"📞 Calling you now to walk you through{about}. I've marked it as "
                "reported and closed it out — nothing's left open. You can still "
                "ask me about it here anytime."
            )
        return (
            "I couldn't place the call right now (one may already be in progress, "
            "or calling isn't set up on this machine). I can explain the incident "
            "right here instead — just ask."
        )
