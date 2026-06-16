"""Regression tests for the live HTTP adapters' socket timeouts (review-driven).

These pin HIGH-1: every `urllib.request.urlopen` on the Google Chat / OAuth path
must pass an explicit `timeout=`, so a hung endpoint can't block a poll cycle (or
the token-mint flow) forever. We stub `urlopen` and assert the timeout kwarg is
forwarded; no network and no Google credentials are touched.

Stdlib `unittest`; offline.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest import mock

from gchat_agent.chat import google_rest, oauth
from gchat_agent.chat.google_rest import GoogleChatClient
from gchat_agent.config import load_config


class _FakeResponse:
    """Minimal `urlopen`-style context manager returning a fixed body."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class ChatTimeoutTest(unittest.TestCase):
    """`GoogleChatClient` HTTP calls pass the module socket timeout."""

    def test_fetch_messages_passes_socket_timeout(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(req, timeout=None):  # noqa: ARG001 - req unused
            captured["timeout"] = timeout
            return _FakeResponse(b'{"messages": []}')

        cfg = replace(
            load_config(env_file="no-such.env"),
            GOOGLE_SPACE="spaces/x",
        )
        client = GoogleChatClient(cfg, token_file="unused.json")

        with mock.patch.object(google_rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(google_rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = client.fetch_messages(None)

        self.assertEqual(out, [])
        self.assertEqual(captured["timeout"], google_rest._HTTP_TIMEOUT_SECONDS)
        self.assertGreater(google_rest._HTTP_TIMEOUT_SECONDS, 0)


class OAuthTimeoutTest(unittest.TestCase):
    """The token-exchange POST passes the module socket timeout."""

    def test_post_form_passes_socket_timeout(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(req, timeout=None):  # noqa: ARG001 - req unused
            captured["timeout"] = timeout
            return _FakeResponse(b'{"access_token": "a", "expires_in": 3600}')

        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = oauth._post_form("https://example/token", {"grant_type": "x"})

        self.assertEqual(out["access_token"], "a")
        self.assertEqual(captured["timeout"], oauth._HTTP_TIMEOUT_SECONDS)
        self.assertGreater(oauth._HTTP_TIMEOUT_SECONDS, 0)


class PostVoiceTest(unittest.TestCase):
    """`post_voice` uploads the audio (multipart) then creates a message that
    references the returned `attachmentDataRef` — both on the bot's user token."""

    def _client(self):
        cfg = replace(load_config(env_file="no-such.env"), GOOGLE_SPACE="spaces/MAIN")
        return GoogleChatClient(cfg, token_file="unused.json"), cfg

    def test_upload_then_create_with_attachment(self) -> None:
        calls: list[dict] = []

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            url = req.full_url
            calls.append({
                "url": url,
                "data": req.data,
                "content_type": req.get_header("Content-type"),
            })
            if "attachments:upload" in url:
                return _FakeResponse(
                    b'{"attachmentDataRef": {"attachmentUploadToken": "TOK-123"}}'
                )
            return _FakeResponse(
                b'{"name": "spaces/REPORTS/messages/m9", '
                b'"sender": {"name": "users/bot", "type": "HUMAN"}, '
                b'"thread": {"name": "spaces/REPORTS/threads/t9"}, '
                b'"space": {"name": "spaces/REPORTS"}, '
                b'"text": "cap", "createTime": "2026-06-15T00:00:00Z"}'
            )

        client, _cfg = self._client()
        with mock.patch.object(google_rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(google_rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            msg = client.post_voice(
                b"ID3-fake-audio-bytes",
                filename="issue-x.mp3",
                text="cap",
                space="spaces/REPORTS",
                request_id="client-issue-x-voice",
            )

        self.assertEqual(msg.id, "spaces/REPORTS/messages/m9")
        self.assertEqual(len(calls), 2)

        upload, create = calls
        # Upload: distinct host/path, multipart body carrying filename + bytes.
        self.assertIn("/upload/v1/spaces/REPORTS/attachments:upload", upload["url"])
        self.assertIn("uploadType=multipart", upload["url"])
        self.assertTrue(upload["content_type"].startswith("multipart/related"))
        self.assertIn(b'"filename": "issue-x.mp3"', upload["data"])
        self.assertIn(b"ID3-fake-audio-bytes", upload["data"])

        # Create: JSON message on /v1, attaching the uploaded token.
        self.assertIn("/v1/spaces/REPORTS/messages", create["url"])
        body = json.loads(create["data"])
        self.assertEqual(body["text"], "cap")
        self.assertEqual(
            body["attachment"][0]["attachmentDataRef"]["attachmentUploadToken"],
            "TOK-123",
        )

    def test_upload_without_ref_raises(self) -> None:
        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            return _FakeResponse(b'{"nope": true}')

        client, _cfg = self._client()
        with mock.patch.object(google_rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(google_rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError):
                client._upload_attachment("spaces/REPORTS", "f.mp3", b"x")


if __name__ == "__main__":
    unittest.main()
