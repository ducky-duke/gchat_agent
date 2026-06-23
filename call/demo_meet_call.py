#!/usr/bin/env python3
"""Mint a REAL Google Meet meeting AS THE ISSUE BOT and send the join link to the
human stakeholder in Chat, so they join a live incident call.

This is the achievable "incident phone call to Duc": the **issue bot**
(``secrets/token_bot.json`` = mikmikb26@gmail.com) creates a real meeting via the
Meet REST API (``spaces.create`` → a ``https://meet.google.com/...`` join link) and
DMs a tight incident briefing + that link to **Duc** (trantrongducqt@gmail.com)
about the production API-gateway 504 incident (INFRA-2207, from the ``apigw``
scenario in ``data/scenarios.json``). The bot HOSTS the meeting and invites the
stakeholder — the reporter's own account is not the convener.

WHY THIS SHAPE (the project truth — do not be misled): an AI CANNOT speak on a
Google Meet. The Meet *Media* API (live audio/video) is **receive-only** and
**Developer-Preview-gated** — every scope is ``.readonly`` ("Capture real-time
audio/video"; see ``docs/google_meet/media-api/guides/get-started.md.txt``). The
Meet *REST* API's ``spaces.create`` only mints a meeting and returns a join link.
So the real, shippable integration is: create a meeting + share its link so a
HUMAN joins the call. This script does NOT put an AI on a call.

It complements ``call/demo_incident_call.py`` — that one places a local Gemini
Live API voice "phone call" (the AI talks on YOUR mic/speaker, not on Meet); this
one mints a shareable Meet link for a human-staffed call in the Chat demo world.
Reference for the Meet APIs is bundled at ``docs/google_meet/``.

Auth
----
User OAuth, the same refresh-token flow as the Chat client. The minting +
posting account is the BOT's token file (default ``secrets/token_bot.json``
= mikmikb26@gmail.com), referenced **by path only** — its contents are secret and
never read or printed here. The token MUST carry the
``https://www.googleapis.com/auth/meetings.space.created`` scope
(``scripts/authorize.py`` grants it); a token minted before that scope existed
gets a 403 until re-authorized.

Run::

    python call/demo_meet_call.py                       # mint + post (apigw → Duc)
    python call/demo_meet_call.py --dry-run             # mint + print, do NOT post
    python call/demo_meet_call.py --persona ops         # a different scenario
    python call/demo_meet_call.py --token secrets/token_promo.json
    python call/demo_meet_call.py --message "Custom briefing"   # link still appended

Exit codes: 0 ok · 2 setup error (missing token/client/space) · 1 runtime error
(Meet create / Chat post failed).

Stdlib only at module top level; the project's own modules (config, meet, chat,
staff) are imported lazily inside functions so ``--help`` works with nothing
configured.
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
DEFAULT_PERSONA = "apigw"
# The ISSUE BOT (mikmikb26) hosts the Meet and invites the human stakeholder — so
# the meeting is created by the bot, not by the reporter's own account.
DEFAULT_TOKEN = "secrets/token_bot.json"
DEFAULT_CALLEE_NAME = "Duc"
DEFAULT_CALLEE_EMAIL = "trantrongducqt@gmail.com"


def _resolve_path(path: str) -> str:
    """Resolve a (possibly relative) path against the repo root, so the script
    works regardless of the caller's cwd."""
    return path if os.path.isabs(path) else os.path.join(_REPO_ROOT, path)


def _load_persona(persona_id: str) -> dict:
    """Load one persona ({role, facts, seed_messages}) from ``data/scenarios.json``
    (the single source of truth shared with run_staff.py / demo_incident_call.py)."""
    from gchat_agent.agent.staff import load_personas  # lazy: project module

    personas = load_personas(os.path.join(_REPO_ROOT, "data", "scenarios.json"))
    if persona_id not in personas:
        have = ", ".join(sorted(personas)) or "(none)"
        raise SystemExit(f"persona {persona_id!r} not in scenarios.json (have: {have})")
    return personas[persona_id]


def _reporter_name(role: str) -> str:
    """Pull the persona's first name out of the 'You are <Name>, ...' role string."""
    head = role.strip()
    if head.lower().startswith("you are "):
        head = head[len("you are "):]
    name = head.split(",", 1)[0].strip()
    return name or "the on-call engineer"


