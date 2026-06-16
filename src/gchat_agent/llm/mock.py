"""Deterministic, rule-based LLM stand-in (§5.3).

`MockLLM` implements the `LLMClient` protocol with no network and no API key, so
the whole offline test suite runs reproducibly. It never parses the prompt
*semantically* — instead it branches on which `MARK_*` marker (imported from
`gchat_agent.agent.prompts`, so the tokens never drift) appears in the combined
system+user text, then applies cheap keyword/heuristic rules to emit JSON that
matches the LLM JSON CONTRACTS exactly.

Every `complete_json` branch returns the object-wrapper shape the contracts
specify (e.g. ``{"issues": [...]}``) so the foundation's `extract_json` — which
rejects bare arrays — accepts it. `chat` returns a short deterministic string.

Stdlib only.
"""
from __future__ import annotations

import re
from typing import Any

from gchat_agent.agent.prompts import (
    MARK_CLARITY,
    MARK_DETECT,
    MARK_NARRATION,
    MARK_QUESTIONS,
    MARK_RESOLUTION,
)

# --- heuristic vocabularies --------------------------------------------------
# Words that, in a work chat, signal something needs clarification or action.
_ISSUE_SIGNALS: tuple[str, ...] = (
    "fail",
    "failed",
    "failing",
    "error",
    "broken",
    "break",
    "down",
    "outage",
    "blocked",
    "blocker",
    "stuck",
    "not sure",
    "unsure",
    "unclear",
    "need",
    "asap",
    "urgent",
    "soon",
    "vague",
    "missing",
    "issue",
    "problem",
    "bug",
    "crash",
    "timeout",
    "stopped",
    "can't",
    "cannot",
    "doesn't work",
    "not working",
)

# Category keywords -> the category label emitted in the contract.
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("incident", ("down", "outage", "crash", "failing", "failed", "fail", "timeout", "stopped")),
    ("bug", ("bug", "broken", "break", "error", "doesn't work", "not working")),
    ("compliance", ("kyc", "compliance", "aml", "audit", "regulator", "license", "rtp")),
    ("request", ("need", "please", "can you", "request", "could you", "want")),
)

# High-severity signals (escalate the default).
_HIGH_SIGNALS: tuple[str, ...] = (
    "asap",
    "urgent",
    "down",
    "outage",
    "crash",
    "blocked",
    "blocker",
    "production",
    "prod",
    "critical",
)
_LOW_SIGNALS: tuple[str, ...] = ("minor", "whenever", "no rush", "low priority", "nice to have")

# Signals that the conversation has supplied the facts needed to act.
_OWNER_HINTS: tuple[str, ...] = (
    "i'll",
    "i will",
    "i can take",
    "assigned to",
    "owner",
    "will own",
    "i'm on it",
    "i am on it",
    "taking this",
    "i've got",
    "i have got",
    "on it",
)
_DATE_PATTERN = re.compile(
    r"\b("
    r"\d{4}-\d{2}-\d{2}"  # ISO date
    r"|\d{1,2}[:/]\d{2}"  # time or m/d
    r"|today|tomorrow|tonight|eod|cob|end of day"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
    r"|by \w+"
    r")\b",
    re.IGNORECASE,
)
_NUMBER_PATTERN = re.compile(r"\b\d+(?:[.,]\d+)?%?\b")

# A tighter date pattern for the resolution line (concrete dates / day names /
# EOD-style tokens only — avoids the loose "by <word>" match used for clarity).
_ISO_DATE_PATTERN = re.compile(
    r"\b("
    r"\d{4}-\d{2}-\d{2}"
    r"|today|tomorrow|tonight|eod|cob|end of day"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r")\b",
    re.IGNORECASE,
)

# Pull "#<id>" tokens out of the rendered transcript (Conversation.render).
_ID_PATTERN = re.compile(r"#(\S+)")


