"""Regression tests for the live HTTP adapters' socket timeouts (review-driven).

These pin HIGH-1: every `urllib.request.urlopen` on the Google Chat / OAuth path
must pass an explicit `timeout=`, so a hung endpoint can't block a poll cycle (or
the token-mint flow) forever. We stub `urlopen` and assert the timeout kwarg is
forwarded; no network and no Google credentials are touched.

Stdlib `unittest`; offline.
"""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
