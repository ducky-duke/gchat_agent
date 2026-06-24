"""Tests for the two-way report-DM assistant (REPORT_ASSISTANT).

Covers `agent/report_assistant.py`: the pure intent/annotation helpers, the
assistant's per-step behavior (chat reply, report-grounded context, call-back in
English + Vietnamese, the missed-call proactive offer, first-run no-backfill, and
never answering the bot's own posts), and the runner integration (`run_cycle`
drives the assistant when a report-DM client is wired).

Fully offline: a `FakeChatClient` stands in for the report DM, `MockLLM`/recording
stubs for the LLM, and the call-back is a plain callable — no network, no calls.
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from gchat_agent.agent.analyzer import Analyzer
from gchat_agent.agent.report_assistant import (
    ReportAssistant,
    huddle_status,
    looks_like_callback_request,
)
from gchat_agent.agent.state import IssueStore
from gchat_agent.config import load_config
from gchat_agent.llm.mock import MockLLM
from gchat_agent.models import Issue, Message, SenderType, Severity, Status
from gchat_agent.runner import Runner
from tests.fakes import FakeChatClient

BOT = "users/bot"
HUMAN = "users/duc"
REPORT_SPACE = "spaces/REPORT"
# A timestamp before the FakeChatClient epoch (2026-01-01), so pre-seeding the
# report cursor here makes the first step actually fetch the injected messages
# instead of pinning the cursor to wall-clock "now" (the no-backfill default).
_PAST = "2020-01-01T00:00:00Z"

_MISSED_ANNOTATION = [
    {"richLinkMetadata": {"meetSpaceLinkData": {
        "meetingCode": "abc-defg-hij", "type": "HUDDLE", "huddleStatus": "MISSED",
    }}}
]


def _cfg(tmp: str, **over):
    base = replace(
        load_config(env_file="no-such.env"),
        REPORTS_DIR=tmp,
        STATE_FILE=os.path.join(tmp, "s.json"),
        REPORT_ASSISTANT=True,
        GOOGLE_CHAT_REPORT_SPACE=REPORT_SPACE,
    )
    return replace(base, **over)


def _resolved_issue(issue_id: str = "abc123", title: str = "API gateway timing out") -> Issue:
    return Issue(
        id=issue_id,
        fingerprint=f"fp-{issue_id}",
        title=title,
        summary="The public API gateway is returning 504s for ~18% of requests.",
        category="incident",
        severity=Severity.HIGH,
        status=Status.RESOLVED,
        thread_id="spaces/MAIN/threads/T1",
        root_message_id="spaces/MAIN/messages/m1",
    )


class _RecordingLLM:
    """An `LLMClient` that records the system prompt + messages it was handed and
    returns a fixed reply, so a test can assert what context the assistant fed it."""

    def __init__(self) -> None:
        self.system: str | None = None
        self.messages: list[dict[str, str]] | None = None

    def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        self.system = system
        self.messages = messages
        return "Here's the latest."

    def complete_json(self, system, user, schema_hint=None):  # pragma: no cover
        return {}


class HelperTest(unittest.TestCase):
    """The pure, deterministic helpers (no I/O)."""

    def test_callback_phrases_match(self) -> None:
        for text in (
            "call me", "please call me back", "can you ring me?",
            "CALL ME BACK now", "gọi lại cho tôi nhé", "gọi cho mình đi",
        ):
            self.assertTrue(looks_like_callback_request(text), text)

    def test_non_callback_messages_do_not_match(self) -> None:
        for text in (
            "what's the status of the gateway incident?",
            "thanks, that's clear", "summarize the last report", "",
        ):
            self.assertFalse(looks_like_callback_request(text), text)

    def test_huddle_status_extracted(self) -> None:
        m = Message(
            id="m1", space=REPORT_SPACE, thread_id="t", sender=BOT,
            sender_type=SenderType.HUMAN, text="", create_time="2026-01-01T00:00:01Z",
            annotations=_MISSED_ANNOTATION,
        )
        self.assertEqual(huddle_status(m), "MISSED")

    def test_huddle_status_none_for_plain_message(self) -> None:
        m = Message(
            id="m1", space=REPORT_SPACE, thread_id="t", sender=HUMAN,
            sender_type=SenderType.HUMAN, text="hi", create_time="2026-01-01T00:00:01Z",
        )
        self.assertIsNone(huddle_status(m))


class ReportAssistantStepTest(unittest.TestCase):
    """The assistant's per-step behavior over a FakeChatClient report DM."""

    def _assistant(self, tmp, *, llm=None, call_back=None, preseed_cursor=True, **cfg_over):
        chat = FakeChatClient(me=BOT, space=REPORT_SPACE)
        store = IssueStore(os.path.join(tmp, "issues.json"))
        store.load()
        if preseed_cursor:
            # Simulate an already-running assistant whose cursor sits in the past,
            # so this step fetches the injected (fake-epoch) messages.
            store.set_report_cursor(_PAST, [])
        cb = call_back if call_back is not None else mock.Mock(return_value=True)
        assistant = ReportAssistant(
            chat, store, _cfg(tmp, **cfg_over), llm or MockLLM(), tmp, call_back=cb,
        )
        return assistant, chat, store, cb

    def _bot_messages(self, chat):
        return [m for m in chat.messages if m.sender == BOT]

    def _bot_posts(self, chat):
        """Bot messages the assistant actually POSTED (non-empty text), excluding
        injected call-lifecycle markers (which are authored by the bot but blank)."""
        return [m for m in chat.messages if m.sender == BOT and m.text.strip()]

    def test_replies_to_a_human_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assistant, chat, _store, cb = self._assistant(tmp)
            chat.inject(HUMAN, "what's the latest incident?")
            summary = assistant.step(BOT)
            self.assertEqual(summary["replied"], 1)
            self.assertEqual(summary["called"], 0)
            self.assertEqual(len(self._bot_messages(chat)), 1)
            self.assertFalse(cb.called)

    def test_context_includes_tracked_incident_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = _RecordingLLM()
            assistant, chat, store, _cb = self._assistant(tmp, llm=llm)
            # A resolved issue + its on-disk report should appear in the context.
            issue = _resolved_issue()
            store.state.issues.append(issue)
            with open(os.path.join(tmp, f"issue-{issue.id}.md"), "w", encoding="utf-8") as fh:
                fh.write("# Resolution report\n\nWallet DB pool exhausted; fix tomorrow.\n")
            chat.inject(HUMAN, "what happened with the gateway?")
            assistant.step(BOT)
            system, messages = llm.system, llm.messages
            assert system is not None and messages is not None
            self.assertIn("API gateway timing out", system)
            self.assertIn("Wallet DB pool exhausted", system)
            # The user's message is the last turn handed to the LLM.
            self.assertEqual(messages[-1]["role"], "user")
            self.assertIn("gateway", messages[-1]["content"])

    def test_callback_request_places_call_english(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assistant, chat, _store, cb = self._assistant(tmp)
            chat.inject(HUMAN, "please call me back")
            summary = assistant.step(BOT)
            self.assertEqual(summary["called"], 1)
            self.assertEqual(summary["replied"], 0)
            cb.assert_called_once()
            bot = self._bot_messages(chat)
            self.assertEqual(len(bot), 1)
            self.assertIn("Calling you back", bot[0].text)

    def test_callback_request_vietnamese(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assistant, chat, _store, cb = self._assistant(tmp)
            chat.inject(HUMAN, "gọi lại cho tôi nhé")
            summary = assistant.step(BOT)
            self.assertEqual(summary["called"], 1)
            cb.assert_called_once()

    def test_callback_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cb = mock.Mock(return_value=False)
            assistant, chat, _store, _cb = self._assistant(tmp, call_back=cb)
            chat.inject(HUMAN, "call me")
            assistant.step(BOT)
            bot = self._bot_messages(chat)
            self.assertEqual(len(bot), 1)
            self.assertIn("couldn't place the call", bot[0].text)

    def test_missed_call_offer_is_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assistant, chat, store, cb = self._assistant(tmp)
            store.set_last_relayed_issue_id("abc123")
            store.state.issues.append(_resolved_issue())
            missed = chat.inject(BOT, "", annotations=_MISSED_ANNOTATION)
            with contextlib.redirect_stderr(io.StringIO()):
                summary = assistant.step(BOT)
            self.assertEqual(summary["offered"], 1)
            self.assertEqual(summary["replied"], 0)
            self.assertEqual(summary["called"], 0)
            self.assertTrue(store.has_offered_missed_call(missed.id))
            offers = self._bot_posts(chat)
            self.assertEqual(len(offers), 1)
            self.assertIn("missed", offers[0].text.lower())
            # No new messages next step ⇒ no second offer.
            self.assertEqual(assistant.step(BOT)["offered"], 0)
            self.assertFalse(cb.called)

    def test_missed_call_offer_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assistant, chat, _store, _cb = self._assistant(
                tmp, REPORT_MISSED_CALL_OFFER=False
            )
            chat.inject(BOT, "", annotations=_MISSED_ANNOTATION)
            summary = assistant.step(BOT)
            self.assertEqual(summary["offered"], 0)
            self.assertEqual(len(self._bot_posts(chat)), 0)

    def test_ignores_the_bots_own_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assistant, chat, _store, cb = self._assistant(tmp)
            chat.inject(BOT, "✅ Issue recorded: API gateway timing out")
            summary = assistant.step(BOT)
            self.assertEqual(summary["replied"], 0)
            self.assertFalse(cb.called)

    def test_first_run_does_not_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # No cursor pre-seed: the first step pins the cursor to "now", so a
            # message already sitting in the DM (fake past epoch) is NOT replayed.
            assistant, chat, _store, _cb = self._assistant(tmp, preseed_cursor=False)
            chat.inject(HUMAN, "hello, anything new?")
            summary = assistant.step(BOT)
            self.assertEqual(summary["replied"], 0)

    def test_unknown_bot_id_skips_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assistant, chat, _store, cb = self._assistant(tmp)
            chat.inject(HUMAN, "call me")
            summary = assistant.step(None)
            self.assertEqual(summary, {"replied": 0, "called": 0, "offered": 0})
            self.assertFalse(cb.called)


class RunnerIntegrationTest(unittest.TestCase):
    """`Runner.run_cycle` drives the assistant when a report-DM client is wired."""

    def test_run_cycle_services_the_report_dm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            main_chat = FakeChatClient(me=BOT, space="spaces/MAIN")
            report_chat = FakeChatClient(me=BOT, space=REPORT_SPACE)
            store = IssueStore(cfg.STATE_FILE)
            store.load()
            # Persist a past report cursor so the (saved) state survives the
            # run_cycle store.load() and the injected message is fetched.
            store.set_report_cursor(_PAST, [])
            store.save()
            runner = Runner(
                main_chat, Analyzer(MockLLM(), None, 5), store, cfg,
                reports_dir=tmp, llm=MockLLM(), report_chat=report_chat,
            )
            report_chat.inject(HUMAN, "what's the status of the incident?")
            with contextlib.redirect_stderr(io.StringIO()):
                summary = runner.run_cycle()
            self.assertEqual(summary["replied"], 1)
            self.assertTrue(any(m.sender == BOT for m in report_chat.messages))


if __name__ == "__main__":
    unittest.main()