class MockLLM:
    """A deterministic `LLMClient` for offline tests (§5.3).

    Branches on the `MARK_*` marker present in the prompt; applies keyword and
    pattern heuristics over the transcript text to produce contract-shaped JSON.
    """

    def __init__(self) -> None:  # noqa: D401 - simple, no state
        # Stateless by design so repeated runs are identical.
        pass

    # --- LLMClient protocol --------------------------------------------------
    def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        """Return a short, deterministic assistant reply."""
        last = ""
        for msg in reversed(messages or []):
            if msg.get("role") == "user" and msg.get("content"):
                last = str(msg["content"])
                break
        snippet = " ".join(last.split())[:80]
        if snippet:
            return f"[mock] acknowledged: {snippet}"
        return "[mock] acknowledged."

    def complete_json(
        self,
        system: str,
        user: str,
        schema_hint: str | None = None,
    ) -> dict[str, Any]:
        """Branch on the marker in (system+user) and return contract-shaped JSON."""
        blob = f"{system}\n{user}"
        # Order matters only in that markers are distinct; check each.
        if MARK_DETECT in blob:
            return self._detect(user)
        if MARK_CLARITY in blob:
            return self._assess_clarity(user)
        if MARK_QUESTIONS in blob:
            return self._generate_questions(user)
        if MARK_RESOLUTION in blob:
            return self._summarize_resolution(user)
        if MARK_NARRATION in blob:
            return self._narrate_resolution(user)
        # Unknown task: degrade to an empty-issues object (still a valid object).
        return {"issues": []}

    # --- detect_issues -------------------------------------------------------
    def _detect(self, user: str) -> dict[str, Any]:
        """Scan the transcript for issue signals and emit 1-2 issues."""
        text = user or ""
        lower = text.lower()
        all_ids = _ID_PATTERN.findall(text)

        # Find transcript lines that carry an issue signal, capturing their id.
        flagged: list[tuple[str, str]] = []  # (message_id, line_text)
        for line in text.splitlines():
            m = _ID_PATTERN.search(line)
            if not m:
                continue
            line_lower = line.lower()
            if any(sig in line_lower for sig in _ISSUE_SIGNALS) or line.rstrip().endswith("?"):
                flagged.append((m.group(1), line))

        if not flagged:
            return {"issues": []}

        # Emit up to two issues; group flagged lines into one or two buckets.
        if len(flagged) <= 2:
            buckets = [flagged]
        else:
            mid = (len(flagged) + 1) // 2
            buckets = [flagged[:mid], flagged[mid:]]

        issues: list[dict[str, Any]] = []
        for bucket in buckets[:2]:
            ids = [mid for mid, _ in bucket]
            bucket_text = " ".join(line for _, line in bucket)
            category = self._categorize(bucket_text.lower())
            severity = self._severity(bucket_text.lower())
            first_line = bucket[0][1]
            title = self._title_from_line(first_line)
            missing_info = self._missing_for(bucket_text.lower())
            issues.append(
                {
                    "title": title,
                    "summary": (
                        f"Detected a possible {category} from the chat: {title}"
                    ),
                    "category": category,
                    "severity": severity,
                    "source_message_ids": ids or all_ids[:1],
                    "missing_info": missing_info,
                    # Lever 1: open the clarification inline so detect+ask is one
                    # round-trip — the runner posts these as the first question batch.
                    "clarifying_questions": self._questions_from_missing(missing_info),
                }
            )
        return {"issues": issues}

    def _categorize(self, text: str) -> str:
        for label, words in _CATEGORY_KEYWORDS:
            if any(w in text for w in words):
                return label
        return "request"

    def _severity(self, text: str) -> str:
        if any(sig in text for sig in _HIGH_SIGNALS):
            return "high"
        if any(sig in text for sig in _LOW_SIGNALS):
            return "low"
        return "med"

    def _missing_for(self, text: str) -> list[str]:
        """The facts still needed to act, by what's absent from the text."""
        missing: list[str] = []
        if not any(h in text for h in _OWNER_HINTS):
            missing.append("owner")
        if not _DATE_PATTERN.search(text):
            missing.append("deadline")
        if not _NUMBER_PATTERN.search(text):
            missing.append("specific scope or numbers")
        return missing or ["confirmation of next step"]

    def _title_from_line(self, line: str) -> str:
        """A short title from a transcript line, stripping the `#id [time] who:`."""
        # Drop the leading "#id " token.
        body = _ID_PATTERN.sub("", line, count=1).strip()
        # Drop a leading "[time] " stamp.
        body = re.sub(r"^\[[^\]]*\]\s*", "", body).strip()
        # Drop a leading "sender: " prefix.
        if ":" in body:
            head, _, rest = body.partition(":")
            if rest.strip() and "/" in head or head.lower().startswith("user"):
                body = rest.strip()
            elif rest.strip():
                body = rest.strip()
        title = " ".join(body.split())
        if len(title) > 60:
            title = title[:57].rstrip() + "..."
        return title or "Possible issue"

    # --- assess_clarity ------------------------------------------------------
    def _assess_clarity(self, user: str) -> dict[str, Any]:
        """Clear once the transcript supplies owner + deadline + specifics."""
        text = (user or "").lower()
        has_owner = any(h in text for h in _OWNER_HINTS)
        has_date = bool(_DATE_PATTERN.search(text))
        has_numbers = bool(_NUMBER_PATTERN.search(text))

        missing: list[str] = []
        if not has_owner:
            missing.append("owner")
        if not has_date:
            missing.append("deadline")
        if not has_numbers:
            missing.append("specific scope or numbers")

        if not missing:
            return {
                "is_clear": True,
                "confidence": 0.9,
                "missing_info": [],
                "rationale": "Owner, deadline, and concrete details are all present.",
                "questions": [],  # clear ⇒ no further questions (Lever 1 contract)
            }
        return {
            "is_clear": False,
            "confidence": 0.6,
            "missing_info": missing,
            "rationale": "Still missing: " + ", ".join(missing) + ".",
            # Lever 1: draft the next clarifying batch inline so assess+ask is one
            # round-trip — the runner posts these when it re-asks.
            "questions": self._questions_from_missing(missing),
        }

    # --- generate_questions --------------------------------------------------
    def _generate_questions(self, user: str) -> dict[str, Any]:
        """Templated 2-3 questions derived from the listed missing info."""
        return {"questions": self._questions_from_missing(self._parse_missing_block(user))}

    # Templated clarifying question per missing-fact label. Shared so detection's
    # inline `clarifying_questions`, the clarity branch's inline `questions`, and
    # the standalone `generate_questions` task all phrase the same fact identically.
    _QUESTION_TEMPLATES: tuple[tuple[str, str], ...] = (
        ("owner", "Who will own this and drive it to resolution?"),
        ("deadline", "What is the firm deadline or target date for this?"),
        (
            "specific scope or numbers",
            "What are the specific numbers or scope involved (expected vs actual)?",
        ),
        ("scope", "What is the exact scope and acceptance criteria?"),
        ("repro steps", "What are the exact steps to reproduce the problem?"),
    )

    def _questions_from_missing(self, missing: list[str]) -> list[str]:
        """2-3 templated clarifying questions for a list of missing-fact labels.

        Backfills owner/deadline questions when the labels yield fewer than two,
        so every caller (detect / clarity / generate_questions) always opens with
        at least a couple of concrete questions — the contract the runner relies on."""
        templates = dict(self._QUESTION_TEMPLATES)
        questions: list[str] = []
        for item in missing or []:
            key = item.strip().lower()
            q = templates.get(key) or f"Can you clarify the {key}?"
            if q not in questions:
                questions.append(q)
            if len(questions) >= 3:
                break
        if len(questions) < 2:
            for fallback in (templates["owner"], templates["deadline"]):
                if fallback not in questions:
                    questions.append(fallback)
                if len(questions) >= 2:
                    break
        return questions[:3]

    def _parse_missing_block(self, user: str) -> list[str]:
        """Pull the bulleted "Missing information to resolve:" items if present."""
        items: list[str] = []
        capture = False
        for raw in (user or "").splitlines():
            line = raw.strip()
            if line.lower().startswith("missing information"):
                capture = True
                continue
            if capture:
                if line.startswith("- "):
                    val = line[2:].strip()
                    if val and val != "(none listed)":
                        items.append(val)
                elif line == "":
                    continue
                else:
                    break
        return items

    # --- summarize_resolution ------------------------------------------------
    def _summarize_resolution(self, user: str) -> dict[str, Any]:
        """A short summary + resolution drawn from the transcript text."""
        title = self._issue_title_from_brief(user)
        # Look for an owner / date only in the transcript lines (those carry a
        # "#<id>" prefix), not in the issue-brief block, so we don't lift the
        # "- already-noted missing info: owner" line as a real owner.
        owner = "the assigned owner"
        when = "the agreed date"
        for raw in (user or "").splitlines():
            if not _ID_PATTERN.search(raw):
                continue
            low = raw.lower()
            if owner == "the assigned owner" and any(h in low for h in _OWNER_HINTS):
                owner = " ".join(raw.split())[:60]
            dm = _ISO_DATE_PATTERN.search(raw)
            if when == "the agreed date" and dm:
                when = dm.group(0)
        summary = f"{title}." if title else "The reported issue was clarified."
        resolution = (
            f"Clarified with an owner and a target of {when}; "
            f"agreed next step recorded ({owner})."
        )
        return {"summary": summary, "resolution": resolution}

    def _issue_title_from_brief(self, user: str) -> str:
        """Read the '- title: ...' line from the issue brief in the prompt."""
        for raw in (user or "").splitlines():
            line = raw.strip()
            if line.lower().startswith("- title:"):
                return line.split(":", 1)[1].strip()
        return ""

    # --- narrate_resolution --------------------------------------------------
    def _narrate_resolution(self, user: str) -> dict[str, Any]:
        """A short, plain spoken script from the report brief (title/summary/
        resolution) — no Markdown, the contract the voice path feeds to TTS."""
        fields = self._brief_fields(user)
        title = fields.get("title", "") or "the reported issue"
        summary = fields.get("summary", "")
        resolution = fields.get("resolution", "") or "It has been clarified and closed."
        parts = [f"Issue resolved: {title}."]
        if summary:
            parts.append(summary if summary.endswith(".") else summary + ".")
        parts.append(resolution if resolution.endswith(".") else resolution + ".")
        narration = " ".join(" ".join(p.split()) for p in parts if p.strip())
        return {"narration": narration}

    @staticmethod
    def _brief_fields(user: str) -> dict[str, str]:
        """Parse the '- key: value' lines of a report/issue brief into a dict."""
        fields: dict[str, str] = {}
        for raw in (user or "").splitlines():
            line = raw.strip()
            if line.startswith("- ") and ":" in line:
                key, _, val = line[2:].partition(":")
                key = key.strip().lower()
                if key and key not in fields:
                    fields[key] = val.strip()
        return fields
