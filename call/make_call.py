#!/usr/bin/env python3
"""Make a "phone call" from the bot to a person: mint a REAL Google Meet meeting
AS THE BOT and DM the join link to the callee, so they get a live-call invite.

Concretely, run as the issue bot (``secrets/token_bot.json`` = mikmikb26@gmail.com)
this script:

  1. creates a real meeting via the Meet REST API (``spaces.create`` → a
     ``https://meet.google.com/...`` join link), and
  2. posts a short "calling you" message + that link into the bot↔callee Google
     Chat **DM**, which notifies the callee so they can tap to join.

Default route: **mikmikb26 (bot) → Duc (trantrongducqt@gmail.com)** in their DM
(``GOOGLE_VOICE_SPACE`` = spaces/qtotjoAAAAE).

WHAT THIS IS NOT — the project truth, do not be misled: an AI CANNOT speak on a
Google Meet. The Meet *Media* API (live audio/video) is **receive-only** and
**Developer-Preview-gated** (every scope is ``.readonly``). The Meet *REST* API's
``spaces.create`` only mints a meeting and returns a join link. So this is a real,
joinable call invite for a HUMAN — it does not put an AI on the line. (For a local
AI-voice session see ``call/demo_incident_call.py``; for the incident-scenario
variant of this same Meet flow see ``call/demo_meet_call.py``.)

Auth: user OAuth, the same refresh-token flow as the Chat client. The minting +
posting account is the BOT's token file (default ``secrets/token_bot.json``),
referenced **by path only** — its contents are secret and never read or printed
here. The token MUST carry the
``https://www.googleapis.com/auth/meetings.space.created`` scope
(``scripts/authorize.py`` grants it), and the Meet REST API must be **enabled** in
the GCP project (a ``SERVICE_DISABLED`` 403 → ``gcloud services enable
meet.googleapis.com``).

Run::

    python call/make_call.py                       # bot → Duc (DM), mint + send
    python call/make_call.py --dry-run             # mint + print, do NOT post
    python call/make_call.py --to Alex --space spaces/AAAA...
    python call/make_call.py --message "Quick sync?"   # link still appended

Exit codes: 0 ok · 2 setup error (missing token/client/space) · 1 runtime error
(Meet create / Chat post failed).

Stdlib only at module top level; project modules are imported lazily inside
functions so ``--help`` works with nothing configured.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace

# Allow running straight from a checkout without installing the package.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

# --- defaults (overridable by flag) ---------------------------------------------
# The BOT (mikmikb26) places the call and hosts the meeting.
DEFAULT_TOKEN = "secrets/token_bot.json"
DEFAULT_CALLEE_NAME = "Duc"


def _resolve_path(path: str) -> str:
    """Resolve a (possibly relative) path against the repo root, so the script
    works regardless of the caller's cwd."""
    return path if os.path.isabs(path) else os.path.join(_REPO_ROOT, path)


def _compose_message(callee_name: str, meeting_uri: str) -> str:
    """A short, generic call-invite ending with the Meet join link."""
    return (
        f"\U0001F4DE {callee_name}, calling you — let's hop on a quick call. "
        f"Join here: {meeting_uri}"
    )


