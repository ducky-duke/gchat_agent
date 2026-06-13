"""Tests for the resolution-report builder/renderer/writer (§12 + §6).

Exercises :mod:`gchat_agent.agent.report` fully offline: building a
:class:`ResolutionReport` from an :class:`Issue` (with ``llm=None`` and with a
``MockLLM``), the Markdown rendering, the atomic on-disk write, and the
≤2-line Chat-thread confirmation. No network, no real Google/OpenRouter.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from gchat_agent.agent.report import (
    build_resolution_report,
    confirmation_line,
    render_markdown,
    write_report,
)
from gchat_agent.llm.mock import MockLLM
from gchat_agent.models import (
    Issue,
    QAPair,
    ResolutionReport,
    Severity,
    Status,
)


def _make_issue() -> Issue:
    """A resolved issue with a couple of clarifying Q&A exchanges."""
    return Issue(
        id="abc123",
        fingerprint="fp-deadbeef",
        title="Login service failing for EU users",
        summary="EU login requests return 500 errors during peak hours.",
        category="incident",
        severity=Severity.HIGH,
        status=Status.RESOLVED,
        thread_id="spaces/S/threads/T",
        root_message_id="spaces/S/messages/m1",
        source_message_ids=["spaces/S/messages/m1", "spaces/S/messages/m2"],
        missing_info=["owner", "deadline"],
        questions_asked=["Who owns this?", "By when?"],
        qa=[
            QAPair(
                question="Who will own this and drive it to resolution?",
                answer_message_ids=["spaces/S/messages/m3"],
                text="I'll own it — rolling back the bad deploy now.",
            ),
            QAPair(
                question="What is the firm deadline or target date for this?",
                answer_message_ids=["spaces/S/messages/m4"],
                text="Fixed and verified by 2026-06-13.",
            ),
        ],
        report_written_at="2026-06-13T10:00:00Z",
    )


class BuildResolutionReportTest(unittest.TestCase):
    def test_build_without_llm_uses_issue_fields(self) -> None:
        issue = _make_issue()
        report = build_resolution_report(issue, llm=None)

        self.assertIsInstance(report, ResolutionReport)
        self.assertEqual(report.issue_id, issue.id)
        self.assertEqual(report.title, issue.title)
        self.assertEqual(report.category, issue.category)
        self.assertEqual(report.severity, Severity.HIGH)
        # Summary degrades to the issue's stored summary when no LLM is given.
        self.assertEqual(report.summary, issue.summary)
        # Resolution is derived from the last captured Q&A answer.
        self.assertIn("Fixed and verified by 2026-06-13.", report.resolution)
        # resolved_at falls back to report_written_at.
        self.assertEqual(report.resolved_at, "2026-06-13T10:00:00Z")
        # Q&A is carried over (defensive copy: equal value, distinct objects).
        self.assertEqual(len(report.qa), 2)
        self.assertEqual(report.qa[0].question, issue.qa[0].question)
        self.assertIsNot(report.qa[0], issue.qa[0])

    def test_build_with_mock_llm_tightens_prose(self) -> None:
        issue = _make_issue()
        report = build_resolution_report(issue, llm=MockLLM())

        # MockLLM's summarize_resolution branch always returns non-empty
        # summary + resolution strings, so both fields are populated.
        self.assertTrue(report.summary.strip())
        self.assertTrue(report.resolution.strip())
        # The other fields still come straight from the issue.
        self.assertEqual(report.issue_id, issue.id)
        self.assertEqual(report.title, issue.title)
        self.assertEqual(report.severity, Severity.HIGH)
        self.assertEqual(len(report.qa), 2)

    def test_build_without_qa_falls_back(self) -> None:
        issue = _make_issue()
        issue.qa = []
        report = build_resolution_report(issue, llm=None)
        # Generic fallback resolution when there is no clarifying answer.
        self.assertTrue(report.resolution.strip())
        self.assertEqual(report.qa, [])


class RenderMarkdownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.report = build_resolution_report(_make_issue(), llm=None)
        self.md = render_markdown(self.report)

    def test_markdown_is_non_empty(self) -> None:
        self.assertTrue(self.md.strip())

    def test_markdown_contains_title(self) -> None:
        self.assertIn(self.report.title, self.md)
        self.assertIn("# Resolved: Login service failing for EU users", self.md)

    def test_markdown_contains_severity_and_category(self) -> None:
        # Severity renders as the enum's underlying string value ("high").
        self.assertIn("high", self.md)
        self.assertIn(self.report.category, self.md)
        self.assertIn("**Severity:**", self.md)
        self.assertIn("**Category:**", self.md)

    def test_markdown_contains_resolution(self) -> None:
        self.assertIn("## Resolution", self.md)
        self.assertIn(self.report.resolution, self.md)

    def test_markdown_contains_qa(self) -> None:
        self.assertIn("## Clarifying Q&A", self.md)
        for qa in self.report.qa:
            self.assertIn(qa.question, self.md)
            self.assertIn(qa.text, self.md)


class WriteReportTest(unittest.TestCase):
    def test_write_report_creates_file_atomically(self) -> None:
        report = build_resolution_report(_make_issue(), llm=None)
        with tempfile.TemporaryDirectory() as tmp:
            reports_dir = os.path.join(tmp, "reports")
            path = write_report(report, reports_dir)

            # The returned path is reports/issue-<id>.md inside the dir.
            self.assertEqual(os.path.basename(path), "issue-abc123.md")
            self.assertEqual(os.path.dirname(path), reports_dir)

            # The file exists and is non-empty.
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            self.assertTrue(content.strip())
            self.assertEqual(content, render_markdown(report))

            # No leftover temp files from the atomic write.
            leftovers = [
                n for n in os.listdir(reports_dir) if n.startswith(".tmp-")
            ]
            self.assertEqual(leftovers, [])

    def test_write_report_creates_missing_dir(self) -> None:
        report = build_resolution_report(_make_issue(), llm=None)
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "a", "b", "reports")
            os.makedirs(os.path.dirname(nested), exist_ok=True)
            path = write_report(report, nested)
            self.assertTrue(os.path.isfile(path))


class ConfirmationLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.report = build_resolution_report(_make_issue(), llm=None)
        self.line = confirmation_line(self.report)

    def test_contains_resolved_and_report_path(self) -> None:
        self.assertIn("resolved", self.line)
        self.assertIn("reports/issue-abc123.md", self.line)

    def test_contains_title(self) -> None:
        self.assertIn(self.report.title, self.line)

    def test_is_at_most_two_lines(self) -> None:
        self.assertLessEqual(len(self.line.splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
