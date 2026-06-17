"""Tests for the Google Meet REST API integration (`gchat_agent.meet`).

Covers the live `MeetRestClient.create_space` transport (response parsing, the
socket timeout, retry-on-5xx, the single 401-reauth, hard-failure `RuntimeError`,
and the missing-`meetingUri` guard), the `build_meet` factory gate, and the
`FakeMeetClient` double + the `MeetClient` Protocol it satisfies.

Fully offline: `urllib.request.urlopen` and `chat.oauth` are stubbed; no network,
no Google credentials, no real sleeps.
"""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from dataclasses import replace
from email.message import Message as EmailMessage
from unittest import mock

from gchat_agent.config import load_config
from gchat_agent.meet import rest
from gchat_agent.meet.base import MeetClient, MeetSpace
from gchat_agent.meet.rest import MeetRestClient, build_meet
from tests.fakes import FakeMeetClient


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


def _http_error(code: int, body: bytes = b"{}", retry_after: str | None = None):
    """Build a `urllib.error.HTTPError` with a JSON body and optional header."""
    hdrs = EmailMessage()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://meet.googleapis.com/v2/spaces", code, "err", hdrs, io.BytesIO(body)
    )


def _cfg(**over):
    return replace(load_config(env_file="no-such.env"), **over)


def _client(**over) -> MeetRestClient:
    return MeetRestClient(_cfg(**over))


