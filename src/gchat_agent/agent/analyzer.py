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
`source_message_ids` mapped to ids actually present in the transcript, anchored
to a single thread (the one owning the most cited messages — never a stray
greeting in another thread, see `_anchor_thread`), and `root_message_id` = the
earliest source id within that anchor thread. The `IssueStore` (§5.6) owns
dedup/merge against open issues + the tombstone set; the analyzer only mints
self-consistent candidates.

Stdlib only.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from gchat_agent import models
from gchat_agent.agent.prompts import (
    clarity_prompt,
    detect_prompt,
    questions_prompt,
)
from gchat_agent.observability import observe
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

# Lower-cased alphanumeric word tokens, used to anchor a cross-thread issue to the
# thread whose message text best matches its title/summary (`_anchor_thread`).
_WORD_RE = re.compile(r"[a-z0-9]+")


def _word_tokens(text: str) -> set[str]:
    """The set of lower-cased word tokens in `text` (empty for falsy input)."""
    return set(_WORD_RE.findall((text or "").lower()))


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
    @observe(name="analyzer.detect_issues")
    def detect_issues(
        self,
        conversation: "Conversation",
        prior_issues: "list[Issue] | None" = None,
    ) -> list[Issue]:
        """Run one detection call and mint a full `Issue` per candidate.

        The transcript is rendered with ids so the model can cite
        `source_message_ids`; each candidate's cited ids are filtered to those
        actually present in the transcript, `root_message_id` is the earliest such
        id, the fingerprint (= id) is derived from thread+root+category, and
        timestamps are set from the conversation's latest message create_time.
        Candidates with no valid source id are dropped (we cannot anchor them).

        `prior_issues` are recently-closed issues surfaced to the model as episodic
        recall (so it has memory of what was already handled); `None`/empty adds
        nothing to the prompt — the runner passes them only when `EPISODIC_RECALL`
        is on and the store actually holds closed issues.
        """
        transcript = conversation.render(with_ids=True)
        retrieved_context = self._retrieve_context(transcript)
        system, user = detect_prompt(transcript, retrieved_context, prior_issues)
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

        title = str(raw.get("title", "") or "").strip() or "Possible issue"
        summary = str(raw.get("summary", "") or "").strip()

        # Anchor the issue to a SINGLE thread, even when the model cited source
        # messages spanning several top-level threads (in Google Chat each
        # top-level message is its own thread). Taking the globally-earliest cited
        # message as the root would drag the whole issue — and every clarifying
        # reply the bot later posts — into whatever came first, which is often an
        # unrelated greeting in a *different* thread (the "bot replies in the wrong
        # thread" bug). Instead anchor to the thread the issue is genuinely ABOUT —
        # the one whose cited messages best match the model's own title/summary —
        # then drop the cross-thread stragglers so the root, thread, evidence, and
        # every post stay coherent. A single-thread issue is unaffected.
        anchor_thread = self._anchor_thread(source_ids, by_id, f"{title} {summary}")
        in_thread = [mid for mid in source_ids if by_id[mid].thread_id == anchor_thread]
        source_ids = in_thread or source_ids
        root_message_id = source_ids[0]  # earliest in the anchor thread
        thread_id = by_id[root_message_id].thread_id
        reporter_id = by_id[root_message_id].sender or None
        category = str(raw.get("category", "") or "").strip() or "issue"
        severity = _SEVERITY_BY_VALUE.get(
            str(raw.get("severity", "") or "").strip().lower(), Severity.MEDIUM
        )
        fingerprint = models.issue_fingerprint(thread_id, root_message_id, category)

        missing_info = [
            str(m).strip()
            for m in self._as_list(raw.get("missing_info"))
            if str(m).strip()
        ]
        # The opening clarifying questions detection produced inline (Lever 1 —
        # the runner posts these on first contact, skipping a second LLM call).
        pending_questions = self._clean_questions(raw.get("clarifying_questions"))

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
            reporter_id=reporter_id,
            source_message_ids=source_ids,
            missing_info=missing_info,
            pending_questions=pending_questions,
            created_at=latest_create_time or "",
            updated_at=latest_create_time or "",
        )

    # --- clarity assessment --------------------------------------------------
    @observe(name="analyzer.assess_clarity")
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
        assessment = ClarityAssessment.from_dict(data if isinstance(data, dict) else {})
        # Normalize the inline next-question batch (Lever 1) the same way a
        # dedicated generation call would, so the runner can post it verbatim.
        assessment.questions = self._clean_questions(assessment.questions)
        return assessment

    # --- clarifying question generation --------------------------------------
    @observe(name="analyzer.generate_questions")
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
        return self._clean_questions(
            data.get("questions") if isinstance(data, dict) else None
        )

    @staticmethod
    def _clean_questions(raw: object) -> list[str]:
        """Coerce a model's `questions`/`clarifying_questions` value to a clean
        list: stringified, stripped, empties dropped, order-preserving dedupe.
        Shared by `generate_questions`, detection's inline questions, and the
        clarity assessment's inline questions so all three normalize identically."""
        cleaned: list[str] = []
        for q in Analyzer._as_list(raw):
            text = str(q).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

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
        """A retrieval query for an issue: its title/summary/category, the recorded
        missing info, AND the reporter's latest reply — so the ranker pulls passages
        about what the reporter just said, not only the original report (the reply
        is often where the specifics that need grounding first appear)."""
        parts = [issue.title, issue.summary, issue.category]
        parts.extend(issue.missing_info)
        if issue.qa and issue.qa[-1].text:
            parts.append(issue.qa[-1].text)
        query = " ".join(p for p in parts if p)
        return query.strip() or transcript

    @staticmethod
    def _anchor_thread(
        source_ids: list[str], by_id: dict[str, "Message"], issue_text: str
    ) -> str:
        """Choose the one thread to anchor an issue to when its cited source
        messages span several top-level threads. Anchor to the thread the issue is
        genuinely ABOUT: the one whose cited message text shares the most word
        tokens with the issue's own `title`/`summary` (`issue_text`). Ties — and
        the common no-overlap case — fall back to the EARLIEST cited thread, so a
        single-thread issue and a coherent one keep their existing root.

        Why content, not recency or count: a greeting cited *before* the real
        report and a follow-up reply cited *after* it look identical by position
        and count, but the issue's title matches the report's words, never the
        greeting's. `source_ids` is in transcript (chronological) order; returns
        the chosen `thread_id`.
        """
        threads: list[tuple[str, int]] = []  # (thread_id, earliest cited index)
        seen: set[str] = set()
        for idx, mid in enumerate(source_ids):
            tid = by_id[mid].thread_id
            if tid not in seen:
                seen.add(tid)
                threads.append((tid, idx))
        if len(threads) == 1:
            return threads[0][0]
        issue_tokens = _word_tokens(issue_text)

        def score(item: tuple[str, int]) -> tuple[int, int]:
            tid, earliest_idx = item
            text = " ".join(by_id[m].text for m in source_ids if by_id[m].thread_id == tid)
            overlap = len(issue_tokens & _word_tokens(text))
            # Most title/summary overlap first; tie-break toward the earliest
            # cited thread (smallest index ⇒ largest -index).
            return (overlap, -earliest_idx)

        return max(threads, key=score)[0]

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
