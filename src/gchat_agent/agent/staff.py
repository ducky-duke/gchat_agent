"""Staff personas — the two LLM-driven demo participants (§5.8 + §11).

A :class:`StaffAgent` is a *thin* participant that reuses the same
:class:`~gchat_agent.llm.base.LLMClient` and :class:`~gchat_agent.chat.base.ChatClient`
as the issue-spotter bot, so it needs no new infrastructure. It does two jobs:

* **seed** — post a scenario's issue-laden messages (deliberately containing
  blockers, missing info, and vague asks) so the bot has something to detect;
* **answer the bot** — when the bot asks a clarifying question in the seeded
  thread, reply *in character*, revealing information **progressively** (one
  detail per reply) so the multi-round "ask until clear" loop is exercised and
  an issue can actually reach ``resolved`` → report.

Each persona = ``{role, facts, withholding_policy, seed_messages}`` from
``data/scenarios.json``. The persona prompt is ``role`` + the ``facts`` it holds
+ the ``withholding_policy``; it steers an OpenRouter LLM in the live demo. The
offline path uses :class:`~gchat_agent.llm.mock.MockLLM`, whose ``chat`` is a
generic acknowledgement, so :class:`StaffAgent` derives the *content* of each
reply deterministically from the persona ``facts`` (and only uses the LLM to
phrase it when the model returns something fact-bearing). That keeps offline
tests reproducible while still exercising the real loop.

Stdlib only.
"""
from __future__ import annotations

import json
import os
from typing import Any

from gchat_agent.models import Message

# Required keys for every persona entry in scenarios.json.
_REQUIRED_KEYS: tuple[str, ...] = ("role", "facts", "withholding_policy", "seed_messages")

# Map a fact key to the question keywords that should surface it. Order is the
# natural reveal order when a question doesn't clearly target a specific fact.
_FACT_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("owner", ("who", "own", "owner", "assign", "responsible", "drive", "lead")),
    ("deadline", ("when", "deadline", "date", "timeline", "eta", "by when", "target", "live by")),
    ("scope", ("scope", "what exactly", "which", "affected", "impacted", "boundary", "where")),
    ("numbers", ("how many", "how much", "number", "rate", "percent", "%", "amount", "volume", "count")),
    ("repro_steps", ("repro", "reproduce", "steps", "how do", "trigger", "happen")),
    ("terms", ("terms", "wagering", "condition", "rule", "expire", "cap", "limit")),
    ("kyc", ("kyc", "compliance", "verify", "verification", "aml")),
    ("budget", ("budget", "cost", "spend", "approved")),
    ("impact", ("impact", "affect", "player", "customer", "severity")),
    ("root_cause", ("cause", "why", "root", "reason")),
    ("ticket", ("ticket", "jira", "tracked", "reference", "id")),
)


