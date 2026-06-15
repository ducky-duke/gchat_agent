#!/usr/bin/env python3
"""One-time-per-account OAuth loopback mint flow (§5.4/§5.7/§7).

The build version of ``smoke/get_token.py``: run the loopback consent against
your OWN Desktop OAuth client, exchange the authorization code, and save the
account's long-lived **refresh token** as a per-account JSON in the shape
``gchat_agent.chat.oauth`` expects::

    {"refresh_token": "...", "token_uri": "https://oauth2.googleapis.com/token",
     "client_id": "..."}

The bot and each staff persona run this once (``--account <gmail>`` picks the
account, ``--out`` the token path). At runtime ``oauth.get_access_token`` swaps
that refresh token for short-lived bearers — this script never runs again until
the refresh token is revoked / expires (Testing-mode tokens lapse after 7 days).

Why a self-made client: gcloud's built-in client is hard-blocked from Chat
scopes. A client in your own project (you as a test user) only triggers the
"unverified app" warning (Advanced -> Continue), not a block. Refuses a glo.com
account, like ``smoke/get_token.py``.

stdlib ``urllib``/``http.server`` only — no ``google-auth``.

    PYTHONPATH=src python scripts/authorize.py --account you@gmail.com \
        --client secrets/oauth_client.json --out secrets/token_bot.json
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Resolve config defaults for --client / --out without importing at module load
# failing if src/ isn't on the path (keeps --help cheap and import-light).
try:
    from gchat_agent.config import load_config

    _CFG = load_config()
    _DEFAULT_CLIENT = _CFG.GOOGLE_OAUTH_CLIENT
    _DEFAULT_OUT = _CFG.GOOGLE_TOKEN_FILE
except Exception:  # pragma: no cover - fallback mirrors config.py §10 defaults
    _DEFAULT_CLIENT = "secrets/oauth_client.json"
    _DEFAULT_OUT = "secrets/token_bot.json"

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
TOKENINFO = "https://oauth2.googleapis.com/tokeninfo?access_token="

# Per-request socket timeout so a hung endpoint can't wedge the mint flow forever.
HTTP_TIMEOUT_SECONDS = 30.0

# v1 demo scopes (§5.4): read + write Chat messages, create the demo space, and
# userinfo.email so we can confirm (and refuse) the consenting account.
SCOPES = [
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.spaces.create",
    "https://www.googleapis.com/auth/userinfo.email",
]


def load_client(path: str) -> tuple[str, str]:
    """Load (client_id, client_secret) from a Desktop OAuth client JSON."""
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        data = json.load(fh)
    node = data.get("installed") or data.get("web")
    if not node:
        sys.exit(
            f"client JSON {path!r} has neither an 'installed' nor 'web' section "
            "— did you create a 'Desktop app' OAuth client?"
        )
    client_id = node.get("client_id")
    client_secret = node.get("client_secret")
    if not client_id or not client_secret:
        sys.exit(f"client JSON {path!r} is missing client_id/client_secret")
    return client_id, client_secret


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Captures the ?code= / ?error= the consent redirect hands our loopback."""

    code: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802 - http.server API
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        _Catcher.code = (params.get("code") or [None])[0]
        _Catcher.error = (params.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Authorization received — close this tab and return to the terminal."
        if _Catcher.error:
            msg = f"Authorization failed: {_Catcher.error}"
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *_):  # noqa: N802 - silence the default access log
        pass


def free_port() -> int:
    """Grab an ephemeral loopback port for the redirect URI."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def post_form(url: str, fields: dict[str, str]) -> dict:
    """POST a urlencoded form and parse the JSON response."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(
            f"token exchange failed (HTTP {e.code}): "
            f"{e.read().decode('utf-8', 'replace')}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Mint a per-account Google Chat refresh token via OAuth loopback.",
    )
    ap.add_argument(
        "--client",
        default=_DEFAULT_CLIENT,
        help="Desktop OAuth client_secret JSON (default: %(default)s)",
    )
    ap.add_argument(
        "--out",
        default=_DEFAULT_OUT,
        help="where to write the refresh-token JSON (default: %(default)s)",
    )
    ap.add_argument(
        "--account",
        default="",
        help="login_hint — pre-select this Google account in the consent screen",
    )
    args = ap.parse_args(argv)

    client_id, client_secret = load_client(args.client)

    port = free_port()
    redirect_uri = f"http://127.0.0.1:{port}/"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",   # request a refresh_token
        "prompt": "consent",        # force a fresh refresh_token every run
    }
    if args.account:
        params["login_hint"] = args.account
    auth_url = AUTH_URI + "?" + urllib.parse.urlencode(params)

    server = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("Opening your browser to consent. If it doesn't open, paste this URL:\n")
    print(auth_url + "\n")
    print('At the "Google hasn\'t verified this app" screen: Advanced -> Continue.')
    # webbrowser is imported lazily so --help never tries to launch a browser.
    try:
        import webbrowser

        webbrowser.open(auth_url)
    except Exception:
        pass

    # Block until the redirect hits our loopback server (5-min ceiling).
    waited = 0.0
    while _Catcher.code is None and _Catcher.error is None and waited < 300:
        time.sleep(0.5)
        waited += 0.5
    if _Catcher.error:
        sys.exit(f"\nConsent failed: {_Catcher.error}")
    if _Catcher.code is None:
        sys.exit("\nTimed out waiting for consent (5 min).")

    tok = post_form(TOKEN_URI, {
        "code": _Catcher.code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    access = tok.get("access_token")
    refresh = tok.get("refresh_token")
    if not access:
        sys.exit(f"no access_token in response: {tok}")

    # Confirm the account (refuse glo.com) via tokeninfo.
    info: dict = {}
    try:
        with urllib.request.urlopen(
            TOKENINFO + urllib.parse.quote(access), timeout=HTTP_TIMEOUT_SECONDS
        ) as r:
            info = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, TimeoutError):
        info = {}
    email = info.get("email")
    print(f"\nToken minted. Account: {email or '(unknown)'}")
    print(f"  Scopes: {info.get('scope', '(unknown)')}")
    print(f"  Refresh token: {'yes' if refresh else 'no'}")
    if email and "glo.com" in email.lower():
        sys.exit(
            "This is a glo.com account — you asked not to use it. Re-run and "
            "pick your personal Gmail."
        )
    if not refresh:
        sys.exit(
            "No refresh_token returned (Google omits it when one was already "
            "issued for this client+account). Revoke the app's access at "
            "https://myaccount.google.com/permissions and re-run."
        )

    # Save the per-account token JSON in the shape gchat_agent.chat.oauth wants.
    token_record = {
        "refresh_token": refresh,
        "token_uri": TOKEN_URI,
        "client_id": client_id,
    }
    out_path = os.path.expanduser(args.out)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(token_record, fh, indent=2)
        fh.write("\n")
    try:
        os.chmod(out_path, 0o600)  # secrets file — owner-only
    except OSError:
        pass
    print(f"\nWrote refresh-token JSON to {out_path}")
    print("Next: set GOOGLE_TOKEN_FILE to this path (or pass it to the runner).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