def _apply_override(message: str, meeting_uri: str) -> str:
    """Use the operator's --message verbatim, but still append the join link if the
    override omits it (the whole point is to get the callee onto the call)."""
    text = message.strip()
    if meeting_uri and meeting_uri not in text:
        text = f"{text} Join the call here: {meeting_uri}"
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_call",
        description="Mint a real Google Meet meeting AS THE BOT and DM the join "
        "link to the callee, so a HUMAN joins a live call (the AI cannot speak on "
        "Meet — its Media API is receive-only + preview-gated).",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN,
        help="OAuth token file for the account that places the call (mints the "
        "Meet + sends the invite) — the BOT (default: secrets/token_bot.json). "
        "Referenced by path only; contents are never read or printed here.",
    )
    parser.add_argument(
        "--to", default=DEFAULT_CALLEE_NAME, help="callee display name (default: Duc)."
    )
    parser.add_argument(
        "--space",
        default="",
        help="Chat space/DM to post into (e.g. spaces/AAAA...). Empty => the "
        "bot↔callee DM (GOOGLE_VOICE_SPACE), else GOOGLE_SPACE.",
    )
    parser.add_argument(
        "--message",
        default="",
        help="override the call-invite body verbatim (the join link is still "
        "appended if your text omits it).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="mint the Meet link and PRINT the link + message, but do NOT post.",
    )
    args = parser.parse_args(argv)

    # --- 1. config + preflight ---------------------------------------------------
    from gchat_agent.config import load_config  # lazy: project module

    cfg = load_config(os.path.join(_REPO_ROOT, ".env"))

    token_path = _resolve_path(args.token)
    if not os.path.exists(token_path):
        print(
            f"ERROR: token file not found: {token_path}\n"
            "  Mint one with scripts/authorize.py "
            "(needs the meetings.space.created scope), or pass --token <tok.json>.",
            file=sys.stderr,
        )
        return 2

    client_path = _resolve_path(cfg.GOOGLE_OAUTH_CLIENT)
    if not os.path.exists(client_path):
        print(
            f"ERROR: Google OAuth client file not found: {client_path}\n"
            "  Set GOOGLE_OAUTH_CLIENT in .env to your Desktop OAuth client JSON.",
            file=sys.stderr,
        )
        return 2

    # --- 2. mint a REAL Google Meet meeting (as the bot) -------------------------
    from gchat_agent.meet.rest import MeetRestClient  # lazy: project module

    # Direct instantiation — we always want a meeting here, so skip the MEET_LINKS
    # gate that build_meet() enforces. Pass cfg.MEET_API_URL for parity (proxy override).
    meet = MeetRestClient(cfg, token_file=token_path, api_url=cfg.MEET_API_URL)

    print(f"=== Calling {args.to} ===")
    print(f"  bot mints + invites via token: {token_path}")
    try:
        space = meet.create_space()
    except RuntimeError as exc:  # hard failure from the Meet REST client
        msg = str(exc)
        print(f"ERROR: could not create the Meet meeting: {msg}", file=sys.stderr)
        low = msg.lower()
        if "service_disabled" in low or "has not been used in project" in low:
            print(
                "  hint: the Google Meet REST API is not enabled in this GCP "
                "project. Enable it once at "
                "https://console.cloud.google.com/apis/library/meet.googleapis.com "
                "(or: gcloud services enable meet.googleapis.com --project <id>), "
                "wait ~1 min, then retry.",
                file=sys.stderr,
            )
        elif "access_token_scope_insufficient" in low or "insufficient authentication scopes" in low:
            print(
                "  hint: re-run scripts/authorize.py for this account to grant the "
                "meetings.space.created scope (an older token lacks it).",
                file=sys.stderr,
            )
        elif "403" in low or "permission" in low:
            print(
                "  hint: a 403 from Meet usually means the meetings.space.created "
                "scope is missing (re-run scripts/authorize.py) OR the Meet REST API "
                "is disabled in the project (enable it in the Cloud console).",
                file=sys.stderr,
            )
        return 1

    # --- 3. compose the invite ---------------------------------------------------
    if args.message.strip():
        message = _apply_override(args.message, space.meeting_uri)
    else:
        message = _compose_message(args.to, space.meeting_uri)

    # --- 4. dry run: print, don't post -------------------------------------------
    if args.dry_run:
        print(f"\n\U0001F517 Meet link: {space.meeting_uri}")
        print(f"\U0001F4AC Message (NOT posted — --dry-run):\n  {message}")
        return 0

    # --- 5. DM the invite + link as the bot --------------------------------------
    # Prefer an explicit --space, then the bot↔callee DM (GOOGLE_VOICE_SPACE — the
    # same DM voice reports use), then the shared space as a last resort.
    target_space = (
        args.space or cfg.GOOGLE_VOICE_SPACE or cfg.GOOGLE_SPACE or ""
    ).strip()
    if not target_space:
        print(
            "ERROR: no Chat space to post to — pass --space spaces/<id> or set "
            "GOOGLE_VOICE_SPACE / GOOGLE_SPACE in .env. (The Meet link was created: "
            f"{space.meeting_uri})",
            file=sys.stderr,
        )
        return 2

    from gchat_agent.chat.google_rest import GoogleChatClient  # lazy: project module

    # Post to the RESOLVED target space (the client reads its space from
    # config.GOOGLE_SPACE, so override it for this post).
    chat = GoogleChatClient(
        replace(cfg, GOOGLE_SPACE=target_space), token_file=token_path
    )
    # Tie the request id to the freshly-minted meeting code so each call is a NEW
    # message (a new meeting → a new code), never deduped against a prior call.
    request_id = f"make-call-{space.meeting_code or space.meeting_uri}"
    try:
        chat.post_message(message, request_id=request_id)
    except Exception as exc:  # noqa: BLE001 - friendly one-liner, not a traceback
        print(
            f"ERROR: created the Meet link ({space.meeting_uri}) but the Chat post "
            f"failed: {exc}",
            file=sys.stderr,
        )
        return 1

    print(f"\n\U0001F517 Meet link: {space.meeting_uri}")
    print(f"\U0001F4AC Posted to Chat space {target_space}:\n  {message}")
    print(f"✅ {args.to} can now join the live call.")
    return 0


# Run example::
#   python call/make_call.py                 # bot → Duc (DM): mint + send
#   python call/make_call.py --dry-run       # mint + show, no Chat post
if __name__ == "__main__":
    raise SystemExit(main())