class StaffAgent:
    """A scenario-driven Chat participant (§5.8).

    Seeds an issue and answers the bot's clarifying questions in character,
    revealing one held fact per reply so the bot's multi-round loop runs to a
    resolution.
    """

    def __init__(self, llm: Any, chat: Any, persona: dict) -> None:
        """``llm`` implements ``LLMClient``, ``chat`` implements ``ChatClient``,
        and ``persona`` is one entry from :func:`load_personas` (a dict with
        ``role``, ``facts``, ``withholding_policy``, ``seed_messages``)."""
        self.llm = llm
        self.chat = chat
        self.persona = persona or {}
        self.role: str = str(self.persona.get("role", "")).strip()
        self.facts: dict[str, Any] = dict(self.persona.get("facts", {}) or {})
        self.withholding_policy: str = str(self.persona.get("withholding_policy", "")).strip()
        self.seed_messages: list[str] = list(self.persona.get("seed_messages", []) or [])
        # Per-thread set of fact keys already revealed, so reveals are
        # progressive and never repeat within a thread.
        self._revealed: dict[str, set[str]] = {}
        # The thread the seed messages were posted into (set by ``seed``).
        self.seed_thread_id: str | None = None

    # --- job (a): seed the scenario -----------------------------------------
    def seed(self) -> list[Message]:
        """Post the persona's seed messages and return the created messages.

        The first message starts a new thread; later seed messages reply into
        that same thread so the scenario reads as one conversation. A stable
        ``request_id`` per seed message keeps re-runs idempotent.
        """
        posted: list[Message] = []
        for index, text in enumerate(self.seed_messages):
            if not str(text).strip():
                continue
            request_id = f"staff-{self._persona_slug()}-seed-{index}"
            message = self.chat.post_message(
                text=str(text),
                thread_id=self.seed_thread_id,
                request_id=request_id,
            )
            posted.append(message)
            # Anchor subsequent seed messages to the thread created by the first.
            if self.seed_thread_id is None and message is not None:
                self.seed_thread_id = message.thread_id or None
        return posted

    # --- job (b): answer the bot's clarifying questions ----------------------
    def answer_question(self, thread_id: str, question_text: str) -> Message | None:
        """Reply to a bot clarifying question in ``thread_id``, in character.

        Reveals the single most relevant *unrevealed* held fact (progressive
        disclosure). Returns the posted reply ``Message``, or ``None`` when the
        persona has nothing left to disclose for this thread.
        """
        if not self.facts:
            return None
        key = self._pick_fact(thread_id, question_text or "")
        if key is None:
            return None  # everything already disclosed; stay quiet
        self._revealed.setdefault(thread_id, set()).add(key)

        reply_text = self._compose_reply(key, question_text or "")
        request_id = f"staff-{self._persona_slug()}-ans-{key}"
        return self.chat.post_message(
            text=reply_text,
            thread_id=thread_id,
            request_id=request_id,
        )

    # --- persona prompt ------------------------------------------------------
    def persona_system_prompt(self) -> str:
        """The persona system prompt = role + held facts + withholding policy.

        Used to steer an OpenRouter LLM in the live demo; harmless offline (the
        mock ignores semantics)."""
        facts_block = "\n".join(f"- {k}: {v}" for k, v in self.facts.items()) or "- (none)"
        parts = [
            self.role or "You are a member of staff in an iGaming work chat.",
            "Facts you know (reveal only when directly asked, one at a time):",
            facts_block,
        ]
        if self.withholding_policy:
            parts.append(f"Withholding policy: {self.withholding_policy}")
        return "\n\n".join(parts)

    # --- internal helpers ----------------------------------------------------
    def _pick_fact(self, thread_id: str, question_text: str) -> str | None:
        """Choose the next fact key to reveal: prefer one the question targets,
        else the next held fact in natural order — skipping already-revealed
        keys for this thread."""
        revealed = self._revealed.get(thread_id, set())
        q = question_text.lower()

        # 1) A fact whose cue words appear in the question and isn't yet revealed.
        for key, cues in _FACT_CUES:
            if key in self.facts and key not in revealed and any(c in q for c in cues):
                return key

        # 2) Otherwise the next unrevealed fact, honoring the cue ordering first
        #    then any extra keys present in the scenario.
        ordered_keys = [k for k, _ in _FACT_CUES if k in self.facts]
        ordered_keys += [k for k in self.facts if k not in ordered_keys]
        for key in ordered_keys:
            if key not in revealed:
                return key
        return None

    def _compose_reply(self, key: str, question_text: str) -> str:
        """Build the reply text for revealing ``facts[key]``.

        Offline (MockLLM), the LLM's ``chat`` returns a generic acknowledgement
        with no fact content, so we use the fact value directly — that's what
        lets the bot's clarity check eventually pass. When the LLM returns text
        that actually surfaces the fact (a real model following the persona
        prompt), we prefer the model's phrasing.
        """
        fact_value = str(self.facts.get(key, "")).strip()
        llm_text = self._llm_phrasing(question_text, fact_value)
        if llm_text and self._mentions_fact(llm_text, fact_value):
            return self._dedupe_repeat(llm_text)
        return fact_value or "Let me check and get back to you."

    @staticmethod
    def _dedupe_repeat(text: str) -> str:
        """Collapse a verbatim doubled reply to a single copy. Some models (e.g.
        minimax) echo the whole answer twice — 'Sentence.Sentence.' — which would
        otherwise land doubled in the transcript and the resolution report."""
        s = (text or "").strip()
        n = len(s)
        if n < 2:
            return s
        # Exact 'XX' with no separator (the observed behavior).
        if n % 2 == 0 and s[: n // 2] == s[n // 2:]:
            return s[: n // 2].strip()
        # 'X<sep>X' with a small whitespace separator near the midpoint.
        half = n // 2
        for i in range(max(1, half - 1), half + 2):
            a, b = s[:i].strip(), s[i:].strip()
            if a and a == b:
                return a
        return s

    def _llm_phrasing(self, question_text: str, fact_value: str) -> str:
        """Ask the LLM to phrase the answer in character; tolerate any failure.

        Returns the model text (possibly a generic mock acknowledgement, which
        the caller will reject for not containing the fact) or "" on error."""
        try:
            system = self.persona_system_prompt()
            user = (
                f"A teammate asked: {question_text}\n\n"
                f"Answer in character with exactly this detail and nothing more: "
                f"{fact_value}"
            )
            reply = self.llm.chat(system, [{"role": "user", "content": user}])
            return str(reply or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _mentions_fact(text: str, fact_value: str) -> bool:
        """True if ``text`` plausibly contains the fact (loose substring of a
        salient token), so we only trust real LLM phrasing, not the mock's
        generic acknowledgement."""
        if not fact_value:
            return False
        if fact_value.lower() in text.lower():
            return True
        # Match on a distinctive token (numbers, codes) from the fact.
        for token in fact_value.replace(",", " ").split():
            t = token.strip(".;:()").lower()
            if len(t) >= 4 and any(ch.isdigit() for ch in t) and t in text.lower():
                return True
        return False

    def _persona_slug(self) -> str:
        """A stable, filesystem/id-safe slug for request ids, from the role."""
        base = (self.role.split(",")[0] if self.role else "staff").lower()
        slug = "".join(ch if ch.isalnum() else "-" for ch in base).strip("-")
        slug = "-".join(p for p in slug.split("-") if p)
        return slug[:32] or "staff"


def load_personas(path: str = "data/scenarios.json") -> dict:
    """Load the staff personas from ``scenarios.json`` and validate them.

    Returns a dict mapping persona id (e.g. ``"ops"``, ``"promo"``) to its
    config dict. Raises ``FileNotFoundError`` if ``path`` is missing and
    ``ValueError`` if the JSON is malformed or a persona is missing a required
    key (``role``, ``facts``, ``withholding_policy``, ``seed_messages``).
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"scenarios file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError(f"{path} must be a non-empty object of personas")
    for persona_id, persona in data.items():
        if not isinstance(persona, dict):
            raise ValueError(f"persona {persona_id!r} must be an object")
        missing = [k for k in _REQUIRED_KEYS if k not in persona]
        if missing:
            raise ValueError(
                f"persona {persona_id!r} missing required key(s): {', '.join(missing)}"
            )
    return data
