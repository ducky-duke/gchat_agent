"""Domain models (§5.2) — dataclasses with lossless JSON round-trip.

Every model exposes `to_dict()` / `from_dict()` so the whole `IssueStore` state
file serializes and reloads with no information loss (nested `qa` dicts, id
lists, optional/None fields). Enums subclass `str` so they JSON round-trip to
plain strings. Stdlib only.
"""
from __future__ import annotations

import enum
import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


# --- string-literal enums (JSON round-trip to plain strings) ----------------
class SenderType(str, enum.Enum):
    """Who authored a message. Staff personas post as HUMAN (user OAuth)."""

    HUMAN = "human"
    APP = "app"


class Severity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "med"
    HIGH = "high"


class Status(str, enum.Enum):
    OPEN = "open"
    CLARIFYING = "clarifying"
    RESOLVED = "resolved"
    STALE = "stale"


def issue_fingerprint(thread_id: str, root_message_id: str, category: str) -> str:
    """Stable dedup anchor (§5.2/§6).

    Hashes the deterministic anchors `thread_id` + earliest `root_message_id`,
    plus a *normalized* `category` (lower-cased, whitespace-collapsed) to
    disambiguate distinct issues raised from the same root message while
    absorbing trivial wording drift. The fingerprint is resilient to LLM drift
    in `source_message_ids`; a genuine category change yields a new fingerprint,
    which the IssueStore's secondary title/summary similarity check and the
    resolved/stale tombstone set guard against re-raising. Callers must pass a
    non-empty `thread_id` and `root_message_id` (the IssueStore guarantees this
    before dedup). Returns a 16-char hex digest."""
    cat = " ".join((category or "").lower().split())
    raw = "\x1f".join((thread_id or "", root_message_id or "", cat))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --- helpers for enum-aware (de)serialization -------------------------------
def _enum_value(value: Any) -> Any:
    """Unwrap an enum to its underlying string for JSON; pass through otherwise."""
    return value.value if isinstance(value, enum.Enum) else value


def _coerce_bool(value: Any) -> bool:
    """Coerce a possibly-stringy value to bool — raw LLM JSON may yield the
    string `"false"`, which the builtin `bool()` would wrongly read as True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on", "y")
    return bool(value)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    """Coerce a possibly-stringy/None value to float, falling back to `default`
    — raw LLM JSON may carry a non-numeric confidence like `"high"`."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class QAPair:
    """One clarifying exchange (§6): the bot's question plus the reply(ies) that
    answered it, captured as replies arrive for the resolution report."""

    question: str
    answer_message_ids: list[str] = field(default_factory=list)
    text: str = ""  # joined reply text

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer_message_ids": list(self.answer_message_ids),
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QAPair":
        return cls(
            question=data.get("question", ""),
            answer_message_ids=list(data.get("answer_message_ids", [])),
            text=data.get("text", ""),
        )


@dataclass
class Message:
    """A single Chat message (§5.2)."""

    id: str
    space: str
    thread_id: str
    sender: str  # `users/<id>` resource name
    sender_type: SenderType
    text: str
    create_time: str  # RFC-3339

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space": self.space,
            "thread_id": self.thread_id,
            "sender": self.sender,
            "sender_type": _enum_value(self.sender_type),
            "text": self.text,
            "create_time": self.create_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            id=data["id"],
            space=data.get("space", ""),
            thread_id=data.get("thread_id", ""),
            sender=data.get("sender", ""),
            sender_type=SenderType(data.get("sender_type", SenderType.HUMAN.value)),
            text=data.get("text", ""),
            create_time=data.get("create_time", ""),
        )


