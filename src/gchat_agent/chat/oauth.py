"""Minimal stdlib user-OAuth token refresh (§5.4/§7).

The live Chat client needs a short-lived bearer access token. Each account has a
long-lived **refresh token** (minted once via the loopback `authorize` flow — a
port of `smoke/get_token.py`, which is a separate script, NOT this module). Here
we only exchange that refresh token for an access token via the standard
``grant_type=refresh_token`` POST, and cache the result until shortly before it
expires.

stdlib `urllib` only — no `google-auth` / `google-api-python-client`. The token
exchange mirrors `smoke/get_token.py`'s `load_client` + `post_form`.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Final

# Fallback token endpoint when the client/token JSON omits an explicit one.
_DEFAULT_TOKEN_URI: Final[str] = "https://oauth2.googleapis.com/token"

# Refresh a little early so an in-flight request never races the expiry.
_EXPIRY_SKEW_SECONDS: Final[int] = 60

# Per-request socket timeout (connect + read) for the token exchange, so a hung
# token endpoint can't block a poll cycle forever. A read timeout raises
# `TimeoutError` (not a `URLError` subclass), surfaced as a transport error.
_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0


def _load_client(path: str) -> tuple[str, str, str]:
    """Load (client_id, client_secret, token_uri) from a Desktop OAuth client
    JSON (the ``installed`` or ``web`` node, like `smoke/get_token.py`)."""
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        data = json.load(fh)
    node = data.get("installed") or data.get("web")
    if not node:
        raise RuntimeError(
            f"client JSON {path!r} has neither an 'installed' nor 'web' section "
            "— it must be a 'Desktop app' OAuth client"
        )
    client_id = node.get("client_id")
    client_secret = node.get("client_secret")
    if not client_id or not client_secret:
        raise RuntimeError(
            f"client JSON {path!r} is missing client_id/client_secret"
        )
    token_uri = node.get("token_uri") or _DEFAULT_TOKEN_URI
    return client_id, client_secret, token_uri


def _load_refresh_token(path: str) -> tuple[str, str | None]:
    """Load (refresh_token, token_uri|None) from a per-account token JSON.

    Accepts either a bare ``{"refresh_token": ...}`` blob or the richer shape
    written by Google's OAuth flow (which may also carry its own ``token_uri``).
    """
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise RuntimeError(f"token JSON {path!r} is not an object")
    refresh = data.get("refresh_token")
    if not refresh:
        raise RuntimeError(
            f"token JSON {path!r} has no 'refresh_token' — mint one with the "
            "authorize flow (scripts/authorize.py)"
        )
    return refresh, data.get("token_uri")


def _post_form(url: str, fields: dict[str, str]) -> dict:
    """POST a urlencoded form and parse the JSON response (like
    `smoke/get_token.py`'s `post_form`)."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"token endpoint returned non-JSON: {raw[:200]!r}"
                ) from exc
    except urllib.error.HTTPError as exc:
        # Bound the echoed body: a hostile/verbose endpoint shouldn't be able to
        # flood logs, and the OAuth error JSON we care about is short.
        body = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(
            f"token refresh failed (HTTP {exc.code}): {body}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"token endpoint unreachable: {exc}") from exc


# --- token cache, keyed by (client_json, token_json) ------------------------
# Cache by absolute paths so multiple GoogleChatClient instances bound to the
# same account share one refreshed access token. Guarded by a lock so concurrent
# refreshes (staff + bot in one process) don't stampede the token endpoint.
_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_CACHE_LOCK = threading.Lock()


def _refresh(client_json: str, token_json: str) -> tuple[str, float]:
    """Run the refresh_token grant once and return (access_token, expiry_epoch)."""
    client_id, client_secret, client_token_uri = _load_client(client_json)
    refresh_token, tok_token_uri = _load_refresh_token(token_json)
    token_uri = tok_token_uri or client_token_uri or _DEFAULT_TOKEN_URI

    payload = _post_form(token_uri, {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    access = payload.get("access_token")
    if not access:
        raise RuntimeError(f"no access_token in refresh response: {payload}")
    try:
        expires_in = int(payload.get("expires_in", 3600) or 3600)
    except (TypeError, ValueError):
        expires_in = 3600  # conservative default if the server omits/garbles it
    expiry = time.time() + max(0, expires_in - _EXPIRY_SKEW_SECONDS)
    return access, expiry


def get_access_token(
    client_json: str,
    token_json: str,
    quota_project: str | None = None,
) -> str:
    """Return a valid bearer access token for the account whose refresh token is
    in ``token_json``, using the Desktop client in ``client_json``.

    Refreshes via the ``grant_type=refresh_token`` POST and caches the access
    token until shortly before its expiry, so back-to-back API calls reuse one
    token. ``quota_project`` is accepted for signature parity with the live HTTP
    layer (it is applied as the ``x-goog-user-project`` header on API calls, not
    the token exchange) — kept here so callers thread one config through.
    """
    key = (os.path.abspath(os.path.expanduser(client_json)),
           os.path.abspath(os.path.expanduser(token_json)))
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
        access, expiry = _refresh(client_json, token_json)
        _CACHE[key] = (access, expiry)
        return access


def invalidate(client_json: str, token_json: str) -> None:
    """Drop the cached access token for this account so the next
    `get_access_token` forces a fresh `refresh_token` grant. Called on a 401
    (token revoked early, or clock-skew expiry math left a stale token cached)."""
    key = (os.path.abspath(os.path.expanduser(client_json)),
           os.path.abspath(os.path.expanduser(token_json)))
    with _CACHE_LOCK:
        _CACHE.pop(key, None)