def _compose_message(persona: dict, callee_name: str, meeting_uri: str) -> str:
    """A tight call-invite in the ISSUE BOT's voice, addressed to the human
    stakeholder and ending with the Meet join link. Pulls the incident facts the
    bot holds from the detected issue (ticket, impact, on-call owner)."""
    name = _reporter_name(persona.get("role", ""))
    facts = persona.get("facts", {}) or {}
    # The scenario `ticket` fact may be a full sentence ("Tracked as INFRA-2207 in
    # Jira.") or a fragment; normalise to a fragment for the headline.
    ticket = str(facts.get("ticket", "") or "").strip().rstrip(".")
    ticket = ticket.removeprefix("Tracked as ").strip()
    impact = str(facts.get("impact", "") or "").strip()

    headline = f"incident {ticket}" if ticket else "a production incident"
    parts = [f"\U0001F534 {callee_name}, {headline} needs a live look."]
    if impact:
        parts.append(impact if impact.endswith((".", "!", "?")) else impact + ".")
    parts.append(
        f"{name} (on-call) is on it — I've opened a Meet so we can work it "
        f"together. Join here: {meeting_uri}"
    )
    return " ".join(parts)


def _apply_override(message: str, meeting_uri: str) -> str:
    """Use the operator's --message verbatim, but still append the join link if the
    override omits it (the whole point is to get the callee onto the call)."""
    text = message.strip()
    if meeting_uri and meeting_uri not in text:
        text = f"{text} Join the incident call here: {meeting_uri}"
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="demo_meet_call",
        description="Mint a real Google Meet meeting and post its join link into "
        "Google Chat so a HUMAN joins a live incident call (the AI cannot speak on "
        "Meet — its Media API is receive-only + preview-gated).",
    )
    parser.add_argument(
        "--persona",
        default=DEFAULT_PERSONA,
        choices=("apigw", "ops", "promo", "dupe"),
        help="which scenario from data/scenarios.json supplies the briefing text "
        "(default: apigw = the API-gateway-timeout incident, INFRA-2207).",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN,
        help="OAuth token file for the account that mints the Meet + sends the "
        "invite — the ISSUE BOT (default: secrets/token_bot.json). Referenced by "
        "path only; its contents are never read or printed here.",
    )
    parser.add_argument(
        "--callee", default=DEFAULT_CALLEE_NAME, help="callee display name."
    )
    parser.add_argument(
        "--callee-email",
        default=DEFAULT_CALLEE_EMAIL,
        help="callee email (display only).",
    )
    parser.add_argument(
        "--space",
        default="",
        help="Chat space override (e.g. spaces/AAAA...). Empty => the bot↔recipient "
        "DM (GOOGLE_VOICE_SPACE), else GOOGLE_SPACE.",
    )
    parser.add_argument(
        "--message",
        default="",
        help="override the composed Chat message body verbatim (the join link is "
        "still appended if your text omits it).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="mint the Meet link and PRINT the link + composed message, but do NOT "
        "post to Chat.",
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

    persona = _load_persona(args.persona)

    # --- 2. mint a REAL Google Meet meeting (as the bot) -------------------------
    from gchat_agent.meet.rest import MeetRestClient  # lazy: project module

    # Direct instantiation — the demo doesn't need the MEET_LINKS gate that
    # build_meet() enforces; we always want to create a meeting here. Pass
    # cfg.MEET_API_URL for parity with build_meet() (honors a proxy override).
    meet = MeetRestClient(cfg, token_file=token_path, api_url=cfg.MEET_API_URL)

    print(f"=== Meet incident call → {args.callee} ({args.callee_email}) ===")
    print(f"  scenario: {args.persona}    bot mints + invites via token: {token_path}")
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

    # --- 3. compose the briefing -------------------------------------------------
    if args.message.strip():
        message = _apply_override(args.message, space.meeting_uri)
    else:
        message = _compose_message(persona, args.callee, space.meeting_uri)

    # --- 4. dry run: print, don't post -------------------------------------------
    if args.dry_run:
        print(f"\n\U0001F517 Meet link: {space.meeting_uri}")
        print(f"\U0001F4AC Message (NOT posted — --dry-run):\n  {message}")
        return 0

    # --- 5. post the briefing + link into Chat as the reporter -------------------
    # The bot invites the stakeholder PRIVATELY: prefer an explicit --space, then
    # the bot↔recipient DM (GOOGLE_VOICE_SPACE — the same DM voice reports use),
    # then the shared incident space as a last resort.
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

    # Post to the RESOLVED target space — honor --space, not just cfg.GOOGLE_SPACE
    # (the client reads its space from config.GOOGLE_SPACE, so override it).
    chat = GoogleChatClient(
        replace(cfg, GOOGLE_SPACE=target_space), token_file=token_path
    )
    # Stable per-(persona, space, link) request id so a re-run against the same
    # space is idempotent on the same meeting rather than duplicating the post.
    request_id = f"meet-call-{args.persona}-{space.meeting_code or space.meeting_uri}"
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
    print(f"✅ {args.callee} can now join the live incident call.")
    return 0


# Run example::
#   python call/demo_meet_call.py --persona apigw --callee Duc
#   python call/demo_meet_call.py --dry-run        # mint + show, no Chat post
if __name__ == "__main__":
    raise SystemExit(main())
