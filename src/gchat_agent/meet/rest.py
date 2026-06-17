"""Live Google Meet REST API client over **stdlib `urllib` only** (§ Meet REST).

Mirrors `chat.google_rest` / `github.rest`: a Bearer-authed JSON `urllib` request
with bounded retry/backoff on 429/5xx, a single 401-reauth, and a clean
`RuntimeError` on a hard failure. One instance is bound to one account's
**user-OAuth** token — the same refresh-token flow as the Chat client, via
`chat.oauth` (so the bot, or a staff persona, can mint a meeting under their own
identity with no service account).

The only call we make is `spaces.create` (`POST .../v2/spaces`): mint a meeting
space and return its `meetingUri` join link. The OAuth token MUST carry the
`https://www.googleapis.com/auth/meetings.space.created` scope (added to
`scripts/authorize.py`); a refresh token minted before that scope existed gets a
403 on create until it is re-authorized.

Read the bundled reference at `docs/google_meet/` before changing endpoints. The
Meet *Media* API (live audio) is receive-only + preview-gated, so this REST path —
create + share a join link — is the achievable integration.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any, Final

from ..chat import oauth
from ..config import Config
from .base import MeetSpace

# Default v2 resource base. Overridable via `MEET_API_URL` for testing/proxies.
_API_BASE: Final[str] = "https://meet.googleapis.com/v2"

# Per-request socket timeout (connect + read) so a hung endpoint can't wedge a
# call forever. A read timeout raises `TimeoutError` (NOT a `URLError` subclass),
# handled explicitly below as a transient failure.
_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

# Backoff on transient 429 / 5xx: base * 2**attempt, capped, with full jitter.
_MAX_RETRIES: Final[int] = 3
_BACKOFF_BASE_SECONDS: Final[float] = 0.5
_BACKOFF_CAP_SECONDS: Final[float] = 30.0
_RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


class MeetRestClient:
    """`MeetClient` adapter for the live Google Meet REST API (user OAuth)."""

    def __init__(
        self,
        config: Config,
        token_file: str | None = None,
        api_url: str = _API_BASE,
    ) -> None:
        self.config = config
        self.client_json = config.GOOGLE_OAUTH_CLIENT
        self.token_file = token_file or config.GOOGLE_TOKEN_FILE
        self.quota_project = config.GOOGLE_QUOTA_PROJECT or None
        self.api_url = (api_url or _API_BASE).rstrip("/")

    # --- auth / HTTP -------------------------------------------------------
    def _token(self) -> str:
        return oauth.get_access_token(
            self.client_json, self.token_file, self.quota_project
        )

    # --- MeetClient protocol ----------------------------------------------
    def create_space(self) -> MeetSpace:
        """Create a meeting space and return a `MeetSpace` with its join link.

        `spaces.create` takes an (optional) `Space` body; we send `{}` and let
        Meet generate the access type / code. Raises `RuntimeError` if the
        response carries no `meetingUri` (the join link is the whole point)."""
        payload = self._post("/spaces", {})
        uri = str(payload.get("meetingUri", "") or "")
        if not uri:
            raise RuntimeError(
                "Meet spaces.create returned no meetingUri: "
                f"{json.dumps(payload)[:400]}"
            )
        return MeetSpace(
            name=str(payload.get("name", "") or ""),
            meeting_uri=uri,
            meeting_code=str(payload.get("meetingCode", "") or ""),
            raw=payload,
        )

    # --- transport ---------------------------------------------------------
    def _post(self, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """POST `body` (JSON) to `{api_url}{path}` with retries + a single
        401-reauth. Raises `RuntimeError` on a non-retryable HTTP error or after
        exhausting retries.

        `attempt` counts only *transient* retries (429/5xx/timeout). A 401-reauth
        refreshes the token and retries WITHOUT consuming that budget — so a 401
        on what would be the last attempt still gets one fresh-token retry instead
        of falling through un-retried (a `for attempt` loop had that edge bug)."""
        url = f"{self.api_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        attempt = 0
        reauthed = False
        while True:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Authorization", f"Bearer {self._token()}")
            req.add_header("Content-Type", "application/json")
            if self.quota_project:
                req.add_header("x-goog-user-project", self.quota_project)
            try:
                with urllib.request.urlopen(
                    req, timeout=_HTTP_TIMEOUT_SECONDS
                ) as resp:
                    raw = resp.read()
                    if not raw:
                        return {}
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"Meet API POST {path} returned non-JSON "
                            f"(HTTP {resp.status}): {raw[:400]!r}"
                        ) from exc
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    parsed = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    parsed = {"_raw": raw.decode("utf-8", "replace")}
                # A cached token may have been revoked early / its expiry skewed:
                # drop it and retry ONCE with a freshly minted token (off-budget).
                if exc.code == 401 and not reauthed:
                    reauthed = True
                    oauth.invalidate(self.client_json, self.token_file)
                    continue
                if self._should_retry(exc.code, parsed) and attempt < _MAX_RETRIES:
                    self._sleep_backoff(attempt, self._retry_after(exc))
                    attempt += 1
                    continue
                raise RuntimeError(
                    f"Meet API POST {path} failed (HTTP {exc.code}): "
                    f"{json.dumps(parsed)[:600]}"
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < _MAX_RETRIES:
                    self._sleep_backoff(attempt)
                    attempt += 1
                    continue
                raise RuntimeError(f"Meet API unreachable: {exc}") from exc

    @staticmethod
    def _should_retry(status: int, payload: dict[str, Any]) -> bool:
        """Whether an HTTP error is transient. The status family, plus a
        `RESOURCE_EXHAUSTED` quota error that Google may surface as a 403 body
        (parity with `chat.google_rest._should_retry`)."""
        if status in _RETRY_STATUSES:
            return True
        err = payload.get("error", {}) or {}
        return (err.get("status", "") or "").upper() == "RESOURCE_EXHAUSTED"

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError) -> float | None:
        """Seconds from a `Retry-After` response header (delta-seconds form only),
        or ``None``. Mirrors `chat.google_rest._retry_after`."""
        headers = getattr(exc, "headers", None)
        get = getattr(headers, "get", None)
        if not callable(get):
            return None
        raw = get("Retry-After")
        if raw is None:
            return None
        try:
            secs = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return secs if secs >= 0 else None

    @staticmethod
    def _sleep_backoff(attempt: int, retry_after: float | None = None) -> None:
        """Sleep before a retry. Honor a server `Retry-After` (clamped to the cap),
        else exponential backoff with full jitter — a uniform pick in
        ``[0, min(cap, base*2**attempt)]`` — so concurrent retries don't thunder."""
        if retry_after is not None:
            time.sleep(min(retry_after, _BACKOFF_CAP_SECONDS))
            return
        ceiling = min(_BACKOFF_BASE_SECONDS * (2 ** attempt), _BACKOFF_CAP_SECONDS)
        time.sleep(random.uniform(0, ceiling))


def build_meet(config: Config) -> "MeetRestClient | None":
    """Wire a `MeetRestClient` from `config`, or `None` when Meet links are off.

    Gated by `MEET_LINKS` (off by default, so the offline/test path needs no Meet
    API). The token validity (and the `meetings.space.created` scope) is checked
    lazily at `create_space` time, not here — exactly like `build_github` defers
    the token check — so a misconfigured demo still loads and degrades gracefully.
    """
    if not config.MEET_LINKS:
        return None
    return MeetRestClient(config, api_url=config.MEET_API_URL)
