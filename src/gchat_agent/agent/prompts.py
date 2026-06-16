"""LLM task prompts — the single source of truth for the agent's LLM contracts (§5.6).

Four tasks drive the issue-spotter bot: detect candidate issues, assess whether an
open issue is clear enough to act on, generate sharper clarifying questions, and
summarize a resolution. Each builder returns a ``(system, user)`` tuple.

Every system prompt embeds:
  * a crisp role ("issue-spotter for an iGaming work chat"),
  * the task instruction,
  * a stable ``MARK_*`` marker token so ``llm/mock.py`` can branch deterministically,
  * the strict-JSON output contract (the exact shape + "respond with ONLY that JSON object").

The user prompt carries the rendered transcript (``Conversation.render(with_ids=True)``
prefixes each line with ``#<id>`` so the model can cite ``source_message_ids``) and,
when supplied, a "Retrieved context:" block that SUPPLEMENTS — never replaces — the
transcript (§3 graceful direct-context bypass when retrieval is off).

The object-wrapper convention (e.g. ``{"issues": [...]}``) is used everywhere so the
foundation's ``extract_json`` — which rejects bare arrays — parses every response.

Stdlib only; no imports needed beyond ``__future__``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a runtime dependency cycle; only needed for type hints
    from gchat_agent.models import Issue, ResolutionReport


# --- task markers (MockLLM branches on these appearing in system+user) ------
MARK_DETECT = "TASK:detect_issues"
MARK_CLARITY = "TASK:assess_clarity"
MARK_QUESTIONS = "TASK:generate_questions"
MARK_RESOLUTION = "TASK:summarize_resolution"
MARK_NARRATION = "TASK:narrate_resolution"


# --- shared prompt fragments -------------------------------------------------
_ROLE = (
    "You are an issue-spotter for an iGaming work chat. Staff (operations, "
    "promotions, payments, compliance/KYC) discuss incidents, requests, and bugs. "
    "An issue is anything that needs clarification or action before it can move "
    "forward: a reported failure, a blocker, a vague or risky ask, or a request "
    "missing information (owner, deadline, scope, acceptance criteria, exact "
    "numbers). You reason over the conversation itself."
)

_STRICT = (
    "Output requirements: respond with ONLY that JSON object. No prose, no "
    "Markdown code fences, no comments, no trailing text. Use double-quoted keys "
    "and string values. Do not invent message ids — cite only ids that appear in "
    "the transcript. Each transcript line begins with a `#<id>` marker; cite the "
    "id WITHOUT the leading '#' (for a line `#abc123 [time] ...`, cite `abc123`)."
)


def _render_user(transcript: str, retrieved_context: str = "", task_line: str = "") -> str:
    """Assemble the user prompt: an optional task line, the transcript, and — only
    when non-empty — a "Retrieved context:" block that SUPPLEMENTS the transcript."""
    parts: list[str] = []
    if task_line:
        parts.append(task_line)
    parts.append("Transcript (each line is `#<message_id> [time] sender: text`):")
    parts.append(transcript if transcript.strip() else "(empty transcript)")
    context = (retrieved_context or "").strip()
    if context:
        parts.append(
            "Retrieved context (supplementary background — KB excerpts and earlier "
            "chat; use it to inform your answer but treat the transcript above as "
            "the source of truth, and never cite these as message ids):"
        )
        parts.append(context)
    return "\n\n".join(parts)


def _issue_brief(issue: "Issue") -> str:
    """A compact one-block summary of the issue under consideration."""
    src = ", ".join(issue.source_message_ids) if issue.source_message_ids else "(none)"
    missing = "; ".join(issue.missing_info) if issue.missing_info else "(none recorded)"
    severity = getattr(issue.severity, "value", issue.severity)
    return (
        "Issue under consideration:\n"
        f'- title: {issue.title}\n'
        f"- summary: {issue.summary}\n"
        f"- category: {issue.category}\n"
        f"- severity: {severity}\n"
        f"- source message ids: {src}\n"
        f"- already-noted missing info: {missing}"
    )


# --- detection ---------------------------------------------------------------
def detect_prompt(transcript: str, retrieved_context: str = "") -> tuple[str, str]:
    """Build the (system, user) prompt for detecting candidate issues.

    Output shape (object-wrapped so ``extract_json`` accepts it)::

        {"issues": [{"title": str, "summary": str, "category": str,
                     "severity": "low"|"med"|"high",
                     "source_message_ids": [str, ...],
                     "missing_info": [str, ...],
                     "clarifying_questions": [str, ...]}, ...]}

    ``clarifying_questions`` lets detection ALSO open the clarification in the same
    round-trip (the runner posts them as the first question batch without a second
    `generate_questions` call); an empty list is fine and the runner falls back.
    """
    system = (
        f"{_ROLE}\n\n"
        f"{MARK_DETECT}\n"
        "Read the transcript and identify the distinct issues that need "
        "clarification or action. Be conservative: only raise something that is a "
        "genuine incident, blocker, bug, or under-specified request — ignore "
        "small talk and resolved chatter. Merge messages about the same problem "
        "into one issue. For each issue, choose a short `category` (e.g. "
        '"incident", "request", "bug", "compliance"), a `severity` of exactly '
        '"low", "med", or "high", cite the `source_message_ids` it is drawn from, '
        "and list `missing_info`: the specific facts still needed to act (owner, "
        "deadline, scope, repro steps, expected vs actual numbers, acceptance "
        "criteria). Also write `clarifying_questions`: 2-3 sharp, specific "
        "questions (one concrete thing each, no compound or yes/no questions, a "
        "single sentence each) that target that `missing_info` so the reporter can "
        "answer them straight away — ask nothing already answered in the "
        "transcript. If there are no real issues, return an empty list.\n\n"
        "Respond with a JSON object of exactly this shape:\n"
        '{"issues": [{"title": str, "summary": str, "category": str, '
        '"severity": "low"|"med"|"high", "source_message_ids": [str, ...], '
        '"missing_info": [str, ...], "clarifying_questions": [str, ...]}, ...]}\n'
        f"{_STRICT}"
    )
    user = _render_user(
        transcript,
        retrieved_context,
        task_line="Detect the issues in the following work-chat transcript.",
    )
    return system, user


# --- clarity assessment ------------------------------------------------------
def clarity_prompt(
    issue: "Issue", transcript: str, retrieved_context: str = ""
) -> tuple[str, str]:
    """Build the (system, user) prompt for assessing whether an issue is clear.

    Output shape::

        {"is_clear": bool, "confidence": number 0..1,
         "missing_info": [str, ...], "rationale": str,
         "questions": [str, ...]}

    ``questions`` merges the next clarifying batch into the SAME round-trip: 2-3
    questions when the issue is not clear (empty when it is), so the runner can
    re-ask without a separate `generate_questions` call. An empty list falls back.
    """
    system = (
        f"{_ROLE}\n\n"
        f"{MARK_CLARITY}\n"
        "Decide whether the issue below is now clear enough to act on, based on "
        "the transcript. It is clear once the CORE facts below are known — and you "
        "should resolve as soon as they are, rather than chasing further detail:\n"
        "- a named owner or responsible person;\n"
        "- for an incident or bug: the scope (what is and isn't affected), the "
        "impact or key numbers, and the likely root cause or a concrete "
        "fix/mitigation plan with a target time;\n"
        "- for a request or change: the concrete dates (both the start/go-live and "
        "the end or deadline), the scope or target audience, and the essential "
        "terms or numbers;\n"
        "- a tracking reference (e.g. a ticket id) if one has been mentioned.\n"
        "Once those are present, set `is_clear` true and leave `missing_info` "
        "empty. Do NOT hold the issue open for peripheral nice-to-haves — extra "
        "diagnostics, dashboard or status-page confirmations, exhaustive metrics, "
        "exact timestamps, or a formal severity label; treat those as optional "
        "follow-ups, not blockers. Only list a fact in `missing_info` if it is one "
        "of the CORE facts above and is still absent. `confidence` is your "
        "certainty in [0, 1]. Give a one-sentence `rationale`. When `is_clear` is "
        "false, also write `questions`: 2-3 sharp, specific clarifying questions "
        "(one concrete thing each, no compound or yes/no questions, a single "
        "sentence each) targeting the `missing_info` above and not repeating "
        "anything already answered. When `is_clear` is true, return `questions` as "
        "an empty list.\n\n"
        "Respond with a JSON object of exactly this shape:\n"
        '{"is_clear": bool, "confidence": number between 0 and 1, '
        '"missing_info": [str, ...], "rationale": str, "questions": [str, ...]}\n'
        f"{_STRICT}"
    )
    user = _render_user(
        transcript,
        retrieved_context,
        task_line=f"{_issue_brief(issue)}\n\nAssess whether this issue is now clear.",
    )
    return system, user


# --- clarifying question generation ------------------------------------------
def questions_prompt(
    issue: "Issue",
    transcript: str,
    missing_info: list[str],
    retrieved_context: str = "",
) -> tuple[str, str]:
    """Build the (system, user) prompt for generating clarifying questions.

    Output shape::

        {"questions": [str, ...]}
    """
    system = (
        f"{_ROLE}\n\n"
        f"{MARK_QUESTIONS}\n"
        "Write 2-3 sharp, specific clarifying questions that, once answered, would "
        "make the issue clear enough to act on. Target the missing facts listed "
        "below; ask one concrete thing per question (no compound or yes/no "
        "questions), do not repeat anything already answered in the transcript, "
        "and keep each question to a single sentence.\n\n"
        "Respond with a JSON object of exactly this shape:\n"
        '{"questions": [str, ...]}\n'
        f"{_STRICT}"
    )
    missing = "\n".join(f"- {m}" for m in missing_info) if missing_info else "- (none listed)"
    task_line = (
        f"{_issue_brief(issue)}\n\n"
        f"Missing information to resolve:\n{missing}\n\n"
        "Generate clarifying questions for this issue."
    )
    user = _render_user(transcript, retrieved_context, task_line=task_line)
    return system, user


# --- resolution summary ------------------------------------------------------
def resolution_prompt(issue: "Issue", transcript: str) -> tuple[str, str]:
    """Build the (system, user) prompt for summarizing a resolved issue.

    No retrieved-context block — the resolution is drawn from the issue's own
    thread and clarifying Q&A. Output shape::

        {"summary": str, "resolution": str}
    """
    system = (
        f"{_ROLE}\n\n"
        f"{MARK_RESOLUTION}\n"
        "The issue below has been clarified and is ready to close. Write a concise "
        "`summary` (1-2 sentences stating what the issue was) and a `resolution` "
        "(1-2 sentences stating how it was resolved or the agreed action, owner, "
        "and deadline), grounded only in the transcript and the clarifying "
        "exchange. Do not speculate beyond what was said.\n\n"
        "Respond with a JSON object of exactly this shape:\n"
        '{"summary": str, "resolution": str}\n'
        f"{_STRICT}"
    )
    user = _render_user(
        transcript,
        "",
        task_line=f"{_issue_brief(issue)}\n\nSummarize this resolved issue.",
    )
    return system, user


# --- spoken-narration script -------------------------------------------------
def _report_brief(report: "ResolutionReport") -> str:
    """A compact block of a resolved report's facts for the narration prompt."""
    severity = getattr(report.severity, "value", report.severity)
    lines = [
        "Resolved issue to narrate:",
        f"- title: {report.title}",
        f"- category: {report.category}",
        f"- severity: {severity}",
        f"- summary: {report.summary}",
        f"- resolution: {report.resolution}",
    ]
    if report.qa:
        lines.append("- key clarifications:")
        for qa in report.qa:
            answer = " ".join((qa.text or "").split())
            if answer:
                lines.append(f"  - {answer}")
    return "\n".join(lines)


def narration_prompt(report: "ResolutionReport") -> tuple[str, str]:
    """Build the (system, user) prompt for a concise SPOKEN narration of a
    resolved report — the script handed to text-to-speech for a voice update.

    Output shape::

        {"narration": str}
    """
    system = (
        f"{_ROLE}\n\n"
        f"{MARK_NARRATION}\n"
        "Write a short spoken update that a colleague will hear as audio — a "
        "voice note announcing that an issue has been resolved. Cover what the "
        "issue was, how it was resolved (owner / action / deadline if known), and "
        "the single most important clarification. Requirements: plain spoken "
        "prose only — NO Markdown, NO headings, NO bullet points, NO emoji, NO "
        "message ids or URLs; 2 to 4 sentences, about 20 to 40 seconds when read "
        "aloud; natural and clear, since it will be spoken, not read. Open with a "
        "brief framing such as \"Issue resolved:\".\n\n"
        "Respond with a JSON object of exactly this shape:\n"
        '{"narration": str}\n'
        "Output requirements: respond with ONLY that JSON object. No prose, no "
        "Markdown code fences, no comments, no trailing text."
    )
    user = (
        f"{_report_brief(report)}\n\n"
        "Write the spoken narration for this resolved issue."
    )
    return system, user
