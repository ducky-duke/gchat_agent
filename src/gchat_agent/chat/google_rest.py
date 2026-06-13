"""Live Google Chat REST client (§5.4/§7) — the primary ingress/egress adapter.

Implements the `ChatClient` protocol over the Chat REST API using **stdlib
`urllib` only** (no `google-auth` / `google-api-python-client`). One instance is
bound to one account's token (the bot or a staff persona). The HTTP `call`
pattern (Bearer auth, JSON content-type, `x-goog-user-project`, `HTTPError`
handling) is ported from `smoke/smoke_test_chat.py`; the bearer comes from
`chat.oauth.get_access_token`.

Reads via `spaces.messages.list` with a double-quoted `createTime >` filter,
`orderBy=ASC`, `pageSize=1000`, following `nextPageToken` until empty. Posts via
`spaces.messages.create` as a threaded reply (`thread.name` +
`messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD`) with a stable
`requestId` for idempotency. Exponential backoff on `429 / RESOURCE_EXHAUSTED`.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final

from ..config import Config
from ..models import Message, SenderType
from . import oauth

_API_BASE: Final[str] = "https://chat.googleapis.com/v1"

# Backoff on transient 429 / 5xx (RESOURCE_EXHAUSTED): base * 2**attempt.
_MAX_RETRIES: Final[int] = 5
_BACKOFF_BASE_SECONDS: Final[float] = 0.5
_BACKOFF_CAP_SECONDS: Final[float] = 30.0
_RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


class GoogleChatClient:
    """`ChatClient` adapter for the live Google Chat REST API (user OAuth)."""

    def __init__(
        self,
        config: Config,
        token_file: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self.config = config
        self.space = config.GOOGLE_SPACE
        self.client_json = config.GOOGLE_OAUTH_CLIENT
        self.token_file = token_file or config.GOOGLE_TOKEN_FILE
        self.quota_project = config.GOOGLE_QUOTA_PROJECT or None
        # Own users/<id> for self-filtering (§5.7/§6). Seeded from persisted
        # state when known (so it survives a restart before the bot posts), else
        # learned lazily from the first posted message's sender.name.
        self._me: str | None = user_id

    # --- auth / HTTP -------------------------------------------------------
    def _token(self) -> str:
        return oauth.get_access_token(
            self.client_json, self.token_file, self.quota_project
        )

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """One HTTP call to the Chat API with retries on transient failures.

        Mirrors `smoke/smoke_test_chat.py`'s `call()`: Bearer auth, JSON
        content-type, optional `x-goog-user-project`, and `HTTPError` decoding.
        Retries (with exponential backoff) on 429/5xx; raises `RuntimeError` on
        a non-retryable HTTP error or after exhausting retries.
        """
        url = f"{_API_BASE}{path}"
        data = json.dumps(body).encode() if body is not None else None
        last_status = 0
        last_payload: dict[str, Any] = {}
        reauthed = False
        for attempt in range(_MAX_RETRIES + 1):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self._token()}")
            req.add_header("Content-Type", "application/json")
            if self.quota_project:
                req.add_header("x-goog-user-project", self.quota_project)
            try:
                with urllib.request.urlopen(req) as resp:
                    raw = resp.read()
                    if not raw:
                        return resp.status, {}
                    try:
                        return resp.status, json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"Chat API {method} {path} returned non-JSON "
                            f"(HTTP {resp.status}): {raw[:400]!r}"
                        ) from exc
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    payload = {"_raw": raw.decode("utf-8", "replace")}
                last_status, last_payload = exc.code, payload
                # A cached token may have been revoked early / its expiry math
                # skewed: drop it and retry once with a freshly minted token.
                if exc.code == 401 and not reauthed:
                    reauthed = True
                    oauth.invalidate(self.client_json, self.token_file)
                    continue
                if self._should_retry(exc.code, payload) and attempt < _MAX_RETRIES:
                    self._sleep_backoff(attempt)
                    continue
                raise RuntimeError(
                    f"Chat API {method} {path} failed (HTTP {exc.code}): "
                    f"{json.dumps(payload)[:800]}"
                ) from exc
            except urllib.error.URLError as exc:
                # Transport-level hiccup — treat like a transient failure.
                if attempt < _MAX_RETRIES:
                    self._sleep_backoff(attempt)
                    continue
                raise RuntimeError(
                    f"Chat API {method} {path} unreachable: {exc}"
                ) from exc
        raise RuntimeError(
            f"Chat API {method} {path} failed after retries "
            f"(HTTP {last_status}): {json.dumps(last_payload)[:800]}"
        )

    @staticmethod
    def _should_retry(status: int, payload: dict[str, Any]) -> bool:
        if status in _RETRY_STATUSES:
            return True
        err = payload.get("error", {}) or {}
        status_str = (err.get("status", "") or "").upper()
        return status_str == "RESOURCE_EXHAUSTED"

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        delay = min(_BACKOFF_BASE_SECONDS * (2 ** attempt), _BACKOFF_CAP_SECONDS)
        time.sleep(delay)

    # --- mapping -----------------------------------------------------------
    def _to_message(self, raw: dict[str, Any]) -> Message:
        """Map a Chat API Message resource onto the domain `Message`."""
        sender = raw.get("sender", {}) or {}
        gtype = (sender.get("type", "") or "").upper()
        # Chat User.type is HUMAN / BOT; the foundation enum is human / app.
        sender_type = SenderType.APP if gtype == "BOT" else SenderType.HUMAN
        thread = raw.get("thread", {}) or {}
        space = raw.get("space", {}) or {}
        return Message(
            id=raw.get("name", ""),
            space=space.get("name", "") or self.space,
            thread_id=thread.get("name", ""),
            sender=sender.get("name", ""),
            sender_type=sender_type,
            text=raw.get("text", ""),
            create_time=raw.get("createTime", ""),
        )

    def _require_space(self) -> str:
        if not self.space:
            raise RuntimeError(
                "GOOGLE_SPACE is not set — cannot read/post without a space"
            )
        return self.space

    # --- ChatClient protocol ----------------------------------------------
    def me(self) -> str | None:
        """Own `users/<id>` resource name, learned from the first posted
        message's `sender.name` (cached). `None` until a post has happened."""
        return self._me

    def fetch_messages(self, since: str | None) -> list[Message]:
        """List messages created after `since` (RFC-3339), oldest-first, fully
        paginated. `None` ⇒ no `createTime` filter (all messages the account can
        see in the space)."""
        space = self._require_space()
        params: dict[str, str] = {"pageSize": "1000", "orderBy": "ASC"}
        if since:
            # RFC-3339 timestamp must be DOUBLE-QUOTED inside the filter (§7).
            params["filter"] = f'createTime > "{since}"'

        messages: list[Message] = []
        page_token: str | None = None
        while True:
            query = dict(params)
            if page_token:
                query["pageToken"] = page_token
            path = f"/{space}/messages?{urllib.parse.urlencode(query)}"
            _status, payload = self._call("GET", path)
            for raw in payload.get("messages", []) or []:
                messages.append(self._to_message(raw))
            page_token = payload.get("nextPageToken") or None
            if not page_token:
                break
        return messages

    def post_message(
        self,
        text: str,
        thread_id: str | None = None,
        request_id: str | None = None,
    ) -> Message:
        """Create a message, optionally in an existing thread (replying with
        fallback-to-new-thread). Idempotent on `request_id`."""
        space = self._require_space()
        body: dict[str, Any] = {"text": text}
        query: dict[str, str] = {}
        if thread_id:
            body["thread"] = {"name": thread_id}
            query["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        query["requestId"] = request_id or self._default_request_id(text, thread_id)

        path = f"/{space}/messages?{urllib.parse.urlencode(query)}"
        _status, payload = self._call("POST", path, body)
        created = self._to_message(payload)
        self._learn_self(payload)
        return created

    def post_reply(
        self,
        message: Message,
        text: str,
        request_id: str | None = None,
    ) -> Message:
        """Reply to `message` in its thread (idempotent on `request_id`)."""
        return self.post_message(
            text, thread_id=message.thread_id, request_id=request_id
        )

    # --- helpers -----------------------------------------------------------
    def _learn_self(self, raw: dict[str, Any]) -> None:
        if self._me is None:
            sender = raw.get("sender", {}) or {}
            name = sender.get("name")
            if name:
                self._me = name

    @staticmethod
    def _default_request_id(text: str, thread_id: str | None) -> str:
        """Stable fallback idempotency key derived from the payload, so a retry
        of the *same* post never double-creates even when the caller omits one.
        Callers normally pass an explicit `client-issue-{id}-r{n}` (§5.4)."""
        import hashlib

        raw = "\x1f".join((thread_id or "", text))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"client-{digest}"
