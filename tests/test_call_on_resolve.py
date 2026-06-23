"""Tests for the outbound voice call on resolve (CALL_ON_RESOLVE).

Covers the incident-payload builder (`build_call_incident` — the JSON contract
`call/gemini_call.py --incident-file` reads) and the runner's spawn path: it launches
the call subprocess off the resolve critical path, serializes concurrent calls,
honors the gate, and never crashes a resolve when the launch fails.

Fully offline: `subprocess.Popen` is patched, so no real browser/audio/Gemini is
touched and no process is spawned.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from gchat_agent.agent.analyzer import Analyzer
from gchat_agent.agent.report import build_resolution_report
from gchat_agent.agent.state import IssueStore
from gchat_agent.config import load_config
from gchat_agent.llm.mock import MockLLM
from gchat_agent.models import (
    Conversation,
    Issue,
    Message,
    QAPair,
    ResolutionReport,
    SenderType,
    Severity,
    Status,
)
from gchat_agent.runner import Runner, build_call_incident
from tests.fakes import FakeChatClient


def _cfg(tmp: str, **over):
    base = replace(
        load_config(env_file="no-such.env"),
        REPORTS_DIR=tmp,
        STATE_FILE=os.path.join(tmp, "s.json"),
        CALL_LOG_DIR=tmp,
    )
    return replace(base, **over)


def _issue() -> Issue:
    return Issue(
        id="abc123",
        fingerprint="fp-1",
        title="API gateway timing out",
        summary="The public API gateway is returning 504s for ~18% of requests.",
        category="incident",
        severity=Severity.HIGH,
        status=Status.CLARIFYING,
        thread_id="spaces/MAIN/threads/T1",
        root_message_id="spaces/MAIN/messages/m1",
        source_message_ids=["spaces/MAIN/messages/m1"],
        qa=[
            QAPair(question="Which endpoints are affected?", text="Game launches and balance checks."),
            QAPair(question="Is there a workaround?", text="Wallet pool restart mitigates it."),
        ],
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


def _thread() -> Conversation:
    conv = Conversation()
    conv.add(_msg("users/alice", "API gateway is throwing 504s", seq=1))
    conv.add(_msg("users/bot", "Which endpoints?", seq=2))
    conv.add(_msg("users/alice", "Game launches and balance checks", seq=3))
    return conv


class _FakePopen:
    """Stand-in for `subprocess.Popen`: records the argv/kwargs it was launched
    with and reports a configurable liveness via `poll()` (None = still running).
    Never starts a process."""

    instances: list["_FakePopen"] = []
    poll_returns = None  # None ⇒ the spawned call is still "in flight"

    def __init__(self, argv, **kwargs) -> None:
        self.argv = list(argv)
        self.kwargs = kwargs
        self.pid = 4242 + len(type(self).instances)
        self._poll = type(self).poll_returns
        type(self).instances.append(self)

    def poll(self):
        return self._poll


@contextlib.contextmanager
def _patched_popen():
    """Patch `subprocess.Popen` with `_FakePopen` and reset its recording, so each
    test sees only its own spawns."""
    _FakePopen.instances = []
    _FakePopen.poll_returns = None
    with mock.patch.object(subprocess, "Popen", _FakePopen):
        yield _FakePopen


class IncidentPayloadTest(unittest.TestCase):
    """`build_call_incident` renders a report into the --incident-file contract."""

    def _report(self, **over) -> ResolutionReport:
        base = ResolutionReport(
            issue_id="abc123",
            title="API gateway timing out",
            category="incident",
            severity=Severity.HIGH,
            summary="The public API gateway is returning 504s for ~18% of requests.",
            resolution="Wallet DB pool exhausted; mitigation by EOD, full fix tomorrow.",
            qa=[
                QAPair(question="Which endpoints are affected?", text="Game launches and balance checks."),
                QAPair(question="", text="p99 latency is 30s+ before the 504."),
            ],
        )
        return replace(base, **over)

    def test_core_fields_and_facts(self) -> None:
        inc = build_call_incident(self._report(), owner="Dave", language="en")
        self.assertEqual(inc["title"], "API gateway timing out")
        self.assertEqual(inc["owner"], "Dave")
        self.assertIn("504s", inc["situation"])
        self.assertEqual(inc["language"], "en")
        facts = inc["facts"]
        self.assertEqual(facts["Severity"], "high")
        self.assertEqual(facts["Category"], "incident")
        self.assertIn("Wallet DB pool", facts["What's being done / current status"])
        # A clarified Q&A answer rides along, keyed by its (one-lined) question.
        self.assertEqual(facts["Which endpoints are affected?"], "Game launches and balance checks.")
        # A Q&A with no question text falls back to a generic label.
        self.assertIn("Clarified detail 2", facts)

    def test_empty_owner_falls_back_to_generic(self) -> None:
        inc = build_call_incident(self._report(), owner="", language="")
        self.assertEqual(inc["owner"], "the on-call engineer")
        self.assertEqual(inc["language"], "en")  # blank language ⇒ English

    def test_open_questions_carried(self) -> None:
        inc = build_call_incident(
            self._report(open_questions=["who is the incident owner?"]),
            owner="Dave", language="vi",
        )
        self.assertEqual(inc["open_questions"], ["who is the incident owner?"])
        self.assertEqual(inc["language"], "vi")

    def test_answerless_qa_is_dropped(self) -> None:
        rep = self._report(qa=[QAPair(question="Anything else?", text="   ")])
        inc = build_call_incident(rep, owner="Dave", language="en")
        self.assertNotIn("Anything else?", inc["facts"])

    def test_built_from_a_real_report(self) -> None:
        # The full path the runner uses: build_resolution_report → build_call_incident.
        report = build_resolution_report(_issue(), llm=None)
        inc = build_call_incident(report, owner="Dave", language="en")
        self.assertEqual(inc["title"], "API gateway timing out")
        self.assertIn("Severity", inc["facts"])


class RunnerCallTest(unittest.TestCase):
    """The runner spawns the call subprocess off the resolve critical path."""

    def _runner(self, tmp, **cfg_over):
        # The call self-gates on a Gemini key; the offline suite has none, so
        # spawn-expecting tests supply a fake one (overridable per test).
        cfg_over.setdefault("GEMINI_API_KEY", "test-key")
        chat = FakeChatClient(me="users/bot", space="spaces/MAIN")
        store = IssueStore(os.path.join(tmp, "issues.json"))
        store.load()
        cfg = _cfg(tmp, **cfg_over)
        runner = Runner(
            chat, Analyzer(MockLLM(), None, 5), store, cfg,
            reports_dir=tmp, llm=MockLLM(),
        )
        return runner, chat, store

    def _dummy_script(self, tmp: str) -> str:
        path = os.path.join(tmp, "fake_call.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# dummy call orchestrator for the test\n")
        return path

    def test_spawns_call_on_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _patched_popen() as Popen:
            script = self._dummy_script(tmp)
            runner, chat, store = self._runner(
                tmp, CALL_ON_RESOLVE=True, CALL_SCRIPT=script,
                CALL_CALLEE="Bob", CALL_LANGUAGE="vi", CALL_URL="https://chat.example/dm",
                CALL_OWNER="Dave",
            )
            issue = _issue()
            with contextlib.redirect_stderr(io.StringIO()):
                runner._resolve(issue, _thread())

            # The issue closed in-thread regardless (call is off the critical path).
            self.assertEqual(issue.status, Status.RESOLVED)
            self.assertTrue(store.is_tombstoned(issue.fingerprint))
            self.assertTrue(any(m.text.startswith("✅") for m in chat.messages))

            # Exactly one call was spawned, detached, with the configured argv.
            self.assertEqual(len(Popen.instances), 1)
            proc = Popen.instances[0]
            self.assertIs(runner._active_call_proc, proc)
            self.assertTrue(proc.kwargs.get("start_new_session"))
            self.assertEqual(proc.argv[0], sys.executable)
            self.assertIn(script, proc.argv)
            self.assertEqual(proc.argv[proc.argv.index("--callee") + 1], "Bob")
            self.assertEqual(proc.argv[proc.argv.index("--language") + 1], "vi")
            self.assertEqual(proc.argv[proc.argv.index("--url") + 1], "https://chat.example/dm")

            # The incident JSON it reads was written with the report's facts.
            inc_path = proc.argv[proc.argv.index("--incident-file") + 1]
            self.assertTrue(os.path.isfile(inc_path))
            with open(inc_path, encoding="utf-8") as fh:
                inc = json.load(fh)
            self.assertEqual(inc["title"], "API gateway timing out")
            self.assertEqual(inc["owner"], "Dave")
            self.assertEqual(inc["facts"]["Severity"], "high")

    def test_gate_off_spawns_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _patched_popen() as Popen:
            runner, chat, _ = self._runner(
                tmp, CALL_ON_RESOLVE=False, CALL_SCRIPT=self._dummy_script(tmp),
            )
            runner._resolve(_issue(), _thread())
            self.assertEqual(len(Popen.instances), 0)
            self.assertIsNone(runner._active_call_proc)
            self.assertTrue(any(m.text.startswith("✅") for m in chat.messages))

    def test_no_gemini_key_skips_call(self) -> None:
        # Default-ON but self-gating: with no Gemini key the call can't work, so the
        # spawn is skipped silently (this is the offline/test default).
        with tempfile.TemporaryDirectory() as tmp, _patched_popen() as Popen:
            runner, chat, _ = self._runner(
                tmp, CALL_ON_RESOLVE=True, GEMINI_API_KEY="",
                CALL_SCRIPT=self._dummy_script(tmp),
            )
            runner._resolve(_issue(), _thread())
            self.assertEqual(len(Popen.instances), 0)
            self.assertIsNone(runner._active_call_proc)
            self.assertTrue(any(m.text.startswith("✅") for m in chat.messages))

    def test_missing_script_skips_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _patched_popen() as Popen:
            runner, _chat, _ = self._runner(
                tmp, CALL_ON_RESOLVE=True,
                CALL_SCRIPT=os.path.join(tmp, "does-not-exist.py"),
            )
            issue = _issue()
            with contextlib.redirect_stderr(io.StringIO()):
                runner._resolve(issue, _thread())
            self.assertEqual(len(Popen.instances), 0)
            self.assertEqual(issue.status, Status.RESOLVED)  # resolve still completes

    def test_calls_are_serialized(self) -> None:
        # A prior call still in flight (poll() is None) ⇒ the next resolve skips its
        # call rather than racing the shared browser/audio devices.
        with tempfile.TemporaryDirectory() as tmp, _patched_popen() as Popen:
            script = self._dummy_script(tmp)
            runner, _chat, _ = self._runner(
                tmp, CALL_ON_RESOLVE=True, CALL_SCRIPT=script,
            )
            with contextlib.redirect_stderr(io.StringIO()):
                runner._resolve(_issue(), _thread())
                # A second, distinct issue resolving while the first call runs.
                other = replace(_issue(), id="def456", fingerprint="fp-2")
                runner._resolve(other, _thread())
            self.assertEqual(len(Popen.instances), 1, "second call must be skipped")

    def test_finished_prior_call_allows_a_new_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _patched_popen() as Popen:
            script = self._dummy_script(tmp)
            runner, _chat, _ = self._runner(
                tmp, CALL_ON_RESOLVE=True, CALL_SCRIPT=script,
            )
            with contextlib.redirect_stderr(io.StringIO()):
                runner._resolve(_issue(), _thread())
                # The first call has finished — poll() now returns an exit code.
                runner._active_call_proc._poll = 0
                other = replace(_issue(), id="def456", fingerprint="fp-2")
                runner._resolve(other, _thread())
            self.assertEqual(len(Popen.instances), 2)

    def test_launch_failure_never_crashes_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = self._dummy_script(tmp)
            runner, _chat, _ = self._runner(
                tmp, CALL_ON_RESOLVE=True, CALL_SCRIPT=script,
            )
            issue = _issue()
            with mock.patch.object(subprocess, "Popen", side_effect=OSError("boom")):
                with contextlib.redirect_stderr(io.StringIO()):
                    runner._resolve(issue, _thread())
            # Resolve still completed; the report still landed on disk.
            self.assertEqual(issue.status, Status.RESOLVED)
            self.assertTrue(os.path.isfile(os.path.join(tmp, f"issue-{issue.id}.md")))


if __name__ == "__main__":
    unittest.main()
