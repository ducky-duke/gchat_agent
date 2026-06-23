#!/usr/bin/env python3
"""Detect when the OTHER participant hangs up — via the **Google Meet REST API**
(no browser/network scraping). This is the clean, supported path: poll the active
conference's participant roster and watch for a REMOTE participant's
``latestEndTime`` to get set (equivalently: they drop off the
``latest_end_time IS NULL`` active-participant filter). That transition is the
"user kia cúp máy" signal.

Channel (Meet REST v2, scope ``…/auth/meetings.space.created`` OR
``…/auth/meetings.space.readonly`` — the bot token already has the former):
  - ``GET /v2/conferenceRecords?filter=end_time IS NULL``           → active conferences
  - ``GET /v2/conferenceRecords?filter=space.meeting_code="abc-…"`` → by join code
  - ``GET /v2/conferenceRecords/{rec}/participants?filter=latest_end_time IS NULL``
        → participants still in the call (each: displayName + users/<id> +
          earliestStartTime; latestEndTime is null while present, set on leave)

A conference record is created when the conference STARTS (someone joins) and is
queryable by the MEETING ORGANIZER's token. ⚠️ REST conference/participant data
has propagation latency (seconds–minutes), so this detects a hangup *reliably*
but not necessarily *instantly* — for truly real-time push use the Workspace
Events API (participant joined/left over Pub/Sub). This script measures the actual
lag empirically.

Run
---
  # target a specific meeting by its join code (from the meet.google.com/<code> link):
  python call/meet_rest_watch.py --meeting-code abc-mnop-xyz

  # or auto-find the bot account's currently-active conference:
  python call/meet_rest_watch.py --auto

Defaults: bot token (secrets/token_bot.json = mikmikb26 = the caller), poll 2s,
self = the bot's user id (everyone else = a REMOTE whose leave is the signal).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from gchat_agent.chat import oauth          # noqa: E402
from gchat_agent.config import load_config  # noqa: E402

_API = "https://meet.googleapis.com/v2"
# The bot account (mikmikb26) is the caller; any OTHER participant is the remote
# whose hang-up we want. Override with --self-id if running as a different account.
_DEFAULT_SELF_ID = "users/116566195804326411461"


class Meet:
    """Minimal authed GET wrapper for the Meet REST API (reuses chat.oauth)."""

    def __init__(self, cfg, token_file: str):
        self._cfg = cfg
        self._token_file = token_file
        self._qp = cfg.GOOGLE_QUOTA_PROJECT or None

    def get(self, path: str, params: dict | None = None) -> tuple[int, dict]:
        import json
        url = f"{_API}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self._token()}")
        if self._qp:
            req.add_header("x-goog-user-project", self._qp)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else {}
            except Exception:  # noqa: BLE001
                return exc.code, {"_raw": raw.decode("utf-8", "replace")}

    def _token(self) -> str:
        return oauth.get_access_token(
            self._cfg.GOOGLE_OAUTH_CLIENT, self._token_file, self._qp
        )


def _who(p: dict) -> tuple[str, str]:
    """(user_id, display_name) for a participant, across signed-in/anon/phone."""
    su = p.get("signedinUser") or {}
    if su:
        return su.get("user", ""), su.get("displayName", "") or "(signed-in)"
    au = p.get("anonymousUser") or {}
    if au:
        return "", au.get("displayName", "") or "(anonymous)"
    ph = p.get("phoneUser") or {}
    if ph:
        return "", ph.get("displayName", "") or "(phone)"
    return "", "(unknown)"


def _resolve_conference(meet: Meet, *, meeting_code: "str | None",
                        find_timeout: float, poll: float) -> "str | None":
    """Find the conferenceRecord to watch: by meeting_code, else auto (the most
    recent active conference, when meeting_code is falsy). Polls until one appears
    or find_timeout elapses."""
    deadline = time.monotonic() + find_timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if meeting_code:
            f = f'space.meeting_code="{meeting_code}"'
        else:
            f = "end_time IS NULL"
        st, body = meet.get("conferenceRecords",
                            {"filter": f, "pageSize": 10})
        if st != 200:
            print(f"  ! conferenceRecords.list HTTP {st}: {str(body)[:300]}")
            return None
        recs = body.get("conferenceRecords", []) or []
        # Prefer an ACTIVE record (no endTime); else the newest match.
        active = [r for r in recs if not r.get("endTime")]
        chosen = (active or recs)
        if chosen:
            r = chosen[0]
            state = "ACTIVE" if not r.get("endTime") else f"ended {r.get('endTime')}"
            print(f"  ✓ conference: {r['name']}  space={r.get('space')}  "
                  f"start={r.get('startTime')}  [{state}]")
            return r["name"]
        print(f"  …no matching conference yet (try {attempt}, filter={f!r}) — "
              "the record appears once the conference STARTS + propagates")
        time.sleep(poll)
    return None


def watch(meet: Meet, *, meeting_code: "str | None", self_id: str,
          poll: float, duration: float, find_timeout: float,
          stop_event=None) -> int:
    """Resolve the conference (by meeting_code, or auto = active when falsy) then
    poll its active roster: print the room data each cycle and report the remote
    LEAVE (hang-up). Reusable by other callers (e.g. meet_call_browser --watch-rest,
    which runs this in a urllib-only thread); pass a threading.Event as stop_event to
    end the loop early when the call drops. Returns 0 (hang-up seen / stopped clean),
    1 (no conference found), 2 (duration cap reached with no hang-up)."""
    rec = _resolve_conference(meet, meeting_code=meeting_code,
                              find_timeout=find_timeout, poll=poll)
    if not rec:
        print("\n✗ No conference record found in time. Either no one has joined yet, "
              "the meeting is under a different account, or REST hasn't propagated "
              "it. (Confirm you joined as the token's account; pass --meeting-code.)")
        return 1

    # --- poll the active roster; report joins + the remote LEAVE (hang-up) -------
    t0 = time.monotonic()
    seen: dict[str, dict] = {}        # name -> participant (last seen active)
    remote_present: set[str] = set()  # display names of active REMOTE participants
    print("\nwatching active participants (filter: latest_end_time IS NULL) …")
    deadline = t0 + duration
    hangup_reported = False
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            print("\n(REST watch stopped — the call ended.)")
            return 0 if hangup_reported else 2
        st, body = meet.get(f"{rec}/participants",
                            {"filter": "latest_end_time IS NULL", "pageSize": 50})
        ts = round(time.monotonic() - t0, 1)
        if st != 200:
            print(f"  [{ts:6.1f}s] participants.list HTTP {st}: {str(body)[:200]}")
            time.sleep(poll)
            continue
        parts = body.get("participants", []) or []
        active_names = []
        active_remotes = set()
        for pr in parts:
            uid, name = _who(pr)
            tag = "self" if uid == self_id else "REMOTE"
            active_names.append(f"{name}[{tag}]")
            if uid != self_id:
                active_remotes.add(name)
            seen[name] = pr

        # diff vs last cycle
        joined = active_remotes - remote_present
        left = remote_present - active_remotes
        for nm in sorted(joined):
            print(f"  [{ts:6.1f}s] ➕ REMOTE JOINED: {nm}")
        for nm in sorted(left):
            print(f"  [{ts:6.1f}s] 📴 REMOTE LEFT (HANG-UP): {nm}  "
                  "← latest_end_time now set")
            hangup_reported = True
        if not joined and not left:
            print(f"  [{ts:6.1f}s] active: {active_names or '(none)'}")
        remote_present = active_remotes

        # If a remote joined then everyone remote is gone, we've captured the signal.
        if hangup_reported and not active_remotes:
            print(f"\n✅ Hang-up captured at ~{ts:.1f}s "
                  "(remote dropped off the active-participant roster).")
            # confirm with the full record (shows the exact latestEndTime)
            st2, body2 = meet.get(f"{rec}/participants", {"pageSize": 50})
            if st2 == 200:
                print("\nFull participant records (with latestEndTime = leave time):")
                for pr in body2.get("participants", []) or []:
                    uid, name = _who(pr)
                    print(f"  - {name} ({uid or 'no-id'})  "
                          f"start={pr.get('earliestStartTime')}  "
                          f"end={pr.get('latestEndTime') or 'STILL IN'}")
            return 0
        time.sleep(poll)

    print(f"\n⏱ Reached the {duration:.0f}s cap "
          f"(hang-up {'WAS' if hangup_reported else 'was NOT'} observed).")
    return 0 if hangup_reported else 2


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="meet_rest_watch",
        description="Detect a remote participant hang-up via the Meet REST API.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--meeting-code", default="",
                   help="join code from meet.google.com/<code> (e.g. abc-mnop-xyz).")
    g.add_argument("--auto", action="store_true",
                   help="auto-find the bot account's currently-active conference.")
    p.add_argument("--token", default="secrets/token_bot.json",
                   help="OAuth token file (default the bot = mikmikb26 = caller).")
    p.add_argument("--self-id", default=_DEFAULT_SELF_ID,
                   help="our users/<id>; everyone else is a REMOTE (leave = signal).")
    p.add_argument("--poll", type=float, default=2.0, help="seconds between polls.")
    p.add_argument("--duration", type=float, default=300.0,
                   help="max seconds to watch (default 300).")
    p.add_argument("--find-timeout", type=float, default=60.0,
                   help="seconds to wait for the conference record to appear.")
    args = p.parse_args(argv)
    if not args.meeting_code and not args.auto:
        args.auto = True

    cfg = load_config()
    token_file = args.token if os.path.isabs(args.token) else \
        os.path.join(_REPO_ROOT, args.token)
    meet = Meet(cfg, token_file)

    print("=== Meet REST hang-up watch ===")
    print(f"  token: {args.token}   self: {args.self_id}   poll: {args.poll}s")
    print(f"  target: " + (f"meeting_code={args.meeting_code}" if args.meeting_code
                           else "auto (active conference)"))

    return watch(
        meet,
        meeting_code=args.meeting_code or None,
        self_id=args.self_id,
        poll=args.poll,
        duration=args.duration,
        find_timeout=args.find_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
