#!/usr/bin/env python3
"""DIAGNOSTIC: place a Chat DM call via CDP and LOG every JOIN-relevant signal each
second — through the callee ANSWERING — so we can see EXACTLY which signal flips the
instant the remote participant joins, then build real-time join detection from real
data instead of guessing Meet's obfuscated DOM. Sibling of diag_call_dom.py (which
did the same for the hang-up signal).

Captured per second (to stderr + /tmp/call_join.log):
  * WebRTC view (init-script hook installed BEFORE the call iframe loads):
      - __pcStates    : RTCPeerConnection connection/ice state history (when WE connect)
      - __remoteTracks: count of inbound `track` events (a remote participant's media
                        appears on the PC when they join → the cleanest WebRTC join cue)
      - __trackLog    : kind:id of each remote track added
  * DOM roster probes in the meet frame (candidate join signals; the one that goes
    1→2 the moment the callee answers is our detector):
      - [data-participant-id] / [data-requested-participant-id] / [data-allocation-index]
        element counts (Meet renders one per participant tile),
      - the first aria-label that reads like a participant COUNT ("2 participants", …),
      - any visible "<name> joined" announcement text.

Run (after `brave-browser --remote-debugging-port=9222`, signed in as mikmikb26 at
authuser 1):
    python call/diag/diag_call_join.py

Then on the CALLEE device (the Duc account): ANSWER the ring, STAY ~20s, then hang up.
Watch which line(s) change at the moment of answering. Ctrl+C to stop early.
⚠️ This RINGS a real person — demo accounts only.
"""
from __future__ import annotations

import re
import sys
import time

sys.path.insert(0, "src")

CDP = "http://127.0.0.1:9222"
URL = "https://chat.google.com/u/1/app/chat/qtotjoAAAAE"   # bot↔Duc DM, authuser 1
LOGFILE = "/tmp/call_join.log"
DURATION = 75  # seconds to observe (Ctrl+C to stop sooner)

# Install BEFORE the call iframe loads: wrap RTCPeerConnection to record connection
# state AND count inbound remote tracks (a remote participant's media lands as a
# `track` event the moment they join — the WebRTC-level join signal).
HOOK = """
(() => {
  try {
    if (window.__cj_installed) return;
    window.__cj_installed = true;
    window.__pcStates = [];
    window.__remoteTracks = 0;
    window.__trackLog = [];
    const O = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (!O) return;
    const W = function(...a) {
      const pc = new O(...a);
      const note = (tag) => {
        window.__pcStates.push(tag + ':' + pc.connectionState + '/' + pc.iceConnectionState);
      };
      try { pc.addEventListener('connectionstatechange', () => note('conn')); } catch(e){}
      try { pc.addEventListener('iceconnectionstatechange', () => note('ice')); } catch(e){}
      try {
        pc.addEventListener('track', (ev) => {
          window.__remoteTracks++;
          const t = (ev && ev.track) || {};
          window.__trackLog.push((t.kind || '?') + ':' + ((t.id || '') + '').slice(0,8));
        });
      } catch(e){}
      return pc;
    };
    W.prototype = O.prototype;
    window.RTCPeerConnection = W;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = W;
  } catch(e) {}
})();
"""

# Run in the meet frame each second to count participant tiles + read a count label.
ROSTER_PROBE = """
(() => {
  const q = (s) => { try { return document.querySelectorAll(s).length; } catch(e){ return -1; } };
  const out = {
    dpid: q('[data-participant-id]'),
    rpid: q('[data-requested-participant-id]'),
    alloc: q('[data-allocation-index]'),
    countLabel: null,
    joinedText: null,
  };
  try {
    for (const el of document.querySelectorAll('[aria-label]')) {
      const a = el.getAttribute('aria-label') || '';
      if (/\\b\\d+\\b.*(participant|people|in (this|the) call|others)/i.test(a)) {
        out.countLabel = a.slice(0, 70); break;
      }
    }
  } catch(e){}
  try {
    const bt = (document.body ? document.body.innerText : '') || '';
    const m = bt.match(/[^\\n]{0,40}\\bjoined\\b[^\\n]{0,20}/i);
    if (m) out.joinedText = m[0].trim().slice(0, 70);
  } catch(e){}
  return out;
})()
"""

_log_fh = open(LOGFILE, "w")
_t0 = time.monotonic()


