#!/usr/bin/env python3
"""Detect a native Google **Chat 1:1 call** (huddle) ending — i.e. "the other user
hung up" — via the **Chat REST API** (no browser, no Meet REST).

The clean signal (discovered empirically 2026-06-18). A DM call posts a *message*
into the space whose annotation is a ``MEET_SPACE`` rich-link carrying a
``meetSpaceLinkData`` block::

    {"meetingCode": "abc-defg-hij", "type": "HUDDLE", "huddleStatus": "<STATE>"}

``huddleStatus`` is the call's lifecycle. Terminal values observed:
  - ``MISSED``  — the callee never answered the ring (no 2-party connect).
  - ``ENDED``   — the call connected and then ended (someone hung up).
A live call shows a non-terminal status (e.g. STARTED/ONGOING) which this watcher
prints verbatim as it transitions, so the ENDED transition = the hang-up moment.

Why this beats the other two channels (both dead ends for a native Chat call):
  - **Meet REST API** is BLIND to Chat-UI 1:1 calls — ``conferenceRecords`` is
    empty for the call's space and ``spaces.get`` 400s (only bot-MINTED spaces are
    visible). Confirmed twice, including against a genuinely connected call.
  - **Browser network** only shows the hang-up *indirectly* (the call's
    ``/webchannel/events`` long-poll SID is rotated + the DOM call UI tears down);
    the clean roster-leave frame is inside the ``SyncMeetingSpaceCollections``
    server-stream, which Playwright's ``response.body()`` can't read mid-stream.

The Chat REST path needs only the ``chat.messages.readonly`` (or ``chat.messages``)
scope the bot token already has, and is a supported, pollable API — so it's the
real answer.

⚠️ Latency: ``huddleStatus`` is a server-updated message annotation, so ENDED lags
the physical hang-up by the propagation + poll interval (seconds), not instant. For
truly instant push you'd need the Workspace Events API (``google.workspace.chat.
message`` events over Pub/Sub) — this watcher measures the lag empirically.

Run
---
  # start watching BEFORE placing the call (so --since pins to now):
  python scripts/huddle_watch.py --duration 240
  # then place the ringing call (meet_call_browser.py / call_network_capture.py),
  # answer the RING on the callee's device, stay, then hang up.

Defaults: bot↔Duc DM (spaces/qtotjoAAAAE), bot token, poll 2s.
"""
from __future__ import annotations

import argparse
import json
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

_CHAT_API = "https://chat.googleapis.com/v1"
_TERMINAL = {"ENDED", "MISSED"}


class Chat:
    """Minimal authed GET wrapper for the Chat REST API (reuses chat.oauth)."""

    def __init__(self, cfg, token_file: str):
        self._cfg = cfg
        self._token_file = token_file
        self._qp = cfg.GOOGLE_QUOTA_PROJECT or None

    def get(self, path: str, params: dict | None = None) -> tuple[int, dict]:
        url = f"{_CHAT_API}/{path}"
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
            self._cfg.GOOGLE_OAUTH_CLIENT, self._token_file, self._qp)


def _huddles(msg: dict):
    """Yield (meetingCode, type, huddleStatus) for each MEET_SPACE annotation."""
    for a in msg.get("annotations", []) or []:
        meta = a.get("richLinkMetadata") or {}
        d = meta.get("meetSpaceLinkData")
        if d:
            yield d.get("meetingCode", "?"), d.get("type", "?"), d.get("huddleStatus")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="huddle_watch",
        description="Detect a native Chat 1:1 call ending via the Chat REST API.")
    p.add_argument("--space", default="spaces/qtotjoAAAAE",
                   help="space/DM to watch (default the bot<->Duc DM).")
    p.add_argument("--token", default="secrets/token_bot.json",
                   help="OAuth token file (default the bot = mikmikb26 = caller).")
    p.add_argument("--code", default="",
                   help="only follow this meeting code (default: newest huddle "
                   "created at/after --since).")
    p.add_argument("--poll", type=float, default=2.0, help="seconds between polls.")
    p.add_argument("--duration", type=float, default=240.0,
                   help="max seconds to watch (default 240).")
    p.add_argument("--since", type=float, default=-1.0,
                   help="epoch seconds; only consider huddle messages created at/"
                   "after this (default now). Use 0 to consider existing calls too.")
    args = p.parse_args(argv)

    cfg = load_config()
    token_file = args.token if os.path.isabs(args.token) else \
        os.path.join(_REPO_ROOT, args.token)
    chat = Chat(cfg, token_file)

    t0 = time.monotonic()
    since = time.time() if args.since < 0 else args.since
    print("=== Chat huddle hang-up watch (Chat REST API) ===")
    print(f"  space: {args.space}   token: {args.token}   poll: {args.poll}s")
    print(f"  following: " + (f"code={args.code}" if args.code
                              else f"newest HUDDLE created >= {since:.0f} (now)"))
    print("\nwatching huddleStatus … (place the call now; answer the RING + stay, "
          "then hang up)\n")

    last_status: dict[str, str | None] = {}   # code -> last seen status
    followed: str | None = args.code or None
    deadline = t0 + args.duration
    while time.monotonic() < deadline:
        ts = round(time.monotonic() - t0, 1)
        st, body = chat.get(f"{args.space}/messages",
                            {"pageSize": 10, "orderBy": "createTime desc"})
        if st != 200:
            print(f"  [{ts:6.1f}s] messages.list HTTP {st}: {str(body)[:200]}")
            time.sleep(args.poll)
            continue
        # newest first; pick the huddle to follow
        for m in body.get("messages", []) or []:
            ct = m.get("createTime", "")
            for code, typ, status in _huddles(m):
                # epoch of message createTime (RFC3339) for the --since gate
                if followed is None and args.since != 0:
                    # parse createTime to epoch
                    try:
                        import datetime
                        ep = datetime.datetime.fromisoformat(
                            ct.replace("Z", "+00:00")).timestamp()
                    except Exception:  # noqa: BLE001
                        ep = since
                    if ep + 1 < since:
                        continue  # an old call, not ours
                if followed is None:
                    followed = code
                    print(f"  [{ts:6.1f}s] 📞 following huddle {code} "
                          f"(type={typ}, created {ct})")
                if code != followed:
                    continue
                if last_status.get(code) != status:
                    arrow = "📴" if status in _TERMINAL else "•"
                    print(f"  [{ts:6.1f}s] {arrow} huddleStatus: "
                          f"{last_status.get(code)} -> {status}")
                    last_status[code] = status
                    if status in _TERMINAL:
                        verdict = ("HUNG UP / ENDED" if status == "ENDED"
                                   else "NEVER ANSWERED (missed)")
                        print(f"\n✅ Terminal status for {code}: {status}  "
                              f"= {verdict}  (at ~{ts:.1f}s)")
                        return 0
        if followed is None:
            print(f"  [{ts:6.1f}s] …no new huddle message yet")
        time.sleep(args.poll)

    print(f"\n⏱ Reached the {args.duration:.0f}s cap. "
          f"Last status: {last_status}")
    return 0 if any(v in _TERMINAL for v in last_status.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
