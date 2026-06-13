"""Resolution-report builder (§5.6 + §6).

When an issue is resolved the runner calls :func:`build_resolution_report` to
assemble a :class:`~gchat_agent.models.ResolutionReport` from the issue's own
fields and its clarifying Q&A. An optional single ``llm.complete_json`` call
(keyed off the resolution prompt) tightens the ``summary`` / ``resolution``
prose; with ``llm=None`` the builder degrades gracefully to the issue's stored
text. :func:`render_markdown` produces the on-disk Markdown, :func:`write_report`
writes it atomically to ``REPORTS_DIR/issue-<id>.md`` (returning that path), and
:func:`confirmation_line` renders the ≤2-line Chat-thread confirmation.

Pure stdlib; no new deps. The LLM is the only optional collaborator and is typed
against the foundation's :class:`~gchat_agent.llm.base.LLMClient` protocol.
"""
from __future__ import annotations

import datetime as _dt
import os
import tempfile
from typing import TYPE_CHECKING

from gchat_agent.agent.prompts import resolution_prompt
from gchat_agent.models import QAPair, ResolutionReport, _enum_value

if TYPE_CHECKING:  # only for the type hint — never import the LLM at runtime
    from gchat_agent.llm.base import LLMClient
    from gchat_agent.models import Issue


