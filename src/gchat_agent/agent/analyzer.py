"""The single retrieval-augmented `Analyzer` (§5.6 + §6 + §3).

One analyzer drives the bot's three LLM tasks — detect candidate issues, assess
whether an open issue is clear enough to act on, and generate sharper clarifying
questions. Each task is a single `LLMClient.complete_json` call built from the
`agent.prompts` builders and parsed against the LLM JSON CONTRACTS (object-wrapped
shapes: `{"issues": [...]}`, `{"is_clear": ..., "missing_info": [...], ...}`,
`{"questions": [...]}`).

Retrieval *supplements* — never replaces — the transcript:

- `retriever is None` → ``retrieved_context=""`` and the full rendered transcript
  goes straight to the model (the graceful direct-context bypass, §3 — avoids a
  ranker dropping a key message when there is no KB to ground against);
- otherwise → the top-`k` passages for an issue/transcript query are rendered into
  a compact "Retrieved context" block appended after the transcript.

Detected candidates are turned into full `Issue`s: status OPEN, fingerprint =
`models.issue_fingerprint(thread_id, root_message_id, category)` (also the id),
`source_message_ids` mapped to ids actually present in the transcript, and
`root_message_id` = the earliest such source id. The `IssueStore` (§5.6) owns
dedup/merge against open issues + the tombstone set; the analyzer only mints
self-consistent candidates.

Stdlib only.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from gchat_agent import models
from gchat_agent.agent.prompts import (
    clarity_prompt,
    detect_prompt,
    questions_prompt,
)
from gchat_agent.models import (
    ClarityAssessment,
    Issue,
    Severity,
    Status,
)

if TYPE_CHECKING:
    from gchat_agent.llm.base import LLMClient
    from gchat_agent.models import Conversation, Message
    from gchat_agent.rag.base import Passage, Retriever


# Severity strings the contract permits → the Severity enum; anything else falls
# back to MEDIUM (the model is instructed to emit exactly low/med/high).
_SEVERITY_BY_VALUE: dict[str, Severity] = {s.value: s for s in Severity}

# Cap on how much passage text we inject per retrieved chunk so the context block
# stays compact (retrieval only supplements the transcript).
_PASSAGE_CHARS = 600


class Analyzer:
    """Retrieval-augmented analyzer over a `Conversation` (§5.6).

    `llm` is any `LLMClient` (MockLLM offline, OpenRouter live). `retriever` is
    optional: `None` triggers the direct-context bypass (§3). `top_k` bounds how
    many passages are pulled per call when a retriever is present.
    """

    def __init__(
        self,
        llm: "LLMClient",
        retriever: "Retriever | None" = None,
        top_k: int = 5,
    ) -> None:
        self.llm = llm
        self.retriever = retriever
        self.top_k = top_k

    # --- detection -----------------------------------------------------------
    def detect_issues(self, conversation: "Conversation") -> list[Issue]:
        """Run one detection call and mint a full `Issue` per candidate.

        The transcript is rendered with ids so the model can cite
        `source_message_ids`; each candidate's cited ids are filtered to those
        actually present in the transcript, `root_message_id` is the earliest such
        id, the fingerprint (= id) is derived from thread+root+category, and
        timestamps are set from the conversation's latest message create_time.
        Candidates with no valid source id are dropped (we cannot anchor them).
        """
        transcript = conversation.render(with_ids=True)
        retrieved_context = self._retrieve_context(transcript)
        system, user = detect_prompt(transcript, retrieved_context)
        data = self.llm.complete_json(system, user)
        if not isinstance(data, dict):  # degrade gracefully on an unparseable reply
            data = {}

        # Map id -> Message for source resolution + thread/timestamp lookup.
        by_id = {m.id: m for m in conversation.messages}
        latest_create_time = self._latest_create_time(conversation.messages)

        issues: list[Issue] = []
        for raw in self._as_list(data.get("issues")):
            if not isinstance(raw, dict):
                continue
            issue = self._build_issue(raw, by_id, latest_create_time)
            if issue is not None:
                issues.append(issue)
        return issues

    def _build_issue(
        self,
        raw: dict,
        by_id: dict[str, "Message"],
        latest_create_time: str,
    ) -> Issue | None:
        """Turn one raw candidate dict into a fully-formed OPEN `Issue`, or
        `None` when it cites no message id present in the transcript."""
        # Resolve cited ids to real transcript ids, preserving transcript order.
        # Models cite ids inconsistently — full id, `#<id>` marker copied verbatim,
        # or just the trailing segment ("m1" for "spaces/X/messages/m1"). Resolve
        # all of these so a citation-format quirk never silently drops the issue.
        present = [m.id for m in by_id.values()]
        order = {mid: idx for idx, mid in enumerate(present)}
        resolved = [
            real
            for real in (
                self._resolve_cited_id(c, present)
                for c in self._as_list(raw.get("source_message_ids"))
            )
            if real is not None
        ]
        source_ids = sorted(set(resolved), key=lambda i: order[i])
        if not source_ids:
            return None

        root_message_id = source_ids[0]  # earliest in transcript order
        thread_id = by_id[root_message_id].thread_id
        category = str(raw.get("category", "") or "").strip() or "issue"
        severity = _SEVERITY_BY_VALUE.get(
            str(raw.get("severity", "") or "").strip().lower(), Severity.MEDIUM
        )
        fingerprint = models.issue_fingerprint(thread_id, root_message_id, category)

        title = str(raw.get("title", "") or "").strip() or "Possible issue"
        summary = str(raw.get("summary", "") or "").strip()
        missing_info = [
            str(m).strip()
            for m in self._as_list(raw.get("missing_info"))
            if str(m).strip()
        ]

        return Issue(
            id=fingerprint,
            fingerprint=fingerprint,
            title=title,
            summary=summary,
            category=category,
            severity=severity,
            status=Status.OPEN,
            thread_id=thread_id,
            root_message_id=root_message_id,
            source_message_ids=source_ids,
            missing_info=missing_info,
            created_at=latest_create_time or "",
            updated_at=latest_create_time or "",
        )

    # --- clarity assessment --------------------------------------------------
    def assess_clarity(
        self, issue: Issue, conversation: "Conversation"
    ) -> ClarityAssessment:
        """Assess whether `issue` is now clear enough to act on (§6).

        Scopes the transcript to the issue's own thread (the clarity decision is
        per-thread), supplements with retrieval when available, and parses the
        contract shape into a `ClarityAssessment`.
        """
        transcript = self._issue_transcript(issue, conversation)
        retrieved_context = self._retrieve_context(self._issue_query(issue, transcript))
        system, user = clarity_prompt(issue, transcript, retrieved_context)
        data = self.llm.complete_json(system, user)
        return ClarityAssessment.from_dict(data if isinstance(data, dict) else {})

    # --- clarifying question generation --------------------------------------
    def generate_questions(
        self,
        issue: Issue,
        conversation: "Conversation",
        missing_info: list[str],
    ) -> list[str]:
        """Generate 2-3 clarifying questions targeting `missing_info` (§6).

        Returns the deduped, non-empty question strings the model produced (the
        runner caps how many rounds are asked, §5.7).
        """
        transcript = self._issue_transcript(issue, conversation)
        retrieved_context = self._retrieve_context(self._issue_query(issue, transcript))
        system, user = questions_prompt(
            issue, transcript, missing_info, retrieved_context
        )
        data = self.llm.complete_json(system, user)
        questions: list[str] = []
        for q in self._as_list(data.get("questions") if isinstance(data, dict) else None):
            text = str(q).strip()
            if text and text not in questions:
                questions.append(text)
        return questions

    # --- retrieval -----------------------------------------------------------
    def _retrieve_context(self, query: str) -> str:
        """Render the top-`k` passages for `query` into a context block.

        Returns ``""`` when there is no retriever (direct-context bypass, §3) or
        when nothing is retrieved, so prompts.py omits the block entirely.
        """
        if self.retriever is None or self.top_k <= 0:
            return ""
        query = (query or "").strip()
        if not query:
            return ""
        try:
            passages = self.retriever.retrieve(query, self.top_k)
        except Exception:
            # Retrieval is best-effort supplementary context — never let a ranker
            # failure block detection/clarity. Degrade to direct-context.
            return ""
        return self._render_passages(passages)

    @staticmethod
    def _render_passages(passages: "list[Passage]") -> str:
        """One labeled block per passage: `[kind:source › section] text`."""
        lines: list[str] = []
        for p in passages or []:
            text = " ".join((p.text or "").split())
            if not text:
                continue
            if len(text) > _PASSAGE_CHARS:
                text = text[: _PASSAGE_CHARS - 1].rstrip() + "…"
            label = f"[{p.kind}:{p.source}"
            if p.section:
                label += f" › {p.section}"
            label += "]"
            lines.append(f"{label} {text}")
        return "\n".join(lines)

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def _issue_transcript(issue: Issue, conversation: "Conversation") -> str:
        """Render the transcript scoped to the issue's thread, falling back to the
        whole conversation if the thread has no messages (e.g. unset thread_id)."""
        scoped = conversation.for_thread(issue.thread_id) if issue.thread_id else conversation
        if not scoped.messages:
            scoped = conversation
        return scoped.render(with_ids=True)

    @staticmethod
    def _issue_query(issue: Issue, transcript: str) -> str:
        """A retrieval query for an issue: its title/summary/category plus the
        recorded missing info, so the ranker pulls passages about this problem."""
        parts = [issue.title, issue.summary, issue.category]
        parts.extend(issue.missing_info)
        query = " ".join(p for p in parts if p)
        return query.strip() or transcript

    @staticmethod
    def _latest_create_time(messages: "list[Message]") -> str:
        """The latest non-empty `create_time` among messages (RFC-3339 sorts
        lexicographically), or `""` if none carry a timestamp."""
        times = [m.create_time for m in messages if m.create_time]
        return max(times) if times else ""

    @staticmethod
    def _strip_id_marker(value: object) -> str:
        """Normalize a cited message id: drop surrounding whitespace and a single
        leading '#'. The transcript renders each line as `#<id> ...`; some models
        (e.g. glm) copy that '#' marker into `source_message_ids`, which would
        otherwise fail the exact-id match and drop the issue."""
        text = str(value).strip()
        return text[1:].strip() if text.startswith("#") else text

    @staticmethod
    def _resolve_cited_id(value: object, present_ids: list[str]) -> str | None:
        """Resolve a model-cited id to a real transcript message id, or None.

        Tolerates the citation-format quirks seen across models: the `#` marker
        (glm), the full resource name, and trailing-segment truncation (minimax
        cites "m1" for "spaces/X/messages/m1"). Tries, in order: exact match, a
        real id ending in "/<cited>", then last-path-segment equality (message
        id segments are unique within a space, so this won't cross-match)."""
        cited = Analyzer._strip_id_marker(value)
        if not cited:
            return None
        if cited in present_ids:
            return cited
        tail = cited.rsplit("/", 1)[-1]
        for real in present_ids:
            if real.endswith("/" + cited) or real.rsplit("/", 1)[-1] == tail:
                return real
        return None

    @staticmethod
    def _as_list(value: object) -> list:
        """Coerce a possibly-missing/None/non-list value to a list."""
        return value if isinstance(value, list) else []