class CreateSpaceTest(unittest.TestCase):
    """`MeetRestClient.create_space` happy path + transport invariants."""

    def test_parses_response_and_passes_socket_timeout(self) -> None:
        captured: dict[str, object] = {}
        body = json.dumps({
            "name": "spaces/abc123",
            "meetingUri": "https://meet.google.com/abc-mnop-xyz",
            "meetingCode": "abc-mnop-xyz",
        }).encode()

        def fake_urlopen(req, timeout=None):  # noqa: ARG001 - req unused
            captured["timeout"] = timeout
            captured["method"] = req.get_method()
            captured["url"] = req.full_url
            return _FakeResponse(body)

        with mock.patch.object(rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            space = _client().create_space()

        self.assertIsInstance(space, MeetSpace)
        self.assertEqual(space.name, "spaces/abc123")
        self.assertEqual(space.meeting_uri, "https://meet.google.com/abc-mnop-xyz")
        self.assertEqual(space.meeting_code, "abc-mnop-xyz")
        self.assertEqual(captured["timeout"], rest._HTTP_TIMEOUT_SECONDS)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], "https://meet.googleapis.com/v2/spaces")

    def test_missing_meeting_uri_raises(self) -> None:
        body = json.dumps({"name": "spaces/x"}).encode()  # no meetingUri
        with mock.patch.object(rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(rest.urllib.request, "urlopen",
                                  side_effect=lambda req, timeout=None: _FakeResponse(body)):
            with self.assertRaises(RuntimeError) as ctx:
                _client().create_space()
        self.assertIn("meetingUri", str(ctx.exception))

    def test_custom_api_url_is_used(self) -> None:
        captured: dict[str, object] = {}
        body = json.dumps({"meetingUri": "https://meet.google.com/x"}).encode()

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            captured["url"] = req.full_url
            return _FakeResponse(body)

        client = MeetRestClient(_cfg(), api_url="https://meet.example/v9/")
        with mock.patch.object(rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            client.create_space()
        # trailing slash trimmed, path appended.
        self.assertEqual(captured["url"], "https://meet.example/v9/spaces")


class RetryAndAuthTest(unittest.TestCase):
    """Retry-on-5xx, the single 401-reauth, and hard-failure surfacing."""

    def test_retries_on_503_then_succeeds(self) -> None:
        ok = json.dumps({"meetingUri": "https://meet.google.com/x"}).encode()
        seq = [_http_error(503), _FakeResponse(ok)]

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(rest.time, "sleep"), \
                mock.patch.object(rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            space = _client().create_space()
        self.assertEqual(space.meeting_uri, "https://meet.google.com/x")
        self.assertEqual(seq, [])  # both queued responses consumed

    def test_401_triggers_single_reauth_then_retry(self) -> None:
        ok = json.dumps({"meetingUri": "https://meet.google.com/x"}).encode()
        seq = [_http_error(401), _FakeResponse(ok)]

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(rest.oauth, "invalidate") as inval, \
                mock.patch.object(rest.time, "sleep"), \
                mock.patch.object(rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            space = _client().create_space()
        self.assertEqual(space.meeting_uri, "https://meet.google.com/x")
        inval.assert_called_once()

    def test_403_no_scope_raises_runtimeerror(self) -> None:
        # PERMISSION_DENIED (e.g. token lacks meetings.space.created) is NOT
        # retryable, so it raises immediately — no backoff sleep on this path.
        body = json.dumps({"error": {"code": 403, "status": "PERMISSION_DENIED"}}).encode()
        with mock.patch.object(rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(rest.urllib.request, "urlopen",
                                  side_effect=lambda req, timeout=None: (_ for _ in ()).throw(_http_error(403, body))):
            with self.assertRaises(RuntimeError) as ctx:
                _client().create_space()
        self.assertIn("403", str(ctx.exception))

    def test_transport_error_retries_then_succeeds(self) -> None:
        ok = json.dumps({"meetingUri": "https://meet.google.com/x"}).encode()
        seq = [urllib.error.URLError("conn reset"), _FakeResponse(ok)]

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(rest.time, "sleep"), \
                mock.patch.object(rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            space = _client().create_space()
        self.assertEqual(space.meeting_uri, "https://meet.google.com/x")
        self.assertEqual(seq, [])

    def test_recurring_401_reauths_once_then_raises(self) -> None:
        # A SECOND 401 (after the one reauth) must not loop forever — it falls
        # through to a hard RuntimeError, and invalidate fires exactly once.
        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            raise _http_error(401)

        with mock.patch.object(rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(rest.oauth, "invalidate") as inval, \
                mock.patch.object(rest.time, "sleep"), \
                mock.patch.object(rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                _client().create_space()
        inval.assert_called_once()
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_401_on_last_attempt_still_retries(self) -> None:
        # The edge case the for-loop had wrong: transient retries exhaust the
        # budget, THEN a 401 arrives. The reauth must not be starved — it gets one
        # off-budget fresh-token retry, which here succeeds.
        ok = json.dumps({"meetingUri": "https://meet.google.com/x"}).encode()
        seq = [_http_error(503), _http_error(503), _http_error(503),
               _http_error(401), _FakeResponse(ok)]

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(rest.oauth, "invalidate") as inval, \
                mock.patch.object(rest.time, "sleep"), \
                mock.patch.object(rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            space = _client().create_space()
        self.assertEqual(space.meeting_uri, "https://meet.google.com/x")
        self.assertEqual(seq, [])  # all 5 responses consumed (4 retries + success)
        inval.assert_called_once()

    def test_resource_exhausted_403_is_retried(self) -> None:
        # A quota 403 carrying RESOURCE_EXHAUSTED IS transient (parity with the
        # Chat client), unlike a PERMISSION_DENIED 403.
        quota = json.dumps({"error": {"status": "RESOURCE_EXHAUSTED"}}).encode()
        ok = json.dumps({"meetingUri": "https://meet.google.com/x"}).encode()
        seq = [_http_error(403, quota), _FakeResponse(ok)]

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(rest.time, "sleep"), \
                mock.patch.object(rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            space = _client().create_space()
        self.assertEqual(space.meeting_uri, "https://meet.google.com/x")
        self.assertEqual(seq, [])

    def test_exhausts_retries_then_raises(self) -> None:
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            calls["n"] += 1
            raise _http_error(500)

        with mock.patch.object(rest.oauth, "get_access_token", return_value="tok"), \
                mock.patch.object(rest.time, "sleep"), \
                mock.patch.object(rest.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                _client().create_space()
        # Retries the full budget, then surfaces the last HTTP error.
        self.assertEqual(calls["n"], rest._MAX_RETRIES + 1)
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_retry_after_header_parsed(self) -> None:
        self.assertEqual(MeetRestClient._retry_after(_http_error(429, retry_after="7")), 7.0)
        self.assertIsNone(MeetRestClient._retry_after(_http_error(429)))
        self.assertIsNone(MeetRestClient._retry_after(_http_error(429, retry_after="soon")))


class BuildMeetTest(unittest.TestCase):
    """`build_meet` factory gating on `MEET_LINKS`."""

    def test_off_returns_none(self) -> None:
        self.assertIsNone(build_meet(_cfg(MEET_LINKS=False)))

    def test_on_returns_client_with_api_url(self) -> None:
        client = build_meet(_cfg(MEET_LINKS=True, MEET_API_URL="https://m.example/v2"))
        self.assertIsInstance(client, MeetRestClient)
        self.assertEqual(client.api_url, "https://m.example/v2")


class FakeMeetClientTest(unittest.TestCase):
    """The offline double satisfies the Protocol and is deterministic."""

    def test_satisfies_protocol(self) -> None:
        self.assertIsInstance(FakeMeetClient(), MeetClient)

    def test_mints_distinct_spaces_and_records(self) -> None:
        fake = FakeMeetClient()
        a = fake.create_space()
        b = fake.create_space()
        self.assertNotEqual(a.meeting_uri, b.meeting_uri)
        self.assertTrue(a.meeting_uri.startswith("https://meet.google.com/"))
        self.assertEqual(fake.created, [a, b])

    def test_fail_mode_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            FakeMeetClient(fail=True).create_space()


if __name__ == "__main__":
    unittest.main()
