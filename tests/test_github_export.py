"""Tests for GitHub issue export.

Covers the payload renderer (`render_chat_transcript` / `render_github_issue`),
the REST client's unknown-label fallback, the `build_github` factory + config
validation, and the runner's *background* GitHub-export path (filed off the
resolve critical path, best-effort, never crashes a resolve).

Fully offline: `FakeGitHubClient` records issues; no network, token, or `gh`.
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from dataclasses import replace

from gchat_agent.agent.analyzer import Analyzer
from gchat_agent.agent.report import (
    build_resolution_report,
    github_issue_labels,
    render_chat_transcript,
    render_github_issue,
)
from gchat_agent.agent.state import IssueStore
from gchat_agent.config import load_config, validate_config
from gchat_agent.github.rest import (
    GitHubRestClient,
    _UnprocessableEntity,
    build_github,
)
from gchat_agent.llm.mock import MockLLM
from gchat_agent.models import (
    Conversation,
    Issue,
    Message,
    SenderType,
    Severity,
    Status,
)
from gchat_agent.runner import Runner
from tests.fakes import FakeChatClient, FakeGitHubClient, InlineExecutor


def _cfg(tmp: str, **over):
    base = replace(
        load_config(env_file="no-such.env"),
        REPORTS_DIR=tmp,
        STATE_FILE=os.path.join(tmp, "s.json"),
    )
    return replace(base, **over)


def _issue() -> Issue:
    return Issue(
        id="abc123",
        fingerprint="fp-1",
        title="Login service failing for EU users",
        summary="EU login requests return 500 during peak hours.",
        category="incident",
        severity=Severity.HIGH,
        status=Status.CLARIFYING,
        thread_id="spaces/MAIN/threads/T1",
        root_message_id="spaces/MAIN/messages/m1",
        source_message_ids=["spaces/MAIN/messages/m1"],
    )


def _msg(sender: str, text: str, *, seq: int = 1) -> Message:
    return Message(
        id=f"spaces/MAIN/messages/m{seq}",
        space="spaces/MAIN",
        thread_id="spaces/MAIN/threads/T1",
        sender=sender,
        sender_type=SenderType.HUMAN,
        text=text,
        create_time=f"2026-01-01T00:00:0{seq}Z",
    )


class _DeferredExecutor:
    """Captures submitted tasks WITHOUT running them, so a test can assert the
    resolve cycle finished (issue closed in-thread) before the GitHub file even
    started, then run it explicitly."""

    def __init__(self) -> None:
        self.tasks: list = []

    def submit(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))

    def run_all(self) -> None:
        for fn, args, kwargs in self.tasks:
            fn(*args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:  # noqa: ARG002
        pass


class TranscriptTest(unittest.TestCase):
    def test_labels_bot_vs_human(self) -> None:
        msgs = [
            _msg("users/alice", "Login is 500ing", seq=1),
            _msg("users/bot", "Which region?", seq=2),
        ]
        out = render_chat_transcript(msgs, bot_id="users/bot")
        self.assertIn("🧑 users/alice", out)
        self.assertIn("🤖 bot", out)
        self.assertNotIn("🧑 users/bot", out)  # the bot is never labeled a person
        self.assertIn("> Login is 500ing", out)

    def test_multiline_text_is_quoted_per_line(self) -> None:
        out = render_chat_transcript([_msg("users/alice", "line one\nline two")])
        self.assertIn("> line one", out)
        self.assertIn("> line two", out)

    def test_empty_placeholder(self) -> None:
        self.assertIn("no messages", render_chat_transcript([]))


class PayloadTest(unittest.TestCase):
    def test_render_github_issue_shape(self) -> None:
        report = build_resolution_report(_issue(), llm=None)
        transcript = render_chat_transcript(
            [_msg("users/alice", "EU login returns 500")], bot_id="users/bot"
        )
        title, body, labels = render_github_issue(report, transcript)
        self.assertEqual(title, "Login service failing for EU users")
        # The GitHub body must NOT headline "# Resolved: <title>" — GitHub already
        # renders the issue title above the body, so the H1 just duplicates it.
        self.assertNotIn("# Resolved:", body)
        # Body reuses the Markdown report AND carries the collected transcript.
        self.assertIn("## Summary", body)
        self.assertIn("## Resolution", body)
        self.assertIn("## Collected messages", body)
        self.assertIn("EU login returns 500", body)
        self.assertIn("Auto-filed by the gchat_agent", body)
        self.assertIn("abc123", body)  # issue id in the footer
        self.assertEqual(labels, ["auto-filed", "severity:high"])

    def test_labels_track_severity(self) -> None:
        for sev, want in (
            (Severity.LOW, "severity:low"),
            (Severity.MEDIUM, "severity:med"),
            (Severity.HIGH, "severity:high"),
        ):
            report = build_resolution_report(replace(_issue(), severity=sev), llm=None)
            self.assertEqual(github_issue_labels(report), ["auto-filed", want])


class RestClientTest(unittest.TestCase):
    """The transport's label-fallback, exercised with a stubbed `_post_issue` so
    no network is touched."""

    def _client(self) -> GitHubRestClient:
        return GitHubRestClient("owner/repo", "tok")

    def test_labels_sent_on_success(self) -> None:
        client = self._client()
        seen: list[dict] = []

        def stub(payload):
            seen.append(payload)
            return {"html_url": "https://github.test/issues/7"}

        client._post_issue = stub  # type: ignore[assignment]
        url = client.create_issue("t", "b", ["auto-filed", "severity:high"])
        self.assertEqual(url, "https://github.test/issues/7")
        self.assertEqual(seen[0]["labels"], ["auto-filed", "severity:high"])

    def test_unknown_label_retries_without_labels(self) -> None:
        client = self._client()
        seen: list[dict] = []

        def stub(payload):
            seen.append(dict(payload))
            if "labels" in payload:
                raise _UnprocessableEntity("Label does not exist")
            return {"html_url": "https://github.test/issues/9"}

        client._post_issue = stub  # type: ignore[assignment]
        url = client.create_issue("t", "b", ["nope"])
        self.assertEqual(url, "https://github.test/issues/9")
        self.assertEqual(len(seen), 2)
        self.assertIn("labels", seen[0])
        self.assertNotIn("labels", seen[1])  # retried without labels

    def test_422_without_labels_propagates(self) -> None:
        client = self._client()

        def stub(payload):
            raise _UnprocessableEntity("validation failed")

        client._post_issue = stub  # type: ignore[assignment]
        with self.assertRaises(_UnprocessableEntity):
            client.create_issue("t", "b", [])  # no labels → nothing to drop


class FactoryAndConfigTest(unittest.TestCase):
    def test_off_returns_none(self) -> None:
        cfg = _cfg("/tmp", GITHUB_ISSUES=False)
        self.assertIsNone(build_github(cfg))

    def test_on_with_token_builds_client(self) -> None:
        cfg = _cfg(
            "/tmp",
            GITHUB_ISSUES=True,
            GITHUB_REPO="owner/repo",
            GITHUB_TOKEN="secret-tok",
        )
        client = build_github(cfg)
        self.assertIsInstance(client, GitHubRestClient)
        self.assertEqual(client.repo, "owner/repo")
        self.assertEqual(client._token, "secret-tok")

    def test_validate_requires_repo_when_on(self) -> None:
        bad = replace(
            load_config(env_file="no-such.env"),
            GITHUB_ISSUES=True,
            GITHUB_REPO="",
        )
        with self.assertRaises(ValueError):
            validate_config(bad)
        worse = replace(bad, GITHUB_REPO="no-slash")
        with self.assertRaises(ValueError):
            validate_config(worse)
        ok = replace(bad, GITHUB_REPO="owner/repo")
        self.assertIs(validate_config(ok), ok)  # well-formed repo passes


class RunnerGithubTest(unittest.TestCase):
    """The runner files a resolved issue to GitHub OFF the critical path."""

    def _runner(self, tmp, *, github, publish_ex, delivery="disk"):
        chat = FakeChatClient(me="users/bot", space="spaces/MAIN")
        store = IssueStore(os.path.join(tmp, "issues.json"))
        store.load()
        cfg = _cfg(tmp, REPORT_DELIVERY=delivery)
        runner = Runner(
            chat,
            Analyzer(MockLLM(), None, 5),
            store,
            cfg,
            reports_dir=tmp,
            llm=MockLLM(),
            github=github,
            publish_executor=publish_ex,
        )
        return runner, chat, store

    def _thread(self) -> Conversation:
        conv = Conversation()
        conv.add(_msg("users/alice", "Login service is returning 500", seq=1))
        conv.add(_msg("users/bot", "Which region is affected?", seq=2))
        conv.add(_msg("users/alice", "EU, during peak hours", seq=3))
        return conv

    def test_files_issue_with_report_and_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHubClient()
            runner, chat, _ = self._runner(tmp, github=gh, publish_ex=InlineExecutor())
            runner._resolve(_issue(), self._thread())

            self.assertEqual(len(gh.issues), 1)
            filed = gh.issues[0]
            self.assertEqual(filed["title"], "Login service failing for EU users")
            self.assertEqual(filed["labels"], ["auto-filed", "severity:high"])
            # The collected thread (both reporter and bot) rides along in the body.
            self.assertIn("Login service is returning 500", filed["body"])
            self.assertIn("EU, during peak hours", filed["body"])
            self.assertIn("🤖 bot", filed["body"])
            self.assertIn("## Collected messages", filed["body"])

    def test_export_is_off_critical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ex = _DeferredExecutor()
            gh = FakeGitHubClient()
            runner, chat, store = self._runner(tmp, github=gh, publish_ex=ex)
            issue = _issue()

            runner._resolve(issue, self._thread())

            # The issue is closed the instant _resolve returns — GitHub is queued.
            self.assertEqual(issue.status, Status.RESOLVED)
            self.assertTrue(store.is_tombstoned(issue.fingerprint))
            self.assertTrue(any(m.text.startswith("✅") for m in chat.messages))
            self.assertEqual(len(ex.tasks), 1, "GitHub file must be queued, not inline")
            self.assertEqual(gh.issues, [])

            ex.run_all()  # what the background worker would do
            self.assertEqual(len(gh.issues), 1)

    def test_gate_off_files_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, chat, _ = self._runner(tmp, github=None, publish_ex=None)
            runner._resolve(_issue(), self._thread())
            # No publish pool is ever created when the feature is off.
            self.assertIsNone(runner._publish_executor)
            self.assertTrue(any(m.text.startswith("✅") for m in chat.messages))

    def test_github_failure_never_crashes_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHubClient(fail=True)
            runner, chat, _ = self._runner(tmp, github=gh, publish_ex=InlineExecutor())
            issue = _issue()
            with contextlib.redirect_stderr(io.StringIO()):  # expected warning
                runner._resolve(issue, self._thread())
            # Resolve still completed; the report still landed on disk (disk path).
            self.assertEqual(issue.status, Status.RESOLVED)
            self.assertEqual(gh.issues, [])
            self.assertTrue(os.path.isfile(os.path.join(tmp, f"issue-{issue.id}.md")))


if __name__ == "__main__":
    unittest.main()