def log(msg: str) -> None:
    line = f"[{time.monotonic() - _t0:6.1f}s] {msg}"
    print(line, flush=True)
    _log_fh.write(line + "\n")
    _log_fh.flush()


def _frames(page):
    try:
        return list(page.frames)
    except Exception:
        return [page]


def webrtc_state(page):
    """(remoteTracks, trackLog tail, pcStates tail) across meet/chat frames."""
    rt, tlog, states = 0, [], []
    for fr in _frames(page):
        u = fr.url or ""
        if "meet.google.com" not in u and "chat.google.com" not in u:
            continue
        try:
            rt = max(rt, int(fr.evaluate("window.__remoteTracks || 0")))
        except Exception:
            pass
        try:
            tl = fr.evaluate("window.__trackLog || []")
            if tl:
                tlog += tl[-6:]
        except Exception:
            pass
        try:
            s = fr.evaluate("window.__pcStates || []")
            if s:
                states += [x for x in s[-5:]]
        except Exception:
            pass
    return rt, tlog, states


def roster_state(page):
    """The ROSTER_PROBE dict from the meet frame(s) (merged: max counts seen)."""
    merged = {"dpid": 0, "rpid": 0, "alloc": 0, "countLabel": None, "joinedText": None}
    for fr in _frames(page):
        u = fr.url or ""
        if "meet.google.com" not in u:
            continue
        try:
            d = fr.evaluate(ROSTER_PROBE)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        for k in ("dpid", "rpid", "alloc"):
            try:
                merged[k] = max(merged[k], int(d.get(k) or 0))
            except Exception:
                pass
        merged["countLabel"] = merged["countLabel"] or d.get("countLabel")
        merged["joinedText"] = merged["joinedText"] or d.get("joinedText")
    return merged


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: pip install playwright", file=sys.stderr)
        return 2
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0]
        try:
            ctx.add_init_script(HOOK)  # applies to frames created after this point
        except Exception as e:
            log(f"add_init_script failed: {e}")
        page = ctx.new_page()
        log(f"goto {URL}")
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)

        # click the call button
        clicked = False
        for pat in (r"start a video call", r"start a call", r"video call", r"^call$"):
            try:
                loc = page.get_by_role("button", name=re.compile(pat, re.I))
                if loc.count() and loc.first.is_visible():
                    loc.first.click()
                    log(f"clicked call button matching {pat!r}")
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            log("could NOT find the call button — aborting")
            return 1
        page.wait_for_timeout(3000)

        # click Join now across all frames (the caller commits → it rings the callee)
        joined = False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not joined:
            for fr in _frames(page):
                for pat in (r"join now", r"^join$", r"join call", r"ask to join"):
                    try:
                        jb = fr.get_by_role("button", name=re.compile(pat, re.I))
                        if jb.count() and jb.first.is_visible():
                            jb.first.click()
                            log(f"clicked Join matching {pat!r}")
                            joined = True
                            break
                    except Exception:
                        continue
                if joined:
                    break
            if not joined:
                page.wait_for_timeout(1000)
        if not joined:
            log("no Join button seen (maybe already ringing)")

        log(">>> ANSWER the ring on the CALLEE (Duc) device, STAY ~20s, then hang up.")
        log(">>> Watching for the JOIN signal (remoteTracks / tile count 1→2) …")
        prev = None
        try:
            for _ in range(DURATION):
                rt, tlog, states = webrtc_state(page)
                r = roster_state(page)
                snap = (rt, r["dpid"], r["rpid"], r["alloc"], r["countLabel"], r["joinedText"])
                changed = " <== CHANGED" if snap != prev else ""
                log(f"remoteTracks={rt} dpid={r['dpid']} rpid={r['rpid']} "
                    f"alloc={r['alloc']} count={r['countLabel']!r} "
                    f"joined={r['joinedText']!r}{changed}")
                if changed and tlog:
                    log(f"    trackLog={tlog}")
                if changed and states:
                    log(f"    pcStates={states}")
                prev = snap
                time.sleep(1)
        except KeyboardInterrupt:
            log("Ctrl+C — stopping observation")

        # leave + close our tab
        for fr in _frames(page):
            for pat in (r"leave call", r"leave the call", r"end call", r"hang up"):
                try:
                    lb = fr.get_by_role("button", name=re.compile(pat, re.I))
                    if lb.count() and lb.first.is_visible():
                        lb.first.click()
                        break
                except Exception:
                    continue
        try:
            page.close()
        except Exception:
            pass
        log(f"done — full log at {LOGFILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
