"""Tests for the standalone incident chat assistant (scripts/apigw_chat.py).

Covers `agent/incident_chat.py`: the assistant's per-step behavior over a
FakeChatClient DM — chat reply grounded in a fixed incident brief, call-back in
English + Vietnamese (invokes the supplied callable), the missed-call proactive
offer, first-step no-backfill, and never answering the bot's own posts — plus the
`scripts/apigw_chat.py` persona-brief helpers.

Fully offline: a `FakeChatClient` stands in for the DM, `MockLLM`/recording stubs
for the LLM, and the call-back is a plain callable — no network, no real calls.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from dataclasses import replace
from unittest import mock

from gchat_agent.agent import incident_chat, prompts
from gchat_agent.agent.incident_chat import IncidentChatAssistant
from gchat_agent.config import load_config
from gchat_agent.llm.mock import MockLLM
from tests.fakes import FakeChatClient

# scripts/ isn't a package; add it so we can import the apigw_chat entry helpers
# (ty can't resolve a flat scripts/ module statically — same as tests/test_dm_resolve).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))
import apigw_chat  # noqa: E402  # ty: ignore[unresolved-import]

BOT = "users/bot"
HUMAN = "users/duc"
DM = "spaces/REPORT"
# Before the FakeChatClient epoch (2026-01-01), so seeding the cursor here makes the
# first step fetch the injected messages instead of pinning the cursor to "now".
_PAST = "2020-01-01T00:00:00Z"

_MISSED_ANNOTATION = [
    {"richLinkMetadata": {"meetSpaceLinkData": {
        "meetingCode": "abc-defg-hij", "type": "HUDDLE", "huddleStatus": "MISSED",
    }}}
]

_BRIEF = prompts.render_incident_brief(
    "API gateway timeout (504s)",
    "Dave",
    "The public API gateway is timing out in prod.",
    [("Numbers", "About 18% of requests return 504s."), ("Ticket", "INFRA-2207")],
)
_SYSTEM = prompts.report_assistant_system_prompt() + "\n\n" + _BRIEF


class _RecordingLLM:
    """An `LLMClient` that records the system prompt + messages it was handed and
    returns a fixed reply, so a test can assert what context it was fed."""

    def __init__(self) -> None:
        self.system: str | None = None
        self.messages: list[dict[str, str]] | None = None

    def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        self.system = system
        self.messages = messages
        return "Here's the latest on the gateway."

    def complete_json(self, system, user, schema_hint=None):  # pragma: no cover
        return {}


def _cfg(**over):
    base = replace(
        load_config(env_file="no-such.env"),
        GOOGLE_CHAT_REPORT_SPACE=DM,
        GEMINI_API_KEY="k",  # so a call-back gate would pass (the callable is faked)
    )
    return replace(base, **over)


class BriefHelpersTest(unittest.TestCase):
    """The scripts/apigw_chat.py persona-brief helpers + the prompt renderer."""

    def test_reporter_name_extracted(self) -> None:
        self.assertEqual(
            apigw_chat._reporter_name("You are Dave, a backend engineer."), "Dave"
        )
        self.assertEqual(apigw_chat._reporter_name(""), "the on-call engineer")

    def test_humanize(self) -> None:
        self.assertEqual(apigw_chat._humanize("repro_steps"), "Repro steps")

    def test_persona_brief_from_scenarios(self) -> None:
        from gchat_agent.agent.staff import load_personas

        personas = load_personas(os.path.join(_REPO_ROOT, "data", "scenarios.json"))
        title, owner, situation, facts = apigw_chat._persona_brief(
            "apigw", personas["apigw"]
        )
        self.assertIn("API gateway", title)
        self.assertEqual(owner, "Dave")
        self.assertTrue(situation)
        # Facts render as (label, value) pairs with humanized labels.
        labels = {label for label, _ in facts}
        self.assertIn("Repro steps", labels)

    def test_render_incident_brief_untrusted_framing(self) -> None:
        self.assertIn("UNTRUSTED", _BRIEF)
        self.assertIn("INFRA-2207", _BRIEF)
        self.assertIn("Dave", _BRIEF)


class IncidentChatStepTest(unittest.TestCase):
    """The assistant's per-step behavior over a FakeChatClient DM."""

    def _assistant(self, *, llm=None, call_back=None, seed_cursor=True, **cfg_over):
        chat = FakeChatClient(me=BOT, space=DM)
        cb = call_back if call_back is not None else mock.Mock(return_value=True)
        assistant = IncidentChatAssistant(
            chat, _cfg(**cfg_over), llm or MockLLM(),
            system_prompt=_SYSTEM, call_back=cb, incident_title="API gateway timeout",
            cursor=_PAST if seed_cursor else None,
        )
        return assistant, chat, cb

    def _bot_posts(self, chat):
        """Bot messages the assistant actually POSTED (non-empty text), excluding
        injected call-lifecycle markers (authored by the bot but blank)."""
        return [m for m in chat.messages if m.sender == BOT and m.text.strip()]

    def test_replies_to_a_human_message(self) -> None:
        assistant, chat, cb = self._assistant()
        chat.inject(HUMAN, "what's the latest on the gateway?")
        summary = assistant.step(BOT)
        self.assertEqual(summary["replied"], 1)
        self.assertEqual(summary["called"], 0)
        self.assertEqual(len(self._bot_posts(chat)), 1)
        self.assertFalse(cb.called)

    def test_reply_is_grounded_in_the_brief(self) -> None:
        llm = _RecordingLLM()
        assistant, chat, _cb = self._assistant(llm=llm)
        chat.inject(HUMAN, "how many requests are failing?")
        assistant.step(BOT)
        system, messages = llm.system, llm.messages
        assert system is not None and messages is not None
        self.assertIn("INFRA-2207", system)
        self.assertIn("18%", system)
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("failing", messages[-1]["content"])

    def test_callback_request_places_call_english(self) -> None:
        assistant, chat, cb = self._assistant()
        chat.inject(HUMAN, "please call me back")
        summary = assistant.step(BOT)
        self.assertEqual(summary["called"], 1)
        self.assertEqual(summary["replied"], 0)
        cb.assert_called_once()
        posts = self._bot_posts(chat)
        self.assertEqual(len(posts), 1)
        self.assertIn("Calling you now", posts[0].text)

    def test_callback_request_vietnamese(self) -> None:
        assistant, chat, cb = self._assistant()
        chat.inject(HUMAN, "gọi lại cho tôi nhé")
        summary = assistant.step(BOT)
        self.assertEqual(summary["called"], 1)
        cb.assert_called_once()

    def test_callback_failure_is_reported(self) -> None:
        cb = mock.Mock(return_value=False)
        assistant, chat, _cb = self._assistant(call_back=cb)
        chat.inject(HUMAN, "call me")
        assistant.step(BOT)
        posts = self._bot_posts(chat)
        self.assertEqual(len(posts), 1)
        self.assertIn("couldn't place the call", posts[0].text)

    def test_placed_call_marks_incident_reported(self) -> None:
        assistant, chat, _cb = self._assistant()
        chat.inject(HUMAN, "call me")
        assistant.step(BOT)
        self.assertTrue(assistant._reported)
        post = self._bot_posts(chat)[0].text
        self.assertIn("Calling you now", post)
        self.assertIn("reported", post.lower())

    def test_reported_status_prepended_and_history_kept(self) -> None:
        llm = _RecordingLLM()
        assistant, chat, _cb = self._assistant(llm=llm)
        # 1) place the call (the call-back path doesn't touch the LLM).
        chat.inject(HUMAN, "please call me")
        assistant.step(BOT)
        self.assertTrue(assistant._reported)
        # 2) a follow-up now answers as history, with the closed-out posture.
        chat.inject(HUMAN, "remind me how many requests were failing?")
        summary = assistant.step(BOT)
        self.assertEqual(summary["replied"], 1)
        system = llm.system
        assert system is not None
        self.assertIn(incident_chat._REPORTED_STATUS, system)
        self.assertIn("INFRA-2207", system)  # brief still present → history

    def test_status_absent_before_any_call(self) -> None:
        llm = _RecordingLLM()
        assistant, chat, _cb = self._assistant(llm=llm)
        chat.inject(HUMAN, "what's going on?")
        assistant.step(BOT)
        system = llm.system
        assert system is not None
        self.assertNotIn(incident_chat._REPORTED_STATUS, system)
        self.assertFalse(assistant._reported)

    def test_missed_call_reopens_after_reported(self) -> None:
        assistant, chat, _cb = self._assistant()
        chat.inject(HUMAN, "call me")
        assistant.step(BOT)
        self.assertTrue(assistant._reported)
        # The outbound call is then missed → re-open + offer to ring again.
        chat.inject(BOT, "", annotations=_MISSED_ANNOTATION)
        with contextlib.redirect_stderr(io.StringIO()):
            summary = assistant.step(BOT)
        self.assertFalse(assistant._reported)
        self.assertEqual(summary["offered"], 1)

    def test_missed_call_offer_is_one_shot(self) -> None:
        assistant, chat, cb = self._assistant()
        missed = chat.inject(BOT, "", annotations=_MISSED_ANNOTATION)
        with contextlib.redirect_stderr(io.StringIO()):
            summary = assistant.step(BOT)
        self.assertEqual(summary["offered"], 1)
        self.assertEqual(summary["replied"], 0)
        self.assertEqual(summary["called"], 0)
        posts = self._bot_posts(chat)
        self.assertEqual(len(posts), 1)
        self.assertIn("missed", posts[0].text.lower())
        # No new messages next step ⇒ no second offer for the same call.
        self.assertEqual(assistant.step(BOT)["offered"], 0)
        self.assertFalse(cb.called)
        self.assertEqual(missed.text, "")

    def test_missed_call_offer_disabled(self) -> None:
        assistant, chat, _cb = self._assistant(REPORT_MISSED_CALL_OFFER=False)
        chat.inject(BOT, "", annotations=_MISSED_ANNOTATION)
        summary = assistant.step(BOT)
        self.assertEqual(summary["offered"], 0)
        self.assertEqual(len(self._bot_posts(chat)), 0)

    def test_ignores_the_bots_own_messages(self) -> None:
        assistant, chat, cb = self._assistant()
        chat.inject(BOT, "📞 Calling you now — pick up.")
        summary = assistant.step(BOT)
        self.assertEqual(summary["replied"], 0)
        self.assertEqual(summary["called"], 0)
        self.assertFalse(cb.called)

    def test_first_step_does_not_backfill(self) -> None:
        # No cursor seed: the first step pins the cursor to "now", so a message
        # already sitting in the DM (fake past epoch) is NOT replayed.
        assistant, chat, _cb = self._assistant(seed_cursor=False)
        chat.inject(HUMAN, "hello, anything new?")
        self.assertEqual(assistant.step(BOT)["replied"], 0)

    def test_unknown_bot_id_skips_step(self) -> None:
        assistant, chat, cb = self._assistant()
        chat.inject(HUMAN, "call me")
        self.assertEqual(
            assistant.step(None), {"replied": 0, "called": 0, "offered": 0}
        )
        self.assertFalse(cb.called)

    def test_multi_turn_history_carries_assistant_role(self) -> None:
        llm = _RecordingLLM()
        assistant, chat, _cb = self._assistant(llm=llm)
        chat.inject(HUMAN, "first question?")
        assistant.step(BOT)  # posts a reply (authored by BOT)
        chat.inject(HUMAN, "and a follow-up?")
        assistant.step(BOT)
        messages = llm.messages
        assert messages is not None
        roles = [m["role"] for m in messages]
        # The bot's own prior reply re-enters history as an `assistant` turn.
        self.assertIn("assistant", roles)
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("follow-up", messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
