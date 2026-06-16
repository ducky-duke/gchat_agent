"""Tests for voice-report delivery (narration builder, caption, confirmation
reference, and the runner's `REPORT_DELIVERY` disk/voice/both routing).

Fully offline: `MockLLM` narrates, `MockTTS` "synthesizes", and `FakeChatClient`
records the voice attachment — no network, key, or Google credentials.
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
    build_narration,
    build_resolution_report,
    confirmation_line,
    voice_caption,
    voice_message_text,
)
from gchat_agent.config import load_config
from gchat_agent.llm.mock import MockLLM
from gchat_agent.llm.tts import MockTTS
from gchat_agent.models import Conversation, Issue, Severity, Status
from gchat_agent.runner import Runner
from gchat_agent.agent.state import IssueStore
from tests.fakes import FakeChatClient


def _cfg(tmp: str, **over):
    base = replace(load_config(env_file="no-such.env"), REPORTS_DIR=tmp, STATE_FILE=os.path.join(tmp, "s.json"))
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


class NarrationTest(unittest.TestCase):
    def test_fallback_without_llm_is_plain_spoken(self) -> None:
        report = build_resolution_report(_issue(), llm=None)
        narration = build_narration(report, llm=None)
        self.assertTrue(narration)
        # Spoken prose — no Markdown artifacts that read badly aloud.
        for bad in ("#", "**", "`", "✅", "🔊"):
            self.assertNotIn(bad, narration)
        self.assertIn("Login service failing", narration)

    def test_mock_llm_narration_nonempty(self) -> None:
        report = build_resolution_report(_issue(), llm=MockLLM())
        narration = build_narration(report, llm=MockLLM())
        self.assertTrue(narration.strip())
        self.assertNotIn("**", narration)

    def test_voice_caption_names_issue(self) -> None:
        report = build_resolution_report(_issue(), llm=None)
        cap = voice_caption(report)
        self.assertIn("Login service failing for EU users", cap)

    def test_voice_message_text_includes_transcript(self) -> None:
        report = build_resolution_report(_issue(), llm=None)
        narration = build_narration(report, llm=None)
        body = voice_message_text(report, narration)
        # Both the caption header (issue title) and the spoken transcript appear,
        # so the report is readable without playing the download-only audio card.
        self.assertIn("Login service failing for EU users", body)
        self.assertIn(narration, body)
        self.assertIn("Transcript", body)

    def test_voice_message_text_falls_back_to_caption_when_empty(self) -> None:
        report = build_resolution_report(_issue(), llm=None)
        self.assertEqual(voice_message_text(report, ""), voice_caption(report))

    def test_voice_message_text_caps_overlong_transcript(self) -> None:
        # A misbehaving LLM could ignore the 2-4 sentence instruction; the body
        # must stay within Chat's ~4096-char text limit so the voice post never
        # fails on length (which would drop the in-chat voice+transcript to disk).
        report = build_resolution_report(_issue(), llm=None)
        body = voice_message_text(report, "blah " * 2000)  # ~10k chars pre-cap
        self.assertLessEqual(len(body), 4096)
        self.assertTrue(body.rstrip().endswith("…"), "over-long transcript is truncated")
        self.assertIn("Login service failing for EU users", body)  # caption preserved


class ConfirmationRefTest(unittest.TestCase):
    def test_default_ref_unchanged(self) -> None:
        report = build_resolution_report(_issue(), llm=None)
        line = confirmation_line(report)
        self.assertIn("Report: reports/issue-abc123.md", line)

    def test_override_ref(self) -> None:
        report = build_resolution_report(_issue(), llm=None)
        line = confirmation_line(report, "🔊 Voice report sent to spaces/REPORTS")
        self.assertIn("Voice report sent to spaces/REPORTS", line)
        self.assertNotIn("reports/issue-abc123.md", line)


class _Harness:
    def __init__(self, tmp: str, *, delivery: str, voice_space: str = "", tts=None):
        self.chat = FakeChatClient(me="users/bot", space="spaces/MAIN")
        self.store = IssueStore(os.path.join(tmp, "issues.json"))
        self.store.load()
        cfg = _cfg(tmp, REPORT_DELIVERY=delivery, GOOGLE_VOICE_SPACE=voice_space)
        analyzer = Analyzer(MockLLM(), None, 5)
        self.runner = Runner(self.chat, analyzer, self.store, cfg,
                             reports_dir=tmp, llm=MockLLM(), tts=tts)
        self.tmp = tmp
        self.issue = _issue()

    def resolve(self):
        self.runner._resolve(self.issue, Conversation())

    @property
    def disk_path(self) -> str:
        return os.path.join(self.tmp, f"issue-{self.issue.id}.md")

    def confirmation_text(self) -> str:
        # The ✅ confirmation lands in the issue thread (authored by the bot).
        for m in reversed(self.chat.messages):
            if m.text.startswith("✅"):
                return m.text
        return ""


class RunnerDeliveryTest(unittest.TestCase):
    def test_disk_only_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp, delivery="disk")
            h.resolve()
            self.assertTrue(os.path.isfile(h.disk_path))
            self.assertEqual(h.chat.voice_posts, [])
            self.assertIn("Report: reports/issue-abc123.md", h.confirmation_text())

    def test_voice_to_separate_space_no_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp, delivery="voice", voice_space="spaces/REPORTS",
                         tts=MockTTS())
            h.resolve()
            self.assertFalse(os.path.isfile(h.disk_path))  # no disk dump
            self.assertEqual(len(h.chat.voice_posts), 1)
            post = h.chat.voice_posts[0]
            self.assertEqual(post["space"], "spaces/REPORTS")
            self.assertEqual(post["filename"], "issue-abc123.mp3")
            self.assertTrue(post["audio"])  # synthesized bytes flowed through
            # Voice-only: the confirmation no longer announces the audio
            # destination, and there is no on-disk file to reference either.
            text = h.confirmation_text()
            self.assertNotIn("Voice report", text)
            self.assertNotIn("reports/issue-abc123.md", text)
            self.assertIn("recorded", text)

    def test_voice_post_carries_transcript_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp, delivery="voice", voice_space="spaces/REPORTS",
                         tts=MockTTS())
            h.resolve()
            post = h.chat.voice_posts[0]
            # The voice message body carries the spoken transcript (not just the
            # bare caption), so the report reads in-thread without the audio file.
            self.assertIn("Login service failing for EU users", post["text"])
            self.assertIn("Transcript", post["text"])
            bare_caption = voice_caption(build_resolution_report(_issue(), llm=None))
            self.assertGreater(len(post["text"]), len(bare_caption))

    def test_voice_fallback_in_thread_when_no_space(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp, delivery="voice", voice_space="", tts=MockTTS())
            h.resolve()
            self.assertEqual(len(h.chat.voice_posts), 1)
            # Falls back to the issue's own thread.
            self.assertEqual(h.chat.voice_posts[0]["thread_id"], h.issue.thread_id)
            # No voice-destination announcement in the confirmation anymore.
            self.assertNotIn("Voice report", h.confirmation_text())
            self.assertFalse(os.path.isfile(h.disk_path))

    def test_both_writes_disk_and_voice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp, delivery="both", voice_space="spaces/REPORTS",
                         tts=MockTTS())
            h.resolve()
            self.assertTrue(os.path.isfile(h.disk_path))
            self.assertEqual(len(h.chat.voice_posts), 1)
            text = h.confirmation_text()
            # 'both' still references the on-disk report, but no longer announces
            # the voice destination in the confirmation.
            self.assertNotIn("Voice report", text)
            self.assertIn("Report: reports/issue-abc123.md", text)

    def test_voice_requested_but_no_tts_falls_back_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp, delivery="voice", voice_space="spaces/REPORTS", tts=None)
            h.resolve()
            self.assertTrue(os.path.isfile(h.disk_path))  # safety net
            self.assertEqual(h.chat.voice_posts, [])
            self.assertIn("Report: reports/issue-abc123.md", h.confirmation_text())

    def test_voice_synthesis_failure_falls_back_to_disk(self) -> None:
        class _BoomTTS:
            def synthesize(self, text: str) -> bytes:
                raise RuntimeError("tts down")

        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp, delivery="voice", voice_space="spaces/REPORTS",
                         tts=_BoomTTS())
            with contextlib.redirect_stderr(io.StringIO()):  # expected warning
                h.resolve()
            self.assertTrue(os.path.isfile(h.disk_path))  # never lose the report
            self.assertEqual(h.chat.voice_posts, [])

    def test_idempotent_resolve_does_not_resend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp, delivery="voice", voice_space="spaces/REPORTS",
                         tts=MockTTS())
            h.resolve()
            h.issue.report_written_at = h.issue.report_written_at or "2026-01-01T00:00:00Z"
            h.resolve()  # second pass must not re-deliver
            self.assertEqual(len(h.chat.voice_posts), 1)


if __name__ == "__main__":
    unittest.main()
