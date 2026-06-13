#!/usr/bin/env python3
"""Smoke test — can a CONSUMER (@gmail.com) Google account drive the Google Chat
REST API via *user* OAuth?

This answers the one open question blocking the "3 personal accounts in one Space"
design: the docs conflict (API reference implies consumer accounts work; a guide
page says a Workspace account is required). We settle it empirically.

Decisive test  : GET /v1/spaces  (scope chat.spaces.readonly) — returns 200 even
                 for a brand-new account with zero spaces, which already proves the
                 API accepts a consumer user token.
Full-loop test : with --space spaces/XXXX it also creates + lists a message
                 (needs scope chat.messages) to prove the real read/write path.

Token sources (first that is set wins):
  1. --token <ya29...>
  2. env GOOGLE_OAUTH_TOKEN   (e.g. pasted from the OAuth 2.0 Playground)
  3. `gcloud auth application-default print-access-token`  (gcloud ADC)

Nothing here touches glo.com — you sign in with your personal Gmail in step (3)
of the runbook (see smoke/README.md).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://chat.googleapis.com/v1"


def get_token(arg_token: str | None) -> str:
    import os

    if arg_token:
        return arg_token.strip()
    env = os.environ.get("GOOGLE_OAUTH_TOKEN")
    if env:
        return env.strip()
    try:
        out = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            check=True,
        )
        tok = out.stdout.strip()
        if tok:
            return tok
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        sys.exit(
            "No access token available.\n"
            "  Provide one of:\n"
            "    --token ya29....\n"
            "    GOOGLE_OAUTH_TOKEN=ya29.... python smoke_test_chat.py\n"
            "    gcloud auth application-default login --scopes=...\n"
            f"  (gcloud ADC lookup failed: {detail.strip()})"
        )
    sys.exit("gcloud returned an empty token — run the ADC login again.")


def account_of(token: str) -> tuple[str | None, str]:
    """Ask Google's tokeninfo endpoint who this token belongs to (needs the
    userinfo.email scope to reveal the email) and what scopes it carries."""
    url = "https://oauth2.googleapis.com/tokeninfo?access_token=" + urllib.parse.quote(token)
    try:
        with urllib.request.urlopen(url) as r:
            info = json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return None, f"(tokeninfo HTTP {e.code})"
    except urllib.error.URLError as e:
        return None, f"(tokeninfo unreachable: {e})"
    return info.get("email"), info.get("scope", "")


def guard_account(token: str) -> None:
    email, scope = account_of(token)
    print(f"Token account: {email or '(email scope not granted — cannot confirm)'}")
    print(f"Token scopes : {scope or '(unknown)'}")
    if email and "glo.com" in email.lower():
        sys.exit(
            "\n❌ This token belongs to a glo.com account, which you asked NOT to use.\n"
            "   Re-run `gcloud auth application-default login` and pick your PERSONAL Gmail."
        )


def call(method: str, path: str, token: str, quota_project: str | None, body=None):
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    if quota_project:
        req.add_header("x-goog-user-project", quota_project)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"_raw": raw.decode("utf-8", "replace")}
        return e.code, payload


def classify(status: int, payload: dict) -> str:
    if status == 200:
        return "PASS"
    err = payload.get("error", {}) or {}
    text = (err.get("message", "") or "").lower()
    if "chat app not found" in text or "configure the app" in text:
        return "APP_NOT_CONFIGURED"
    if "has not been used in project" in text or "it is disabled" in text:
        return "API_NOT_ENABLED"
    if "insufficient" in text and "scope" in text:
        return "SCOPE"
    if any(k in text for k in ("workspace", "consumer", "administrator", "domain", "not available")):
        return "CONSUMER_BLOCKED"
    return "OTHER"


def show(title: str, status: int, payload: dict) -> str:
    verdict = classify(status, payload)
    print(f"\n=== {title} ===")
    print(f"HTTP {status}  ->  {verdict}")
    print(json.dumps(payload, indent=2)[:1800])
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--token", help="OAuth access token (overrides env / gcloud ADC)")
    ap.add_argument("--quota-project", help="GCP project id with Chat API enabled (sets x-goog-user-project)")
    ap.add_argument("--space", help="spaces/XXXX — also run the write+read full-loop test")
    ap.add_argument("--create-space", action="store_true",
                    help="create a throwaway SPACE via API first, then run the write+read test in it")
    args = ap.parse_args()

    token = get_token(args.token)
    print(f"Using token: {token[:12]}... (len {len(token)})")
    guard_account(token)  # refuse glo.com; show which account + scopes we're using
    if args.quota_project:
        print(f"Quota project: {args.quota_project}")

    # --- Decisive test: list spaces (read-only) ---
    status, payload = call("GET", "/spaces", token, args.quota_project)
    verdict = show("DECISIVE: GET /v1/spaces (chat.spaces.readonly)", status, payload)

    if verdict == "PASS":
        n = len(payload.get("spaces", []))
        print(f"\n✅ Consumer account CAN use the Chat API (listed {n} space(s)).")
    elif verdict == "APP_NOT_CONFIGURED":
        print("\n⚠️  Token + account are fine, but the project has no Chat app configured.")
        print("    This is a project-level requirement (NOT a consumer-account block).")
        print("    Fix: Cloud console -> Chat API -> Configuration tab -> set app name +")
        print("    avatar + description, save; then re-run this command.")
        print("    https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat"
              "?project=chat-smoke-1781346315")
        return 2
    elif verdict == "API_NOT_ENABLED":
        print("\n⚠️  Token works, but the Chat API is not enabled on the quota project.")
        print("    Fix: gcloud services enable chat.googleapis.com  (on YOUR personal project)")
        print("    then re-run with --quota-project <THAT_PROJECT_ID>.")
        return 2
    elif verdict == "SCOPE":
        print("\n⚠️  Token lacks the Chat scope. Re-do ADC login with")
        print("    --scopes=https://www.googleapis.com/auth/chat.spaces.readonly,...")
        return 2
    elif verdict == "CONSUMER_BLOCKED":
        print("\n❌ Consumer account appears to be BLOCKED from this method.")
        print("   The 3-personal-accounts design is NOT viable — fall back to a Workspace.")
        return 1
    else:
        print("\n❓ Unexpected response — read the body above to decide.")
        return 1

    # --- Optionally create a throwaway SPACE to test the write path in ---
    if args.create_space and not args.space:
        new = {"spaceType": "SPACE", "displayName": "smoke-test (safe to delete)"}
        sc, pc = call("POST", "/spaces", token, args.quota_project, new)
        vc = show("CREATE SPACE: POST /v1/spaces (chat.spaces.create)", sc, pc)
        if vc != "PASS":
            print("\n⚠️  Could not create a space — see body above (need chat.spaces.create scope).")
            return 1
        args.space = pc.get("name")
        print(f"\nCreated {args.space} — will test write+read here.")

    # --- Optional full-loop test: write then read a message in one space ---
    if args.space:
        body = {"text": "🤖 smoke test — consumer user-OAuth write check (safe to delete)"}
        s2, p2 = call("POST", f"/{args.space}/messages", token, args.quota_project, body)
        v2 = show(f"WRITE: POST /v1/{args.space}/messages (chat.messages)", s2, p2)
        s3, p3 = call("GET", f"/{args.space}/messages?pageSize=5", token, args.quota_project)
        v3 = show(f"READ: GET /v1/{args.space}/messages", s3, p3)
        if v2 == "PASS" and v3 == "PASS":
            print("\n✅ Full read+write loop works for a consumer account. Design confirmed.")
        else:
            print("\n⚠️  List worked but write/read in the space did not — see bodies above.")
            print("    (Common cause: the token only has chat.spaces.readonly, not chat.messages.)")
            return 1
    else:
        print("\nℹ️  To also test write+read, create a Space in the Chat UI with your")
        print("    personal Gmail, then re-run with --space spaces/XXXX and a token")
        print("    that includes the chat.messages scope.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
