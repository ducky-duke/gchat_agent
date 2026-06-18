#!/usr/bin/env python3
"""Place a ringing Google Chat call AND capture the browser's network to discover
the *hang-up signal* — i.e. WHICH network event fires the moment the callee ends
the call. This is the empirical replacement for ``meet_call_browser.py``'s brittle
DOM heuristic ("the leave button disappeared"): instead of guessing from the DOM,
we record every HTTP request/response + WebSocket frame with timestamps, use the
DOM end-of-call as a GROUND-TRUTH marker, and then print the network events that
clustered right before that marker. The inbound event immediately preceding the
UI flipping to "call ended" is the signal that *caused* it.

How it drives the call
----------------------
Reuses ``meet_call_browser.py`` verbatim for the proven call-placement path
(attach to the daily Brave over CDP → open the DM → click "Start a video call" →
click "Join now" → confirm a leave/hang-up control appeared). We add only the
network instrumentation on top.

What it captures (to a JSONL, one event per line, flushed live so a kill keeps data)
  - ``req``      every request (method, url, resourceType)
  - ``resp``     every response (status, url)
  - ``ws_open``  a WebSocket opened (url)
  - ``ws_recv``  an inbound WS frame (len + payload preview; text inline, binary b64)
  - ``ws_sent``  an outbound WS frame
  - ``ws_close`` a WebSocket closed
  - ``mark``     our own DOM-derived markers (in_call_confirmed, leave_control_gone, …)
Context-level request/response capture covers cross-origin IFRAMEs too; WebSocket
listeners are attached per page (the in-DM Meet UI usually opens as a popup/new
top-level meet.google.com page, so its WS is captured cleanly).

Finding the signal
-------------------
On exit (call ended / cap / Ctrl+C) it prints, for the window around the
leave-control-gone marker, the inbound events (ws_recv + resp) sorted by time with
their offset from the marker. The candidate hang-up signal is the inbound WS frame
(or long-poll response) with a small NEGATIVE offset — it arrived just before the
UI tore the call down.

Run
---
  # 1) SAFE dry pass — open the DM + attach capture, do NOT ring (verify harness,
  #    see which WebSockets exist even idle):
  python scripts/call_network_capture.py --no-call --probe-secs 20

  # 2) REAL ringing call — rings the callee; ANSWER on the other account and HANG
  #    UP to capture the target signal. Exits early when the call ends.
  python scripts/call_network_capture.py --duration 240

Defaults target the bot↔Duc DM (qtotjoAAAAE) on u/1 (mikmikb26) via the daily
Brave at http://127.0.0.1:9222 — i.e. the proven recipe in scripts/CLAUDE.md.

⚠️  Same caveats as meet_call_browser.py: this automates Google's UI (ToS, brittle,
account-flag risk) — demo accounts only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, _THIS_DIR)            # to import the sibling script as a module
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

import meet_call_browser as mcb          # reuse the proven call-placement helpers

# Meet's pre-join "commit" control — clicking it is what actually RINGS the callee.
_JOIN_PATTERNS = (r"join now", r"^join$", r"join call", r"ask to join")

# Hosts that carry Chat/Meet real-time SIGNALING (idle Chat showed *-signaler-pa;
# a live call adds meet/tachyon/instantmessaging). We grab the BODY of small
# responses from these so the actual "participant left / call ended" payload is
# readable — WebSockets aren't used (idle capture showed ws_open=0). Static asset
# hosts/paths are excluded so we don't fetch megabyte JS bundles.
# Narrowed to the two channels that actually carry CALL STATE: Meet's meeting-state
# RPCs (participant join/leave, device collection) and the Chat webchannel long-poll.
# This drops the high-volume contact/roster lookups (people-pa, peoplestack,
# myphonenumbers) that flooded — and slowed — the first capture without carrying any
# hang-up signal.
_SIGNAL_HOST = re.compile(
    r"meet\.google\.com/\$rpc/google\.rtc\.meetings|/webchannel/events", re.I)
_SIGNAL_SKIP = re.compile(r"mss-static|/_/js/|/_/ss/|\.js(\?|$)|\.css(\?|$)|gstatic", re.I)
# webchannel heartbeats/acks ("9\n[1,21,7]\n", noop) carry no call state — drop from
# the report so the real (longer) call-state frames stand out.
_WC_NOISE = re.compile(r"\[1,\d+,7\]|noop|^\)\]\}'", re.I)


def _frame_preview(payload) -> dict:
    """Compact, log-safe preview of a WS frame payload. Text inline (truncated);
    binary as a short base64 prefix (Meet frames are largely binary protobuf)."""
    if isinstance(payload, (bytes, bytearray)):
        b = bytes(payload)
        return {"enc": "b", "len": len(b), "prev": base64.b64encode(b[:160]).decode()}
    s = "" if payload is None else str(payload)
    return {"enc": "t", "len": len(s), "prev": s[:400]}


class Capture:
    """Streams network events to a JSONL (flushed per line) and keeps them in memory
    for the end-of-run correlation. Playwright's sync callbacks run on this thread,
    so a plain list + file handle need no locking."""

    def __init__(self, out_path: str):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        self._fh = open(out_path, "w", buffering=1)  # line-buffered
        self.out_path = out_path
        self.events: list[dict] = []
        self.t0 = time.monotonic()
        self.sig_pending: list = []   # (Response, url, t_req) awaiting body fetch

    def log(self, kind: str, **data) -> dict:
        ev = {"m": round(time.monotonic() - self.t0, 3), "w": round(time.time(), 3),
              "kind": kind, **data}
        self.events.append(ev)
        try:
            self._fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - logging must never break the call
            pass
        return ev

    def close(self):
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass

    # --- listener factories -------------------------------------------------------
    def on_request(self, req):
        try:
            self.log("req", method=req.method, url=req.url, rtype=req.resource_type)
        except Exception:  # noqa: BLE001
            pass

    def on_response(self, resp):
        try:
            url = resp.url
            self.log("resp", status=resp.status, url=url)
            if _SIGNAL_HOST.search(url) and not _SIGNAL_SKIP.search(url):
                # Buffer; the body is fetched in drain_bodies() (NOT here) to avoid
                # blocking/re-entering the event dispatcher.
                self.sig_pending.append((resp, url, round(time.monotonic() - self.t0, 3)))
        except Exception:  # noqa: BLE001
            pass

    def drain_bodies(self):
        """Fetch buffered signaling response bodies. Called from the watch loop, not
        the event handler. Uses Playwright Response.body() so cross-origin Meet
        IFRAME (OOPIF) responses are captured — a raw page CDP session misses those."""
        if not self.sig_pending:
            return
        batch, self.sig_pending = self.sig_pending[:], []
        for resp, url, t_req in batch:
            try:
                raw = resp.body()                # bytes; raises if no body / streamed
            except Exception:  # noqa: BLE001
                continue
            try:
                prev = raw.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                prev = repr(raw[:700])
            self.log("sig_body", url=url, t_req=t_req, prev=prev[:700])

    def attach_ws(self, ws):
        try:
            self.log("ws_open", url=ws.url)
            ws.on("framereceived",
                  lambda payload: self.log("ws_recv", url=ws.url, **_frame_preview(payload)))
            ws.on("framesent",
                  lambda payload: self.log("ws_sent", url=ws.url, **_frame_preview(payload)))
            ws.on("close", lambda *a: self.log("ws_close", url=ws.url))
        except Exception:  # noqa: BLE001
            pass

    def attach_page(self, pg):
        try:
            pg.on("websocket", self.attach_ws)
        except Exception:  # noqa: BLE001
            pass


def _short(url: str, n: int = 110) -> str:
    """Drop the query for readability; the path is what identifies an RPC."""
    base = url.split("?", 1)[0]
    return base if len(base) <= n else base[: n - 1] + "…"


def _rpc_name(url: str) -> str:
    """meet.google.com/$rpc/google.rtc.meetings.v1.MeetingDeviceService/Update… →
    'MeetingDeviceService/UpdateMeetingDevice'."""
    tail = url.split("/$rpc/", 1)[-1].split("?", 1)[0]
    parts = tail.rsplit(".", 1)
    return parts[-1] if parts else tail


def _decode_meet(prev: str) -> str:
    """Meet $rpc bodies are base64 protobuf — decode to printable ASCII so the
    participant names / 'spaces/…/devices/…' / state strings are readable."""
    try:
        raw = base64.b64decode(prev + "==")
    except Exception:  # noqa: BLE001
        return prev[:160]
    return "".join(chr(c) if 32 <= c < 127 else "·" for c in raw)[:200]


def _report(cap: Capture, marker_m, args) -> None:
    """Print the end-of-call correlation: events around the leave-control-gone
    marker, so the hang-up signal is visible (inbound event just before it)."""
    evs = cap.events
    ws_recv = [e for e in evs if e["kind"] == "ws_recv"]
    ws_sent = [e for e in evs if e["kind"] == "ws_sent"]
    ws_open = [e for e in evs if e["kind"] == "ws_open"]
    print("\n" + "=" * 72)
    print(f"NETWORK SUMMARY  ({len(evs)} events → {cap.out_path})")
    print(f"  requests={sum(1 for e in evs if e['kind']=='req')}  "
          f"responses={sum(1 for e in evs if e['kind']=='resp')}  "
          f"ws_open={len(ws_open)}  ws_recv={len(ws_recv)}  ws_sent={len(ws_sent)}")
    if ws_open:
        print("  WebSocket endpoints seen:")
        for e in dict.fromkeys(_short(e["url"], 140) for e in ws_open):
            print(f"    - {e}")

    if marker_m is None:
        print("\n  (no leave-control-gone marker captured — call wasn't established, "
              "or ended via cap/Ctrl+C before a clean DOM end. Inspect the JSONL.)")
        print("=" * 72)
        return

    lo, hi = marker_m - args.window_before, marker_m + args.window_after
    win = [e for e in evs if lo <= e["m"] <= hi and e["kind"] != "req"]
    print(f"\nEVENTS AROUND HANG-UP MARKER (m={marker_m:.3f}s, "
          f"window [-{args.window_before:.0f}s,+{args.window_after:.0f}s]):")
    print("  dt(s)   kind      detail")
    for e in win:
        dt = e["m"] - marker_m
        if e["kind"] in ("ws_recv", "ws_sent"):
            detail = f"{_short(e['url'],60)}  len={e['len']} {e['enc']} {e['prev'][:80]!r}"
        elif e["kind"] == "resp":
            detail = f"[{e['status']}] {_short(e['url'])}"
        elif e["kind"] == "sig_body":
            detail = f"{_short(e['url'],55)}  body={e['prev'][:90]!r}"
        elif e["kind"] in ("ws_open", "ws_close"):
            detail = _short(e["url"], 80)
        elif e["kind"] == "mark":
            detail = e.get("what", "")
        else:
            detail = ""
        flag = "  <== just before marker" if -3.0 <= dt < 0 else ""
        print(f"  {dt:+6.2f}  {e['kind']:<9} {detail}{flag}")

    # The hang-up signal: call-state bodies (decoded) just before the marker, with
    # webchannel heartbeats filtered out so the real participant-leave frames show.
    cand = []
    for e in win:
        dt = e["m"] - marker_m
        if not (-8.0 <= dt < 2.0):    # the wire delta can precede the DOM banner
            continue
        if e["kind"] == "sig_body" and "/$rpc/" in e["url"]:
            cand.append((dt, _rpc_name(e["url"]), _decode_meet(e["prev"])))
        elif e["kind"] == "sig_body" and "webchannel" in e["url"]:
            if not _WC_NOISE.search(e["prev"]):
                cand.append((dt, "webchannel/events", e["prev"][:180]))
    print("\nCANDIDATE HANG-UP SIGNAL (decoded call-state bodies in [-4s,+1s] of "
          "marker; webchannel heartbeats filtered):")
    if cand:
        for dt, name, body in cand:
            print(f"  {dt:+.2f}s  {name:<34} {body}")
        print("\n  → The participant-leave signal is the SyncMeetingSpaceCollections / "
              "MeetingDeviceService body whose decoded text shows the CALLEE's "
              "device (e.g. 'Tran Duc' / 'devices/<n>') going to a left/removed state.")
    else:
        print("  (no decoded call-state bodies in window — the leave likely arrived on "
              "an open SyncMeetingSpaceCollections stream; widen --window-before and "
              "inspect 'sig_body' rows for that endpoint in the JSONL.)")
    print("=" * 72)


def _watch_report(cap: Capture, args) -> None:
    """Watch-only mode has no automated DOM marker (a human drives the call), so we
    dump EVERY call-state body chronologically by request time and decoded. The
    callee's JOIN is a ``CreateMeetingDevice`` naming them + their device appearing
    in a ``SyncMeetingSpaceCollections`` roster; the HANG-UP is a later
    ``SyncMeetingSpaceCollections`` delta that REMOVES that device — that body's
    timestamp is the signal we're after."""
    rows = []
    for e in cap.events:
        if e["kind"] != "sig_body":
            continue
        url, prev = e["url"], e.get("prev", "")
        t = e.get("t_req", e["m"])
        if "/$rpc/" in url:
            rows.append((t, _rpc_name(url), _decode_meet(prev)))
        elif "webchannel" in url and not _WC_NOISE.search(prev):
            rows.append((t, "webchannel/events", prev[:150]))
    rows.sort(key=lambda r: r[0])
    print("\n" + "=" * 72)
    print(f"WATCH-ONLY CAPTURE  ({len(cap.events)} events → {cap.out_path})")
    print(f"  {len(rows)} call-state bodies (Meet $rpc + webchannel), chronological "
          "by request time:")
    print("  t_req     rpc                                      decoded body (head)")
    for t, name, body in rows:
        print(f"  {t:7.2f}s  {name:<40} {body[:110]}")
    joins = [(t, b) for t, n, b in rows if "CreateMeetingDevice" in n]
    print(f"\n  device JOINs seen (CreateMeetingDevice): {len(joins)}")
    for t, b in joins:
        who = b.split("·", 1)[0][:60]
        print(f"    {t:7.2f}s  {who}  …")
    print("\n  → Find the CALLEE ('Duc Tran Trong') in a CreateMeetingDevice / roster")
    print("    (= they joined); the HANG-UP is the later SyncMeetingSpaceCollections")
    print("    body whose roster no longer carries that device.")
    print("=" * 72)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="call_network_capture",
        description="Place a ringing Chat call and capture network to find the "
        "hang-up signal.",
    )
    p.add_argument("--url", default="https://chat.google.com/u/1/app/chat/qtotjoAAAAE",
                   help="Chat DM/space deep link to call into (default: bot↔Duc DM on u/1).")
    p.add_argument("--cdp-url", default="http://127.0.0.1:9222",
                   help="attach to the daily Brave over CDP (proven path).")
    p.add_argument("--authuser", type=int, default=1,
                   help="signed-in account index (u/1 = mikmikb26; default 1).")
    p.add_argument("--button-name", default="",
                   help="exact call-button accessible name if auto-detect misses it.")
    p.add_argument("--duration", type=float, default=240.0,
                   help="MAX seconds to stay; exits early on call end (default 240).")
    p.add_argument("--load-timeout", type=float, default=45.0,
                   help="seconds to wait for the DM + call button (default 45).")
    p.add_argument("--no-call", action="store_true",
                   help="open the DM + attach capture but DON'T ring (safe dry pass).")
    p.add_argument("--probe-secs", type=float, default=15.0,
                   help="with --no-call, seconds to capture idle network (default 15).")
    p.add_argument("--watch-only", action="store_true",
                   help="DON'T automate the call — just attach + record network across "
                   "ALL tabs while YOU place and HOLD the call manually in Brave. This "
                   "is the reliable path: a human keeps the call alive long enough for "
                   "the callee to answer (e.g. on a phone) and then hang up, so the "
                   "callee's JOIN and LEAVE roster deltas are both captured.")
    p.add_argument("--out", default="",
                   help="JSONL output path (default reports/call_network/capture-<ts>.jsonl).")
    p.add_argument("--window-before", type=float, default=12.0,
                   help="seconds before the hang-up marker to report (default 12).")
    p.add_argument("--window-after", type=float, default=5.0,
                   help="seconds after the hang-up marker to report (default 5).")
    p.add_argument("--keep-open", action="store_true",
                   help="don't auto-leave; stay until Ctrl+C.")
    args = p.parse_args(argv)

    if not args.out:
        ts = time.strftime("%Y%m%d-%H%M%S")
        args.out = os.path.join(_REPO_ROOT, "reports", "call_network", f"capture-{ts}.jsonl")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: Playwright not installed → conda run -n igaming pip install playwright",
              file=sys.stderr)
        return 2

    print(f"=== call + network capture → {args.url} ===")
    print(f"  CDP: {args.cdp_url}   out: {args.out}")

    cap = Capture(args.out)
    marker_m = None
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp_url)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: could not attach to Brave at {args.cdp_url}: {exc}\n"
                  "  Launch it with: brave-browser --remote-debugging-port=9222",
                  file=sys.stderr)
            cap.close()
            return 1
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        # Pre-grant A/V so no permission prompt blocks the call.
        try:
            context.grant_permissions(["microphone", "camera"],
                                      origin="https://meet.google.com")
        except Exception:  # noqa: BLE001
            pass

        # Wire capture: context-level req/resp (covers iframes), per-page websockets.
        context.on("request", cap.on_request)
        context.on("response", cap.on_response)
        context.on("page", cap.attach_page)
        for pg in context.pages:
            cap.attach_page(pg)

        opened = []                                   # pages WE opened → close on exit
        page = context.new_page()
        opened.append(page)
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: navigation failed: {exc}", file=sys.stderr)
            cap.close()
            return 1

        # --- watch-only: record everything, human drives the call ----------------
        if args.watch_only:
            cap.log("mark", what="watch_only_start")
            secs = args.duration
            print("\n" + "=" * 72)
            print("WATCH-ONLY: capture is attached to the WHOLE browser (all tabs).")
            print(f"  → In Brave, click 'Start a video call' in the DM and STAY in the")
            print(f"    Meet (don't close it). Have the callee ANSWER, wait a few")
            print(f"    seconds, then HANG UP.  Recording for up to {secs:.0f}s "
                  "(Ctrl+C to stop early).")
            print("=" * 72, flush=True)
            deadline = time.monotonic() + secs
            try:
                while time.monotonic() < deadline:
                    cap.drain_bodies()
                    n = sum(1 for e in cap.events
                            if e["kind"] == "sig_body" and "/$rpc/" in e["url"])
                    print(f"\r  …recording  meet-rpc bodies={n}  "
                          f"events={len(cap.events)}   ", end="", flush=True)
                    page.wait_for_timeout(1000)
            except KeyboardInterrupt:
                print("\n   Ctrl+C — stopping capture.")
                cap.log("mark", what="ctrl_c")
            cap.drain_bodies()
            cap.log("mark", what="watch_only_end")
            # leave the human's call alone; close only nothing (we opened just `page`).
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            cap.close()
            _watch_report(cap, args)
            print("✅ Done.")
            return 0

        # Wait for the DM + call button.
        deadline = time.monotonic() + args.load_timeout
        button = None
        while time.monotonic() < deadline:
            button = mcb._find_call_button(page, args.button_name)
            if button is not None:
                break
            page.wait_for_timeout(1000)
        if button is None:
            print("\nERROR: call button not found.", file=sys.stderr)
            print(f"  url={page.url}  title={page.title()!r}", file=sys.stderr)
            for n in mcb._dump_buttons(page):
                print(f"    - {n!r}", file=sys.stderr)
            cap.close()
            return 1
        label = (button.get_attribute("aria-label") or button.inner_text() or "call").strip()
        print(f"  found call control: {label!r}")

        # --- dry pass: no ring, just probe idle network --------------------------
        if args.no_call:
            cap.log("mark", what="no_call_probe_start")
            print(f"  --no-call: capturing {args.probe_secs:.0f}s of idle network "
                  "(NOT ringing) …")
            page.wait_for_timeout(int(args.probe_secs * 1000))
            cap.drain_bodies()  # exercise body capture + preview idle signaling payloads
            cap.log("mark", what="no_call_probe_end")
            for p in opened:
                try:
                    p.close()
                except Exception:  # noqa: BLE001
                    pass
            cap.close()
            _report(cap, None, args)
            return 0

        # --- click the call button → opens Meet pre-join (popup or in-place) ------
        cap.log("mark", what="call_button_click")
        call_page = page
        try:
            with context.expect_page(timeout=8_000) as popup:
                button.click()
            call_page = popup.value
            opened.append(call_page)
        except Exception:  # noqa: BLE001 - no popup => navigated in-place
            try:
                button.click()
            except Exception:  # noqa: BLE001
                pass
            for pg in context.pages:
                if "meet.google.com" in (pg.url or ""):
                    call_page = pg
                    break
        try:
            call_page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass

        # Signaling-body capture happens in cap.on_response (buffers) + cap.drain_bodies
        # (fetches) — both via Playwright's OOPIF-aware machinery, since the in-DM Meet
        # UI renders in a cross-origin meet.google.com IFRAME, not a popup.

        # --- click "Join now" → this RINGS the callee ----------------------------
        joined = False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not joined:
            jb = mcb._find_button_in_frames(call_page, _JOIN_PATTERNS)
            if jb is not None:
                try:
                    jlabel = jb.get_attribute("aria-label") or jb.inner_text() or "Join"
                    jb.click()
                    joined = True
                    cap.log("mark", what=f"join_clicked:{jlabel}")
                    print(f"  clicked {jlabel!r} → ringing")
                except Exception:  # noqa: BLE001
                    pass
            if not joined:
                call_page.wait_for_timeout(1000)
        if not joined:
            print("  (no 'Join now' seen — may already be ringing)")
        print(f"\n📞 RINGING the callee. Call page: {call_page.url}")
        print("   → ANSWER on the other account, then HANG UP to capture the signal.")

        # --- confirm in-call (leave control present) → baseline for end-detection -
        in_call = False
        connect_deadline = time.monotonic() + 25
        while time.monotonic() < connect_deadline:
            if mcb._in_call(call_page):
                in_call = True
                break
            call_page.wait_for_timeout(1000)
        cap.log("mark", what=f"in_call_confirmed={in_call}")
        print("   ✅ in-call controls present — watching for hang-up …" if in_call else
              "   ⚠️  couldn't confirm in-call controls — will hold to the cap.")

        # --- watch for the leave control to vanish (= call ended) ----------------
        # Two distinct end signals (a 1:1 Meet call does NOT auto-remove the caller
        # when the REMOTE hangs up — the caller lingers alone — so the leave control
        # vanishing only detects OUR-side leave; the remote hangup shows as the
        # "you're the only one here" alone banner + a SyncMeetingSpaceCollections
        # roster delta on the wire):
        #   t_gone  — our leave control disappeared (we left / call torn down)
        #   t_alone — alone banner appeared (the CALLEE hung up) ← the target signal
        gone_for = alone_for = 0
        t_gone = t_alone = None
        cap_deadline = None if args.keep_open else time.monotonic() + args.duration
        ended_reason = None
        try:
            while cap_deadline is None or time.monotonic() < cap_deadline:
                if in_call:
                    present = mcb._in_call(call_page)
                    if not present:                       # WE left / call torn down
                        if t_gone is None:
                            t_gone = time.monotonic() - cap.t0
                            cap.log("mark", what="leave_control_first_absent")
                        gone_for += 1
                        if gone_for >= 3:                  # debounce a re-render
                            ended_reason = "our leave control disappeared (we left)"
                            cap.log("mark", what="call_ended_confirmed")
                            break
                    else:
                        if t_gone is not None:
                            cap.log("mark", what="leave_control_reappeared")
                        gone_for = 0
                        t_gone = None
                        alone = mcb._alone_signal(call_page)
                        if alone:                          # REMOTE left → we're alone
                            if t_alone is None:
                                t_alone = time.monotonic() - cap.t0
                                cap.log("mark", what=f"alone_signal:{alone[:40]}")
                            alone_for += 1
                            if alone_for >= 3:
                                ended_reason = f"callee hung up — we're alone ({alone[:40]})"
                                cap.log("mark", what="remote_hangup_confirmed")
                                break
                        else:
                            alone_for = 0
                            t_alone = None
                cap.drain_bodies()
                call_page.wait_for_timeout(1000)
        except KeyboardInterrupt:
            print("\n   Ctrl+C — stopping capture.")
            cap.log("mark", what="ctrl_c")
        cap.drain_bodies()  # final sweep for the hang-up payload

        # The remote-hangup (alone) marker is the one we want; fall back to our-leave.
        marker_m = t_alone if t_alone is not None else t_gone
        if ended_reason:
            print(f"\n📴 {ended_reason}.")
        elif not args.keep_open:
            print(f"\n⏱  Reached the {args.duration:.0f}s cap with no hang-up detected.")

        # Best-effort hang up our side; close only the tabs WE opened.
        leave = mcb._find_button_in_frames(call_page, mcb._IN_CALL_PATTERNS)
        if leave is not None:
            try:
                leave.click()
                call_page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001
                pass
        for p in opened:
            try:
                p.close()
            except Exception:  # noqa: BLE001
                pass

    cap.close()
    _report(cap, marker_m, args)
    print("✅ Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