@dataclass
class Issue:
    """A detected issue and its clarification state (§5.2).

    `qa` is a list of plain dicts (question -> answer pairs captured as replies
    arrive) so it round-trips through JSON unchanged.
    """

    id: str
    fingerprint: str
    title: str
    summary: str
    category: str
    severity: Severity
    status: Status
    thread_id: str
    root_message_id: str
    source_message_ids: list[str] = field(default_factory=list)
    missing_info: list[str] = field(default_factory=list)
    questions_asked: list[str] = field(default_factory=list)
    qa: list[QAPair] = field(default_factory=list)
    last_bot_message_id: str | None = None
    # Server `create_time` of the last bot question (RFC-3339). Lets `_new_replies`
    # recover replies after a restart, when the working conversation — rebuilt from
    # only *unseen* messages — no longer contains the (already-seen) anchor message.
    last_bot_create_time: str | None = None
    last_question_at: str | None = None
    rounds: int = 0
    idle_cycles: int = 0
    report_written_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = _enum_value(self.severity)
        data["status"] = _enum_value(self.status)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Issue":
        return cls(
            id=data["id"],
            fingerprint=data["fingerprint"],
            title=data.get("title", ""),
            summary=data.get("summary", ""),
            category=data.get("category", ""),
            severity=Severity(data.get("severity", Severity.MEDIUM.value)),
            status=Status(data.get("status", Status.OPEN.value)),
            thread_id=data.get("thread_id", ""),
            root_message_id=data.get("root_message_id", ""),
            source_message_ids=list(data.get("source_message_ids", [])),
            missing_info=list(data.get("missing_info", [])),
            questions_asked=list(data.get("questions_asked", [])),
            qa=[QAPair.from_dict(item) for item in data.get("qa", [])],
            last_bot_message_id=data.get("last_bot_message_id"),
            last_bot_create_time=data.get("last_bot_create_time"),
            last_question_at=data.get("last_question_at"),
            rounds=int(data.get("rounds", 0)),
            idle_cycles=int(data.get("idle_cycles", 0)),
            report_written_at=data.get("report_written_at"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


@dataclass
class ClarityAssessment:
    """Result of `Analyzer.assess_clarity` (§5.2)."""

    is_clear: bool
    confidence: float
    missing_info: list[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_clear": self.is_clear,
            "confidence": self.confidence,
            "missing_info": list(self.missing_info),
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClarityAssessment":
        return cls(
            is_clear=_coerce_bool(data.get("is_clear", False)),
            confidence=_coerce_float(data.get("confidence", 0.0)),
            missing_info=list(data.get("missing_info", [])),
            rationale=data.get("rationale", ""),
        )


@dataclass
class ResolutionReport:
    """The resolved-issue report (§5.2), rendered to Markdown on disk and
    condensed into the Chat-thread confirmation."""

    issue_id: str
    title: str
    category: str
    severity: Severity
    summary: str
    resolution: str
    qa: list[QAPair] = field(default_factory=list)
    source_message_ids: list[str] = field(default_factory=list)
    resolved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = _enum_value(self.severity)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResolutionReport":
        return cls(
            issue_id=data["issue_id"],
            title=data.get("title", ""),
            category=data.get("category", ""),
            severity=Severity(data.get("severity", Severity.MEDIUM.value)),
            summary=data.get("summary", ""),
            resolution=data.get("resolution", ""),
            qa=[QAPair.from_dict(item) for item in data.get("qa", [])],
            source_message_ids=list(data.get("source_message_ids", [])),
            resolved_at=data.get("resolved_at"),
        )


@dataclass
class Conversation:
    """Ordered messages + a compact transcript renderer for prompts (§5.2)."""

    messages: list[Message] = field(default_factory=list)

    def add(self, message: Message) -> None:
        self.messages.append(message)

    def render(self, with_ids: bool = True) -> str:
        """Render a compact transcript: one line per message, in order, suitable
        for direct injection into an LLM prompt. With `with_ids` (default) each
        line is prefixed `#<id>` so the model can cite `source_message_ids` in
        detection (§6)."""
        lines: list[str] = []
        for m in self.messages:
            tag = f"#{m.id} " if with_ids else ""
            stamp = f"[{m.create_time}] " if m.create_time else ""
            who = m.sender or "(unknown)"
            lines.append(f"{tag}{stamp}{who}: {m.text}")
        return "\n".join(lines)

    def tail(self, n: int) -> "Conversation":
        """A Conversation of the last `n` messages (bounds detection by
        `DETECT_WINDOW_MESSAGES`)."""
        if n <= 0:
            return Conversation(messages=[])
        return Conversation(messages=self.messages[-n:])

    def for_thread(self, thread_id: str) -> "Conversation":
        """Messages belonging to one Chat thread (clarity-assessment scope)."""
        return Conversation(
            messages=[m for m in self.messages if m.thread_id == thread_id]
        )

    def without_sender(self, sender: str) -> "Conversation":
        """Drop messages authored by `sender` — the bot's own `users/<id>`, so
        it never re-detects its own questions as issues (§5.7/§6)."""
        return Conversation(
            messages=[m for m in self.messages if m.sender != sender]
        )

    def after(self, message_id: str) -> "Conversation":
        """Messages following `message_id` in order (e.g. replies since the last
        bot message). Returns all messages if `message_id` is not present."""
        ids = [m.id for m in self.messages]
        if message_id in ids:
            return Conversation(messages=self.messages[ids.index(message_id) + 1:])
        return Conversation(messages=list(self.messages))

    def to_dict(self) -> dict[str, Any]:
        return {"messages": [m.to_dict() for m in self.messages]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Conversation":
        return cls(messages=[Message.from_dict(m) for m in data.get("messages", [])])


@dataclass
class AgentState:
    """Top-level persisted state for the `IssueStore` (§5.6/§5.7) — the whole
    `STATE_FILE` blob. Holds the poll cursor (last processed message resource
    `name` plus a bounded recent-id set to survive equal-timestamp clock skew,
    §5.4/§7), the live issues, and a tombstone set of resolved/stale
    fingerprints so a closed issue is not re-raised from the same root (§6)."""

    cursor_message_name: str | None = None
    bot_user_id: str | None = None
    seen_message_ids: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    tombstones: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor_message_name": self.cursor_message_name,
            "bot_user_id": self.bot_user_id,
            "seen_message_ids": list(self.seen_message_ids),
            "issues": [i.to_dict() for i in self.issues],
            "tombstones": list(self.tombstones),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentState":
        return cls(
            cursor_message_name=data.get("cursor_message_name"),
            bot_user_id=data.get("bot_user_id"),
            seen_message_ids=list(data.get("seen_message_ids", [])),
            issues=[Issue.from_dict(i) for i in data.get("issues", [])],
            tombstones=list(data.get("tombstones", [])),
        )
