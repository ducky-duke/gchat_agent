#!/usr/bin/env python3
"""DIAGNOSTIC: place a Chat DM call via CDP and LOG the full call state every second
— through the callee's hang-up — so we can see EXACTLY which signal flips when a call
ends, and build reliable hang-up detection from real data instead of guesses.

It captures, per second, to stderr + /tmp/call_dom.log:
  * frame list (so we see the meet.google.com call iframe attach / DETACH),
  * every visible button whose label looks call-related (leave/join/end/hang/mic/cam),
  * the call iframe's innerText head (the real 'Call ended' / 'you're the only one'
    wording, if any),
  * a WebRTC hook's view: __callEnded + the RTCPeerConnection connectionState history
    (the definitive end signal — the peer connection closes when the call ends).
Also logs frame attached/detached EVENTS with timestamps.

Run (after `brave-browser --remote-debugging-port=9222`, signed in as mikmikb26 at
authuser 1):
    conda run --no-capture-output -n igaming python -u call/diag/diag_call_dom.py

Then on the callee device: PICK UP, wait a few seconds, HANG UP. Watch which line(s)
change at the hang-up. Ctrl+C to stop early. This RINGS a real person — demo only.
"""
from __future__ import annotations

import re
import sys
import time

sys.path.insert(0, "src")

CDP = "http://127.0.0.1:9222"
URL = "https://chat.google.com/u/1/app/chat/qtotjoAAAAE"
LOGFILE = "/tmp/call_dom.log"
DURATION = 150  # seconds to observe (Ctrl+C to stop sooner)

# Install BEFORE the call iframe loads: wrap RTCPeerConnection so we can read the
# connection state history later. The PC closing is the true 'call ended' signal.
HOOK = """
(() => {
  try {
    if (window.__ce_installed) return;
    window.__ce_installed = true;
    window.__callEnded = false;
    window.__pcStates = [];
    const O = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (!O) return;
    const W = function(...a) {
      const pc = new O(...a);
      const note = (tag) => {
        const s = pc.connectionState + '/' + pc.iceConnectionState;
        window.__pcStates.push(tag + ':' + s);
        if (['closed','failed'].includes(pc.connectionState) ||
            ['closed','failed'].includes(pc.iceConnectionState)) window.__callEnded = true;
      };
      try { pc.addEventListener('connectionstatechange', () => note('conn')); } catch(e){}
      try { pc.addEventListener('iceconnectionstatechange', () => note('ice')); } catch(e){}
      return pc;
    };
    W.prototype = O.prototype;
    window.RTCPeerConnection = W;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = W;
  } catch(e) {}
})();
"""

_REL = re.compile(r"call|leave|join|end|hang|mic|microph|camera|cam|present|meet|"
                  r"ringing|connect|only one|no one", re.I)

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


def relevant_buttons(page):
    out = []
    for fr in _frames(page):
        try:
            for h in fr.get_by_role("button").all()[:250]:
                try:
                    if not h.is_visible():
                        continue
                    n = (h.get_attribute("aria-label") or h.inner_text() or "").strip()
                except Exception:
                    continue
                if n and _REL.search(n):
                    out.append(n)
        except Exception:
            continue
    # de-dup, preserve order
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def meet_state(page):
    """(innerText head, __callEnded, pcStates) from the meet/call frame(s)."""
    texts, ended, states = [], False, []
    for fr in _frames(page):
        u = fr.url or ""
        if "meet.google.com" not in u and "chat.google.com" not in u:
            continue
        try:
            e = fr.evaluate("window.__callEnded === true")
            if e:
                ended = True
            s = fr.evaluate("window.__pcStates || []")
            if s:
                states += [f"{u[:24]}:{x}" for x in s[-6:]]
        except Exception:
            pass
        if "meet.google.com" in u:
            try:
                t = fr.evaluate("document.body ? document.body.innerText.slice(0,240) : ''")
                t = " ".join((t or "").split())
                if t:
                    texts.append(t[:240])
            except Exception:
                pass
    return texts, ended, states


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
        page.on("frameattached", lambda f: log(f"FRAME ATTACHED  {(f.url or '')[:80]!r}"))
        page.on("framedetached", lambda f: log(f"FRAME DETACHED  {(f.url or '')[:80]!r}"))
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

        # click Join now across all frames
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

        log(">>> PICK UP on the callee device, wait, then HANG UP. Watching state …")
        try:
            for _ in range(DURATION):
                btns = relevant_buttons(page)
                texts, ended, states = meet_state(page)
                fr_urls = [(_f.url or "")[:50] for _f in _frames(page)]
                log(f"frames={len(fr_urls)} pcEnded={ended} btns={btns}")
                if states:
                    log(f"    pcStates={states}")
                if texts:
                    log(f"    meetText={texts}")
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