def _now_rfc3339() -> str:
    """A UTC RFC-3339 timestamp (``...Z``) for ``resolved_at`` defaults."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _qa_transcript(issue: "Issue") -> str:
    """Render the issue + its clarifying Q&A into a compact transcript for the
    resolution prompt. ``resolution_prompt`` already embeds the issue brief, so
    this carries the Q&A exchange the brief omits — the only resolution evidence
    the report builder holds (the runner passes no Conversation here)."""
    lines: list[str] = []
    if issue.summary:
        lines.append(f"Issue summary: {issue.summary}")
    for i, qa in enumerate(issue.qa, start=1):
        lines.append(f"Q{i}: {qa.question}")
        answer = (qa.text or "").strip()
        lines.append(f"A{i}: {answer if answer else '(no reply captured)'}")
    if not issue.qa:
        lines.append("(no clarifying exchange was recorded)")
    return "\n".join(lines)


def build_resolution_report(
    issue: "Issue", llm: "LLMClient | None" = None
) -> ResolutionReport:
    """Assemble a :class:`ResolutionReport` from a resolved ``issue``.

    The base ``summary`` comes from ``issue.summary``; the base ``resolution`` is
    derived from the last captured Q&A answer (the agreed action), falling back
    to a generic line. When ``llm`` is supplied, one ``complete_json`` call over
    :func:`resolution_prompt` may replace either with crisper prose; any LLM
    failure (exception or missing keys) is swallowed so the report still builds.
    """
    summary = (issue.summary or "").strip()
    resolution = _default_resolution(issue)

    if llm is not None:
        system, user = resolution_prompt(issue, _qa_transcript(issue))
        try:
            data = llm.complete_json(system, user)
        except Exception:  # noqa: BLE001 — never let the report fail on the LLM
            data = {}
        if isinstance(data, dict):
            llm_summary = str(data.get("summary", "") or "").strip()
            llm_resolution = str(data.get("resolution", "") or "").strip()
            if llm_summary:
                summary = llm_summary
            if llm_resolution:
                resolution = llm_resolution

    if not summary:
        summary = issue.title or "(no summary available)"

    return ResolutionReport(
        issue_id=issue.id,
        title=issue.title,
        category=issue.category,
        severity=issue.severity,
        summary=summary,
        resolution=resolution,
        qa=[QAPair.from_dict(qa.to_dict()) for qa in issue.qa],  # defensive copy
        source_message_ids=list(issue.source_message_ids),
        resolved_at=issue.report_written_at or _now_rfc3339(),
    )


def _default_resolution(issue: "Issue") -> str:
    """A resolution line drawn from the issue's own Q&A when no LLM is used: the
    last clarifying answer is the agreed action/outcome (§6)."""
    for qa in reversed(issue.qa):
        answer = (qa.text or "").strip()
        if answer:
            return f"Clarified via the thread: {answer}"
    return "Issue clarified through the thread and is ready to close."


def _one_line(text: str) -> str:
    """Collapse whitespace/newlines to a single line for the confirmation."""
    return " ".join((text or "").split())


def render_markdown(report: ResolutionReport) -> str:
    """Render the resolution report to Markdown (§6).

    Sections: a title heading, a metadata line (category / severity / id /
    resolved-at), Summary, Resolution, the clarifying Q&A (one block per pair),
    and source message ids. Always non-empty and always contains the title.
    """
    title = report.title or "(untitled issue)"
    severity = _enum_value(report.severity)
    lines: list[str] = [f"# Resolved: {title}", ""]

    meta = [f"**Category:** {report.category or 'n/a'}", f"**Severity:** {severity}"]
    meta.append(f"**Issue id:** {report.issue_id}")
    if report.resolved_at:
        meta.append(f"**Resolved at:** {report.resolved_at}")
    lines.append("  \n".join(meta))
    lines.append("")

    lines.append("## Summary")
    lines.append(report.summary or "(no summary)")
    lines.append("")

    lines.append("## Resolution")
    lines.append(report.resolution or "(no resolution recorded)")
    lines.append("")

    lines.append("## Clarifying Q&A")
    if report.qa:
        for i, qa in enumerate(report.qa, start=1):
            question = (qa.question or "(no question)").strip()
            answer = (qa.text or "").strip() or "(no reply captured)"
            lines.append(f"{i}. **Q:** {question}")
            lines.append(f"   **A:** {answer}")
    else:
        lines.append("_No clarifying questions were needed._")
    lines.append("")

    lines.append("## Source messages")
    if report.source_message_ids:
        for mid in report.source_message_ids:
            lines.append(f"- `{mid}`")
    else:
        lines.append("- (none recorded)")
    lines.append("")

    return "\n".join(lines)


def _report_filename(report: ResolutionReport) -> str:
    """The relative report path, ``reports/issue-<id>.md`` (the leaf is what the
    confirmation references). ``<id>`` is sanitized so it is always a safe single
    path segment."""
    safe = "".join(
        ch if (ch.isalnum() or ch in "-_") else "-" for ch in (report.issue_id or "")
    )
    safe = safe.strip("-") or "unknown"
    return f"issue-{safe}.md"


def write_report(report: ResolutionReport, reports_dir: str) -> str:
    """Render and write the report atomically to ``<reports_dir>/issue-<id>.md``.

    Creates ``reports_dir`` if needed, writes to a temp file in the same
    directory, then ``os.replace`` to the final name (atomic on POSIX). Returns
    the path it wrote (e.g. ``reports/issue-<id>.md``).
    """
    os.makedirs(reports_dir, exist_ok=True)
    filename = _report_filename(report)
    path = os.path.join(reports_dir, filename)
    content = render_markdown(report)

    fd, tmp = tempfile.mkstemp(
        prefix=".tmp-", suffix=".md", dir=reports_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Best-effort cleanup of the temp file on any failure before replace.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def confirmation_line(report: ResolutionReport) -> str:
    """The ≤2-line Chat-thread confirmation (§6 template):

    ``✅ Issue "<title>" resolved — <one-line resolution>. Report: reports/issue-<id>.md``
    """
    title = _one_line(report.title) or "issue"
    resolution = _one_line(report.resolution) or "Clarified and ready to close."
    resolution = resolution.rstrip(".")
    rel_path = f"reports/{_report_filename(report)}"
    return (
        f'✅ Issue "{title}" resolved — {resolution}. '
        f"Report: {rel_path}"
    )
