#!/usr/bin/env python3
"""Mint a USER OAuth access token from your OWN Desktop OAuth client.

Why: gcloud's built-in client is blocked by Google from Chat scopes ("This app
is blocked"). A client you create in your own project, with yourself as a test
user, only triggers the "unverified app" warning (click Advanced -> Continue),
not a hard block.

Prereq (one-time, in the Google Cloud Console, project chat-smoke-...):
  1. Google Auth Platform -> "Get started": App name, your Gmail as support +
     developer email, Audience = External. (Publishing status stays "Testing".)
  2. Audience -> Test users -> add YOUR personal Gmail.
  3. Clients -> Create client -> type "Desktop app" -> download the JSON.

Then:
  python smoke/get_token.py --client ~/Downloads/client_secret_xxx.json
  # add --write to also request the chat.messages (send) scope

It opens your browser for ONE consent, captures the token via a localhost
redirect, writes it to smoke/.token, and prints the account + scopes so you can
confirm it is NOT a glo.com account.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
TOKENINFO = "https://oauth2.googleapis.com/tokeninfo?access_token="

READ_SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]
WRITE_SCOPES = [
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.create",
]

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, ".token")


def load_client(path: str) -> tuple[str, str]:
    with open(os.path.expanduser(path)) as f:
        data = json.load(f)
    node = data.get("installed") or data.get("web")
    if not node:
        sys.exit("client JSON has neither an 'installed' nor 'web' section — "
                 "did you create a 'Desktop app' OAuth client?")
    return node["client_id"], node["client_secret"]


class _Catcher(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        _Catcher.code = (params.get("code") or [None])[0]
        _Catcher.error = (params.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Authorization received — you can close this tab and return to the terminal."
        if _Catcher.error:
            msg = f"Authorization failed: {_Catcher.error}"
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *_):  # silence
        pass


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"token exchange failed (HTTP {e.code}): {e.read().decode('utf-8', 'replace')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client", required=True, help="path to the Desktop OAuth client_secret JSON")
    ap.add_argument("--write", action="store_true", help="also request chat.messages (send) scope")
    ap.add_argument("--account", default="", help="login_hint — pre-select this Google account in the consent screen")
    args = ap.parse_args()

    client_id, client_secret = load_client(args.client)
    scopes = list(READ_SCOPES) + (WRITE_SCOPES if args.write else [])

    port = free_port()
    redirect_uri = f"http://127.0.0.1:{port}/"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
    }
    if args.account:
        params["login_hint"] = args.account
    auth_url = AUTH_URI + "?" + urllib.parse.urlencode(params)

    server = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("Opening your browser to consent. If it doesn't open, paste this URL:\n")
    print(auth_url + "\n")
    print('At the "Google hasn\'t verified this app" screen: Advanced -> Continue.')
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # Block until the redirect hits our loopback server (handle_request returns).
    import time
    waited = 0
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
    if not access:
        sys.exit(f"no access_token in response: {tok}")

    # Confirm the account (refuse glo.com) via tokeninfo.
    try:
        with urllib.request.urlopen(TOKENINFO + urllib.parse.quote(access)) as r:
            info = json.loads(r.read() or "{}")
    except urllib.error.URLError:
        info = {}
    email = info.get("email")
    print(f"\n✅ Token minted. Account: {email or '(unknown)'}")
    print(f"   Scopes: {info.get('scope', '(unknown)')}")
    print(f"   Refresh token: {'yes' if tok.get('refresh_token') else 'no'}")
    if email and "glo.com" in email.lower():
        sys.exit("❌ This is a glo.com account — you asked not to use it. Re-run and pick your personal Gmail.")

    with open(TOKEN_FILE, "w") as f:
        f.write(access)
    print(f"\nWrote access token to {TOKEN_FILE}")
    print("Next:")
    print(f"  python smoke/smoke_test_chat.py --token \"$(cat {TOKEN_FILE})\" --quota-project chat-smoke-1781346315")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
