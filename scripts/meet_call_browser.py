#!/usr/bin/env python3
"""Place the *native ringing* Google Chat call by driving a real logged-in browser
with Playwright — the "trick" path, because no public API can ring someone (only a
human clicking the call button in the Chat UI produces the ring; see the project
notes / docs/google_meet/).

What it does
------------
1. Opens a real Chromium-family browser (your system **Brave**) signed into Google,
   reusing a PERSISTENT profile so the login survives across runs and never fights
   your daily browser.
2. Navigates to a Google Chat DM/space.
3. Finds and clicks the **call** button → that fires Meet's internal
   ``MeetingSpaceService/ResolveForHangoutsChat`` RPC and RINGS the other person,
   exactly like clicking it by hand (Chrome generates the protobuf + SAPISIDHASH
   auth itself — far more robust than replaying the raw RPC).
4. Stays in the call, but EXITS THE MOMENT the call ends — when the callee hangs
   up (or declines / never answers) the call UI's leave control disappears / an
   end banner shows, and the script detects that and stops. ``--duration`` is a
   MAX cap, not a fixed wait (``--keep-open`` removes the cap → until Ctrl+C).

This is the browser/ring half. The AI **voice** half (capture the Meet's audio +
inject TTS so the AI talks on the call) is NOT here — that's virtual PulseAudio
devices + the Gemini Live loop from ``scripts/demo_incident_call.py`` pointed at
those devices. See the "AUDIO (next phase)" note at the bottom. Reason: audio is
WebRTC; Playwright drives the page, not the media — but this script is the WebRTC
client that must be *in* the call for that audio to flow, so it's the foundation.

⚠️  Reality check (do not be misled): this automates the Google web UI, which
violates Google's ToS and can get an account flagged. Use only the throwaway demo
accounts. Selectors into Google's obfuscated DOM are brittle by nature — if the
call button isn't found, the script DUMPS every visible button label so you can
pass the exact one via ``--button-name``.

First-time setup
----------------
  conda run -n igaming pip install playwright      # the system Brave is reused,
                                                   # so NO `playwright install` needed

First run opens an EMPTY profile → log into Google (the demo account) in the
window that appears, open the target DM once, then re-run. The cookies persist in
``--profile-dir`` so later runs are already signed in.

Run
---
  # 1) sign in once (headed), confirm the DM loads, find the button:
  python scripts/meet_call_browser.py --dry-run

  # 2) place the real ringing call (default target = GOOGLE_VOICE_SPACE = the
  #    bot↔Duc DM), ring for 90s:
  python scripts/meet_call_browser.py --duration 90

  # most reliable targeting: open the DM in the window, copy the address-bar URL,
  # and pass it verbatim:
  python scripts/meet_call_browser.py --url 'https://chat.google.com/u/0/...'

  # reuse your DAILY Brave session instead of a dedicated profile (quit Brave, then
  # `brave-browser --remote-debugging-port=9222`, then):
  python scripts/meet_call_browser.py --cdp-url http://127.0.0.1:9222

Exit codes: 0 ok · 2 setup error (Playwright missing / no target) · 1 runtime
error (button not found / navigation failed).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request

# Allow running straight from a checkout without installing the package.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

# Browser choices Playwright can drive. "chromium" = Playwright's OWN bundled
# Chromium (executable_path=None → no system install needed). The others are
# system binaries. --browser-path overrides any of these.
_BROWSER_PATHS = {
    "brave": "/usr/bin/brave-browser",
    "chrome": "/usr/bin/google-chrome",
    "chromium": None,  # Playwright-bundled Chromium
}
DEFAULT_BROWSER = "chromium"
# Persistent automation profiles — kept OUT of your daily Brave dir so the two never
# lock each other. Gitignored (they hold live Google cookies → treat as secrets).
# One per browser family, since a Brave-written profile and a Chromium-written one
# aren't interchangeable.
DEFAULT_PROFILE_DIRS = {
    "brave": os.path.join(_REPO_ROOT, ".browser-profile"),
    "chrome": os.path.join(_REPO_ROOT, ".chrome-profile"),
    "chromium": os.path.join(_REPO_ROOT, ".chromium-profile"),
}

# Ordered aria-label / accessible-name patterns for the Chat DM "call" control.
# Google's labels drift; first visible match wins. Override with --button-name.
_CALL_BUTTON_PATTERNS = (
    r"start a video call",
    r"start a call",
    r"video call",
    r"^call$",
    r"\bcall\b",
    r"start a huddle",
    r"start huddle",
    r"\bhuddle\b",
    r"\bmeet\b",
)

# A visible hang-up/leave control means the call is still ACTIVE — used both to
# confirm we connected and (by its disappearance) to detect the call dropping.
_IN_CALL_PATTERNS = (
    r"leave call",
    r"leave the call",
    r"end call",
    r"hang up",
)
# IMPORTANT: end-state TEXT ("Call ended", "Missed call", …) is UNRELIABLE here —
# a Chat DM renders call-HISTORY cards with that exact text in the SAME frame as the
# live call (verified: 4 visible "Call ended" + 6 "missed" cards in an idle DM). So
# the PRIMARY end signal is the leave/hang-up control DISAPPEARING (it only exists
# during a live call, never in a history card). These patterns are only a SECONDARY
# signal for the rare 1:1 case where the callee leaves but we aren't auto-dropped and
# linger alone — wording specific enough not to occur in a history card.
# 'you.?re' tolerates a straight or curly apostrophe (or none) in "you're".
_ALONE_PATTERNS = (
    r"you.?re the only one",
    r"no one else is here",
)


def _resolve_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(_REPO_ROOT, path)


# A Meet meeting code is 3-4-3 lowercase letters: e.g. abc-mnop-xyz. The 2nd form
# pins it to a meet.google.com URL (with an optional path segment like /lookup/ or
# /call/) so we don't false-match arbitrary 3-4-3 text on the page.
_MEETING_CODE_RE = re.compile(r"\b([a-z]{3}-[a-z]{4}-[a-z]{3})\b")
_MEET_URL_CODE_RE = re.compile(
    r"meet\.google\.com/(?:[a-z_]+/)?([a-z]{3}-[a-z]{4}-[a-z]{3})\b"
)


def _extract_meeting_code(url: str) -> "str | None":
    """Pull the abc-mnop-xyz meeting code out of a meet.google.com call URL. The URL
    is short and trusted, so a meet-URL match is preferred but a bare code is fine."""
    if not url:
        return None
    m = _MEET_URL_CODE_RE.search(url) or _MEETING_CODE_RE.search(url)
    return m.group(1) if m else None


def _scan_page_for_code(page) -> "str | None":
    """Fallback when the code isn't in the address bar: scan the call page (all
    frames) for a meet.google.com link, then the page HTML, for a meeting code."""
    for fr in _frames(page):
        try:
            for a in fr.get_by_role("link").all()[:80]:
                href = a.get_attribute("href") or ""
                m = _MEET_URL_CODE_RE.search(href) or _MEETING_CODE_RE.search(href)
                if m:
                    return m.group(1)
        except Exception:  # noqa: BLE001 - best-effort scan, never raise
            continue
    try:
        m = _MEET_URL_CODE_RE.search(page.content())
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return None


def _start_rest_watch(cfg, args, meeting_code: "str | None"):
    """Spin up the Meet REST room-data watch in a DAEMON thread. It is urllib-only
    (reuses scripts/meet_rest_watch), so it never touches Playwright objects from the
    main thread — only the OAuth token + the Meet REST API. Returns (stop_event,
    thread); (None, None) if the watcher can't be started."""
    import threading
    try:
        import meet_rest_watch as mrw  # sibling script (scripts/ is on sys.path)
    except Exception as exc:  # noqa: BLE001
        print(f"   ⚠️  --watch-rest: couldn't import meet_rest_watch ({exc}); skipping.",
              file=sys.stderr)
        return None, None
    token_file = _resolve_path(args.rest_token)
    meet = mrw.Meet(cfg, token_file)
    stop = threading.Event()
    # In --keep-open the call has no cap, so let REST run effectively unbounded too.
    rest_duration = 86_400.0 if args.keep_open else max(args.duration, args.rest_find_timeout)
    # A native Chat 1:1 call's conference is NOT queryable by the meeting code scraped
    # from the page (space.meeting_code returns {} — verified 2026-06-18); the call
    # creates its OWN auto Meet space. So REST always uses AUTO (the active-conference
    # filter `end_time IS NULL`), which finds the live conference. The extracted code
    # is shown for reference only. ⚠️ REST still lags (propagation seconds–minutes), so
    # this can miss a short call — the real-time signal is --watch-join, not this.
    ref = f"  (page code={meeting_code}, not used for query)" if meeting_code else ""
    print(f"\n=== REST room-data watch (token={args.rest_token}, "
          f"auto=active conference){ref} ===")

    def _run():
        try:
            mrw.watch(
                meet,
                meeting_code=None,
                self_id=args.rest_self_id,
                poll=args.rest_poll,
                duration=rest_duration,
                find_timeout=args.rest_find_timeout,
                stop_event=stop,
            )
        except Exception as exc:  # noqa: BLE001 - a background error must not crash the call
            print(f"   ⚠️  REST watch error: {exc}", file=sys.stderr)

    t = threading.Thread(target=_run, name="meet-rest-watch", daemon=True)
    t.start()
    return stop, t


def _space_id(raw: str) -> str:
    """Normalise ``spaces/qtotjoAAAAE`` or ``qtotjoAAAAE`` → the bare id."""
    raw = (raw or "").strip()
    return raw.split("/", 1)[1] if raw.startswith("spaces/") else raw


def _default_url(space: str, authuser: int) -> str:
    """Standalone Chat deep link to a space/DM. The ``/app/chat/<spaceId>`` form is
    what the live app routes to (the older ``#chat/space/...`` hash form silently
    bounces to /app/home). If it still doesn't open, pass --url with the exact
    address-bar URL from the window (the help text says so)."""
    return f"https://chat.google.com/u/{authuser}/app/chat/{_space_id(space)}"


def _derive_proc_match(cdp_url: str) -> "str | None":
    """For 'profile' audio mode: find the LOCAL browser process exposing the CDP debug
    port in `cdp_url` and return its --user-data-dir value — a token that uniquely
    identifies that browser's whole process tree, so PulseAudio capture can be scoped
    to JUST the caller browser (call-only) and never the daily browser or other apps.

    Reads /proc directly (Linux); returns None if not derivable (then profile mode
    falls back to needing an explicit --audio-proc-match)."""
    m = re.search(r":(\d+)", cdp_url or "")
    if not m:
        return None
    port_flag = f"--remote-debugging-port={m.group(1)}"
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except Exception:  # noqa: BLE001
        return None
    port_seen = False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cl = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        if port_flag not in cl:
            continue
        port_seen = True
        um = re.search(r"--user-data-dir=(\S+)", cl)
        if um:
            return um.group(1)  # dedicated profile path — the most specific scope token
    # A browser on the default profile carries no --user-data-dir; fall back to the
    # debug-port flag itself (child processes inherit it, so a PID-tree match still
    # scopes to this browser only). None if no such browser is running.
    return port_flag if port_seen else None


def _dump_buttons(page) -> list[str]:
    """Every visible button's accessible name (aria-label or text) — the lifeline
    when a selector misses, so you can see what to pass via --button-name."""
    labels: list[str] = []
    try:
        handles = page.get_by_role("button").all()
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return labels
    for h in handles[:300]:
        try:
            if not h.is_visible():
                continue
            name = (h.get_attribute("aria-label") or h.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
        if name:
            labels.append(name)
    # De-dup, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for n in labels:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _find_call_button(page, override: str):
    """Return a Playwright Locator for the call control, or None. Tries an exact
    --button-name first, then the pattern list, by accessible role+name."""
    patterns = [re.escape(override)] if override else list(_CALL_BUTTON_PATTERNS)
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for getter in (page.get_by_role, page.get_by_label):
            try:
                if getter is page.get_by_role:
                    loc = getter("button", name=rx)
                else:
                    loc = getter(rx)
            except Exception:  # noqa: BLE001
                continue
            try:
                count = loc.count()
            except Exception:  # noqa: BLE001
                continue
            for i in range(min(count, 5)):
                cand = loc.nth(i)
                try:
                    if cand.is_visible():
                        return cand
                except Exception:  # noqa: BLE001
                    continue
    return None


def _frames(page) -> list:
    """All frames of a page (the in-DM call UI lives in a meet.google.com IFRAME,
    so end-state buttons/text aren't in the main frame). Falls back to the page."""
    try:
        return list(page.frames)
    except Exception:  # noqa: BLE001
        return [page]


def _find_button_in_frames(page, patterns):
    """First VISIBLE role=button across ALL frames whose accessible name matches any
    pattern → its Locator, else None."""
    for fr in _frames(page):
        for pat in patterns:
            rx = re.compile(pat, re.I)
            try:
                loc = fr.get_by_role("button", name=rx)
                if loc.count() and loc.first.is_visible():
                    return loc.first
            except Exception:  # noqa: BLE001
                continue
    return None


def _button_label(page, patterns) -> "str | None":
    """Accessible name of the first matching visible button across frames, or None."""
    loc = _find_button_in_frames(page, patterns)
    if loc is None:
        return None
    try:
        return (loc.get_attribute("aria-label") or loc.inner_text() or "button").strip()[:80]
    except Exception:  # noqa: BLE001
        return "button"


# Mic state in the Meet call UI: the control reads "Turn off microphone" when the mic
# is ON, "Turn on microphone" when OFF. A call can join MUTED (Meet remembers the last
# state) — and a muted track transmits silence no matter what feeds the virtual mic, so
# for the AI-mouth path we must ensure the bot's mic is ON after answering.
_MIC_OFF_PATTERNS = (r"turn on micro", r"unmute")   # mic currently OFF → click to enable
_MIC_ON_PATTERNS = (r"turn off micro",)             # mic currently ON


def _ensure_mic_on(page) -> str:
    """Make sure the bot's mic is ON (so it transmits the injected audio). Clicks the
    unmute control if the mic is OFF. Returns a short status string for logging."""
    off = _find_button_in_frames(page, _MIC_OFF_PATTERNS)
    if off is not None:
        try:
            label = (off.get_attribute("aria-label") or "unmute").strip()[:60]
            off.click()
            return f"was OFF → clicked {label!r} to unmute"
        except Exception as exc:  # noqa: BLE001
            return f"was OFF but unmute click failed: {exc}"
    on = _button_label(page, _MIC_ON_PATTERNS)
    if on:
        return f"already ON ({on!r})"
    return "no mic control found yet (UI not in call frame?)"


def _find_text_in_frames(page, patterns) -> "str | None":
    """First VISIBLE text across all frames matching any pattern → a short snippet,
    else None. Used to read 'Call ended' / 'No answer' style end-state banners."""
    for fr in _frames(page):
        for pat in patterns:
            rx = re.compile(pat, re.I)
            try:
                loc = fr.get_by_text(rx)
                if loc.count() and loc.first.is_visible():
                    return ((loc.first.inner_text() or pat).strip() or pat)[:80]
            except Exception:  # noqa: BLE001
                continue
    return None


def _in_call(page) -> bool:
    """True while a live call is up — a leave/hang-up control is visible. This is the
    RELIABLE signal (unlike end-state text, which collides with DM history cards)."""
    return _find_button_in_frames(page, _IN_CALL_PATTERNS) is not None


def _alone_signal(page) -> "str | None":
    """Secondary end signal: the callee left a 1:1 call but we weren't auto-dropped
    and now linger alone ('You're the only one here'). Wording specific enough not to
    appear in a history card."""
    return _find_text_in_frames(page, _ALONE_PATTERNS)


# Read the live participant roster from the Meet call frame. [data-participant-id]
# renders one element per participant tile; the 'X joined' toast names the joiner.
# Empirically (diag_call_join.py, 2026-06-18) BOTH flip the instant the callee
# answers: tile count 1→2 and the toast appears at the same poll. connectionState is
# NOT a join signal (Meet is SFU-based → it reads 'connected' once WE join the
# server, before any remote), so this DOM roster is the real-time join detector.
_JOIN_PROBE = r"""
(() => {
  const q = (s) => { try { return document.querySelectorAll(s).length; } catch(e){ return 0; } };
  // Force a synchronous layout each poll. A throttled/backgrounded renderer can leave the
  // participant tile uncommitted/unlaid-out (tiles read 0 forever → roster-collapse hang-up
  // never fires); touching offsetHeight pokes the renderer (this is what the live diagnostic
  // was incidentally doing when it read tiles=1 where the plain poll read 0).
  try { void (document.body && document.body.offsetHeight); } catch(e){}
  let joined = null;
  try {
    const bt = (document.body ? document.body.innerText : '') || '';
    const m = bt.match(/([^\n]{1,40}?)\s+joined\b/i);
    if (m) joined = m[1].trim().slice(0, 60);
  } catch(e){}
  return {count: q('[data-participant-id]'), joined: joined};
})()
"""


# Live STRUCTURE probe — diagnostic only (--diag-structure). The user confirmed the call
# UI + video render on screen yet [data-participant-id] reads 0 and the audio tap captures
# silence, so the roster selector has DRIFTED and the live media lives somewhere the tap
# isn't looking. This dumps, per frame, counts for many candidate tile selectors + the live
# <video>/<audio> elements + visibilityState, so one observed call reveals the right selector
# and where the live media actually is. Heavy (scans attributes) → diagnostic runs only.
_STRUCTURE_PROBE = r"""
(() => {
  const q = (s) => { try { return document.querySelectorAll(s).length; } catch(e){ return -1; } };
  let playingVids = -1, liveAud = -1, partAttr = -1;
  try { playingVids = [...document.querySelectorAll('video')]
          .filter(v => !v.paused && v.readyState >= 2).length; } catch(e){}
  try {
    let n = 0;
    document.querySelectorAll('audio,video').forEach(el => {
      const s = el.srcObject;
      if (s && s.getAudioTracks) n += s.getAudioTracks().filter(t => t.readyState === 'live').length;
    });
    liveAud = n;
  } catch(e){}
  try {  // any element carrying a 'participant'-ish attribute → hints the current selector
    let n = 0;
    document.querySelectorAll('div,span,c-wiz').forEach(e => {
      for (const a of e.attributes) { if (/participant|allocation|device-id/i.test(a.name)) { n++; break; } }
    });
    partAttr = n;
  } catch(e){}
  return {
    url: location.href.slice(0, 70), vis: document.visibilityState,
    videos: q('video'), playingVids: playingVids, audios: q('audio'), liveAud: liveAud,
    partId: q('[data-participant-id]'), allocIdx: q('[data-allocation-index]'),
    selfName: q('[data-self-name]'), reqId: q('[data-requested-participant-id]'),
    listitem: q('[role=listitem]'), partAttr: partAttr,
  };
})()
"""


def _dump_structure(page, tag: str) -> None:
    """Print the _STRUCTURE_PROBE for every google frame — diagnostic for finding the live
    tile selector + media. Best-effort; never raises into the loop."""
    for fr in _frames(page):
        u = fr.url or ""
        if "google.com" not in u:
            continue
        try:
            d = fr.evaluate(_STRUCTURE_PROBE)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(d, dict) and (d.get("videos") or d.get("partAttr") or d.get("partId")):
            print(f"   [struct {tag}] {d}")


def _participant_join_state(page) -> "tuple[int, str | None]":
    """(participant_tile_count, joined_name|None) from the Meet call frame(s).
    1 = caller alone (ringing), ≥2 = a remote JOINED, 0 = the call UI has torn down
    (ended). joined_name is the most recent 'X joined' toast when still visible."""
    count = 0
    joined = None
    for fr in _frames(page):
        if "meet.google.com" not in (fr.url or ""):
            continue
        try:
            d = fr.evaluate(_JOIN_PROBE)
        except Exception:  # noqa: BLE001 - probe must never raise into the loop
            continue
        if isinstance(d, dict):
            try:
                count = max(count, int(d.get("count") or 0))
            except Exception:  # noqa: BLE001
                pass
            joined = joined or d.get("joined")
    return count, joined


# WebRTC join signal — robust when the call tab is BACKGROUNDED. Switching to
# another tab throttles the page's timers + pauses requestAnimationFrame, so the
# DOM roster ('X joined' toast, sometimes the tile) can lag while you work
# elsewhere. The inbound `track` event fires at the media layer regardless of tab
# visibility (it's why Meet audio keeps playing when you tab away), so a growth in
# the remote-track count is the signal that survives backgrounding. Installed as a
# context init-script BEFORE the call iframe loads (wraps RTCPeerConnection to count
# inbound tracks). Proven live in diag_call_join.py: tracks went 3→5 the instant the
# callee answered (two video tracks added), alongside the DOM tile 1→2.
#
# DOUBLE DUTY: when window.__MCB_CAPTURE is set (by --capture-audio in webrtc mode),
# the same `track` handler also taps the inbound AUDIO track — it feeds it to a
# MediaRecorder and pushes base64 chunks onto window.__audioChunks, which the Python
# BrowserAudioTap drains. This captures the REMOTE voice (what the bot hears) cleanly
# at the media layer — NOT the OS output mix — and survives a backgrounded tab.
_WEBRTC_HOOK = r"""
(() => {
  try {
    if (window.__mcb_installed) return;
    window.__mcb_installed = true;
    window.__remoteTracks = 0;       // cumulative inbound tracks → JOIN signal (monotonic)
    window.__pcDead = 0;             // PC reached closed/failed → END signal
    window.__audioChunks = window.__audioChunks || [];
    window.__mcbDiag = window.__mcbDiag || {};
    window.__mcbGen = window.__mcbGen || 0;          // recorder generation (bumps on each (re)start)
    window.__mcbFrameId = window.__mcbFrameId ||      // disambiguates per-frame recorders
        ('f' + Math.floor(Math.random() * 1e9).toString(36));
    window.__mcbCaptureOwner = window.__mcbCaptureOwner || false;  // this frame owns a live recorder

    // Each chunk is tagged "<frameId>:<gen>|<base64>" so the Python drainer groups one
    // recorder's chunks into a single standalone segment and NEVER interleaves two
    // recorders. That kills the truncation bug: a RESTARTED recorder writes a fresh webm
    // header mid-file, so a naive append leaves ffmpeg decoding only the first segment
    // (~3s). Per-segment files are decoded independently then concatenated. gen is fixed
    // per recorder (captured in its closure), so chunks never straddle a generation.
    function pushBlob(blob, gen) {
      if (!blob || !blob.size) return;
      const key = window.__mcbFrameId + ':' + gen;
      const fr = new FileReader();
      fr.onload = () => {
        const s = '' + fr.result; const i = s.indexOf(',');
        window.__audioChunks.push(key + '|' + (i >= 0 ? s.slice(i + 1) : s));
      };
      fr.readAsDataURL(blob);
    }

    window.__mcbPCs = window.__mcbPCs || [];
    window.__mcbConnected = window.__mcbConnected || {};  // track.id -> true (already wired into the graph)

    // The CURRENTLY-live remote audio tracks: read fresh from BOTH the PeerConnections'
    // RECEIVERS and the playing <audio>/<video> elements' srcObject. The track objects
    // handed to `ontrack` die / get superseded as the SFU renegotiates (recording them
    // stopped the recorder <1s in; feeding them to WebAudio gave silence — same root
    // cause), and even getReceivers() can read 0-live mid-call, so we union both sources.
    window.__mcbLiveAudioTracks = function() {
      var out = []; var seen = {};
      function add(t){
        if (t && t.kind === 'audio' && t.readyState === 'live' && !seen[t.id]) { seen[t.id] = 1; out.push(t); }
      }
      (window.__mcbPCs || []).forEach(function(pc){
        try { pc.getReceivers().forEach(function(r){ add(r.track); }); } catch(e){}
      });
      try {
        document.querySelectorAll('audio,video').forEach(function(el){
          try { var s = el.srcObject; if (s && s.getAudioTracks) s.getAudioTracks().forEach(add); } catch(e){}
        });
      } catch(e){}
      return out;
    };

    // IMMORTAL capture graph: one AudioContext + MediaStreamDestination created once.
    // The recorder records the DESTINATION's stream — a synthetic LOCAL track that never
    // ends — so the recorder's lifetime is decoupled from the volatile remote tracks
    // (the bug that killed every prior attempt). Remote audio tracks are merely CONNECTED
    // as sources and reconnected as they churn; the recorder keeps running throughout.
    window.__mcbEnsureGraph = function() {
      if (window.__mcbCtx) return true;
      try {
        var AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return false;
        window.__mcbCtx = new AC();
        window.__mcbDest = window.__mcbCtx.createMediaStreamDestination();
      } catch(e) { window.__mcbErr = 'graph:' + e; return false; }
      return true;
    };

    // Idempotent: connect any not-yet-wired live remote audio track to the destination,
    // and start the immortal recorder once at least one source is connected. Python
    // re-pokes this every drain, so newly-unmuted / post-renegotiation tracks get wired
    // as they appear. A track is `muted` (no RTP) until audio flows — we still connect it
    // (the source pulls samples once it unmutes), but only START recording once we have a
    // connected source, so a call with no audio never writes a bogus silent WAV.
    window.__mcbStartRec = function() {
      try {
        if (!window.__MCB_CAPTURE || !window.MediaRecorder) return;
        if (!window.__mcbEnsureGraph()) return;
        try { if (window.__mcbCtx.state === 'suspended') window.__mcbCtx.resume(); } catch(e){}
        var live = window.__mcbLiveAudioTracks();
        window.__mcbDiag.recvLive = live.length;
        window.__mcbDiag.recvUnmuted = live.filter(function(t){ return !t.muted; }).length;
        window.__mcbSinks = window.__mcbSinks || {};  // track.id -> <audio> sink (kept alive)
        live.forEach(function(t){
          if (window.__mcbConnected[t.id]) return;
          try {
            // 🔑 ACTIVATE DECODING: a MediaStreamAudioSourceNode built from a REMOTE WebRTC
            // track outputs SILENCE unless the same track is also attached to a PLAYING media
            // element — Chromium decodes remote audio lazily, only when an element sinks it
            // (proven live 2026-06-19: recvUnmuted=1, recorder 'recording', yet ch=0 / -91dB
            // until this sink was added). muted keeps it off the speakers; we capture via the
            // WebAudio graph, not the element.
            // ONE shared MediaStream for BOTH the decode-activation sink and the WebAudio
            // source. Chromium activates lazy decode of a remote track PER-MediaStream: two
            // separate `new MediaStream([t])` wrappers meant the SINK's stream decoded but the
            // SOURCE's stream stayed silent (proven live 2026-06-19: ICE connected, recvUnmuted=1,
            // inbound RTP 3MB+, recorder 'recording' — yet the WAV was -91dB). Sharing the stream
            // is what feeds the decoded PCM into the capture graph.
            var ms = new MediaStream([t]);
            var sink = new Audio();
            sink.srcObject = ms;
            sink.muted = true;
            try { var pp = sink.play(); if (pp && pp.catch) pp.catch(function(){}); } catch(e){}
            window.__mcbSinks[t.id] = sink;  // retain ref so it isn't GC'd (would stop decode)

            // Capture path 1: tap the shared stream directly.
            var src = window.__mcbCtx.createMediaStreamSource(ms);
            src.connect(window.__mcbDest);
            // Capture path 2 (belt-and-suspenders): route the playing sink ELEMENT through the
            // graph. createMediaElementSource forces the element to decode and re-routes its audio
            // INTO the AudioContext (not the speakers — we connect only to __mcbDest), so even if
            // the raw-stream source above stays lazy, the element pipeline delivers PCM. Mixing
            // both into one destination is harmless (silence + audio = audio).
            try {
              var esrc = window.__mcbCtx.createMediaElementSource(sink);
              esrc.connect(window.__mcbDest);
            } catch(e) { /* element-source unsupported for this srcObject — path 1 still active */ }
            window.__mcbConnected[t.id] = true;
            window.__mcbDiag.connected = (window.__mcbDiag.connected || 0) + 1;
          } catch(e) { window.__mcbErr = 'connect:' + e; }
        });
        window.__mcbDiag.ctxState = window.__mcbCtx.state;
        if (window.__mcbRecorder && window.__mcbRecorder.state === 'recording') return;
        if (!Object.keys(window.__mcbConnected).length) return;  // nothing wired yet
        var mime = 'audio/webm;codecs=opus';
        try { if (!MediaRecorder.isTypeSupported(mime)) mime = 'audio/webm'; } catch(e){ mime = ''; }
        var rec = mime ? new MediaRecorder(window.__mcbDest.stream, {mimeType: mime})
                       : new MediaRecorder(window.__mcbDest.stream);
        window.__audioMime = mime;
        var myGen = ++window.__mcbGen;       // this recorder's generation (1, 2, …)
        window.__mcbCaptureOwner = true;     // this frame owns a recorder → Python drains it
        rec.ondataavailable = function(e){ pushBlob(e.data, myGen); };
        rec.onstart = function(){ window.__mcbDiag.started = (window.__mcbDiag.started||0)+1; };
        rec.onstop  = function(){ window.__mcbDiag.stopped = (window.__mcbDiag.stopped||0)+1; };
        rec.onerror = function(e){ window.__mcbErr = 'onerror:' + ((e&&e.error&&e.error.name)||e); };
        rec.start(1000);  // emit a chunk each second
        window.__mcbDiag.afterStart = rec.state;
        window.__mcbDiag.gen = myGen;
        window.__mcbRecorder = rec;
      } catch(e) { window.__mcbErr = 'startRec:' + e; }
    };

    // Full mid-call inventory — decisive when capture still finds nothing: shows per-PC
    // receiver track states and per-media-element srcObject track states + ctx state.
    window.__mcbInventory = function() {
      var inv = {ctx: (window.__mcbCtx ? window.__mcbCtx.state : null),
                 connected: Object.keys(window.__mcbConnected || {}).length, pcs: [], els: []};
      (window.__mcbPCs || []).forEach(function(pc){
        try {
          var recvs = pc.getReceivers().map(function(r){
            var t = r.track; return t ? {k: t.kind, rs: t.readyState, m: t.muted} : null; });
          // ics/igs reveal the ICE failure MODE: 'checking'→'failed' = no working
          // candidate pair (network/TURN/firewall); 'connected'/'completed' = media
          // DID connect (so a later 'closed' cs is a real hang-up, not a connect failure).
          inv.pcs.push({cs: pc.connectionState, ss: pc.signalingState,
                        ics: pc.iceConnectionState, igs: pc.iceGatheringState, recvs: recvs});
        } catch(e) { inv.pcs.push({err: '' + e}); }
      });
      try {
        document.querySelectorAll('audio,video').forEach(function(el){
          var s = el.srcObject;
          var ats = (s && s.getAudioTracks) ? s.getAudioTracks().map(function(t){
            return {rs: t.readyState, m: t.muted}; }) : [];
          if (ats.length || el.srcObject) inv.els.push({tag: el.tagName, paused: el.paused, ats: ats});
        });
      } catch(e){}
      return inv;
    };

    const O = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (!O) return;
    // Per-PC instrumentation, IDEMPOTENT. Shared by the constructor wrap AND the
    // prototype-method patch below, so a PC is caught no matter HOW it was minted.
    window.__mcbRegisterPC = function(pc) {
      try {
        if (!pc || pc.__mcbReg) return;
        pc.__mcbReg = true;
        window.__mcbPCs.push(pc);  // so __mcbLiveAudioTracks() can read its receivers
        // PC teardown is a reliable END signal: in a 1:1 huddle either side hanging
        // up ends the call → the call UI's PC closes. Survives a backgrounded tab.
        const markDead = () => { window.__pcDead = (window.__pcDead || 0) + 1; };
        pc.addEventListener('connectionstatechange', () => {
          const s = pc.connectionState;
          if (s === 'closed' || s === 'failed') markDead();
        });
        pc.addEventListener('iceconnectionstatechange', () => {
          const s = pc.iceConnectionState;
          if (s === 'closed' || s === 'failed') markDead();
        });
        pc.addEventListener('track', (ev) => {
          window.__remoteTracks++;  // cumulative → JOIN signal (monotonic)
          try {
            const tr = ev && ev.track;
            if (tr && tr.kind === 'audio' && window.__MCB_CAPTURE) {
              // The track is `muted` until RTP flows; start recording when audio is
              // actually arriving (unmute) so the recorder doesn't stop on an empty
              // stream. Python re-pokes __mcbStartRec each drain as the main driver;
              // this listener just reacts faster on the common path.
              tr.addEventListener('unmute', () => {
                window.__mcbDiag.unmuteSeen = (window.__mcbDiag.unmuteSeen || 0) + 1;
                window.__mcbStartRec();
              });
              window.__mcbStartRec();  // in case it's already unmuted
            }
          } catch(e) { window.__mcbErr = 'track:' + e; }
        });
      } catch(e) { window.__mcbErr = 'register:' + e; }
    };
    const W = function(...a) {
      const pc = new O(...a);
      window.__mcbRegisterPC(pc);
      return pc;
    };
    W.prototype = O.prototype;
    window.RTCPeerConnection = W;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = W;
    // 🔑 PROTOTYPE-LEVEL CAPTURE — the decisive fix for catching Meet's LIVE media PC.
    // Wrapping window.RTCPeerConnection only catches PCs built through THAT reference;
    // Google Meet captures the native constructor BEFORE our init-script runs (esp. over
    // CDP into a pre-loaded browser + the cross-origin meet OOPIF), so its real media PC
    // bypasses the wrap. Proven live (run 175205): __mcbPCs held only CLOSED ringback PCs,
    // recvLive=0 throughout, WAV silent — yet the callee confirmed the call connected, so
    // the caller WAS receiving remote audio on a PC the wrap never saw. Every genuine
    // RTCPeerConnection shares O.prototype, so patching these methods registers each
    // instance the first time it touches SDP/media — independent of the minting ctor.
    // Meet renegotiates over the call's life (ICE restarts / track churn), so even a PC
    // created before the patch is caught on its next setRemoteDescription.
    try {
      ['setRemoteDescription','setLocalDescription','addTrack','addTransceiver',
       'createOffer','createAnswer'].forEach(function(m){
        const orig = O.prototype[m];
        if (typeof orig !== 'function') return;
        O.prototype[m] = function() {
          try { window.__mcbRegisterPC(this); } catch(e){}
          return orig.apply(this, arguments);
        };
      });
    } catch(e) { window.__mcbErr = 'protopatch:' + e; }
  } catch(e) {}
})();
"""


def _webrtc_global_max(page, expr: str) -> int:
    """Max of a numeric `window.*` global across the meet/chat frames (the globals
    installed by _WEBRTC_HOOK). Returns 0 if the hook never ran or the frame is gone
    (probe must never raise into the call loop)."""
    n = 0
    for fr in _frames(page):
        u = fr.url or ""
        if "meet.google.com" not in u and "chat.google.com" not in u:
            continue
        try:
            n = max(n, int(fr.evaluate(expr)))
        except Exception:  # noqa: BLE001 - probe must never raise into the loop
            pass
    return n


def _webrtc_track_count(page) -> int:
    """Cumulative inbound remote-track count across the meet/chat frames — the JOIN
    signal (monotonic; rises as the callee's media arrives on answer)."""
    return _webrtc_global_max(page, "window.__remoteTracks || 0")


def _webrtc_live_audio(page) -> int:
    """Count of CURRENTLY-live inbound audio tracks, read fresh from the PCs' RECEIVERS
    — the WebRTC END signal. Rises to ≥1 when the remote's audio arrives, falls to 0
    when the tracks end / the call UI is torn down (hang-up). Read from receivers (not
    a cumulative counter) because the remote 'ended' event proved unreliable. Mirrors
    the join modality so we don't misread the always-zero DOM roster as a call collapse
    in this embedded call UI."""
    return _webrtc_global_max(
        page, "(window.__mcbLiveAudioTracks ? window.__mcbLiveAudioTracks().length : 0)")


def _webrtc_pc_dead(page) -> bool:
    """True once any call PeerConnection reached closed/failed — a 1:1 huddle ends for
    both sides when either hangs up, tearing down the call UI's PC."""
    return _webrtc_global_max(page, "window.__pcDead || 0") > 0


def _webrtc_unmute_seen(page) -> int:
    """Cumulative count of 'unmute' events on a remote AUDIO track — RTP actually
    started flowing at least once. The most robust 'real media connected' latch:
    a ringback PeerConnection that never receives remote media never fires an unmute
    (so this can't false-fire during the ring), and unlike the POLLED live-track /
    inbound-byte checks it's a MONOTONIC event counter, so a real-but-short-lived
    connection that comes and goes BETWEEN two polls can't be missed. Set in the
    'track'→'unmute' listener of _WEBRTC_HOOK. (Fix: a real call whose media flickered
    on then dropped left media_connected un-latched → every hang-up signal stayed
    gated off → the call held to the duration cap instead of stopping on hang-up.)"""
    return _webrtc_global_max(page, "(window.__mcbDiag && window.__mcbDiag.unmuteSeen) || 0")


def _keepalive_renderer(call_page):
    """Keep the call tab's renderer AWAKE so media + DOM survive an occluded/unfocused
    window. CRITICAL on GNOME-Wayland: an occluded Brave renderer is FULLY suspended (not
    just DOM-throttled) → ICE times out → the call's PeerConnections close → silent audio
    capture + no hang-up signal + the ~+33s late join. bring_to_front + CDP focus-emulation
    + setWebLifecycleState(active) + a tiny Page.startScreencast whose frames we ack to keep
    the compositor ticking. MUST run the INSTANT the call page loads — BEFORE the answer +
    media-setup window — or the connection is already dead by the time it fires. Returns the
    CDP session (kept alive so its frame-ack loop persists), or None. Best-effort throughout."""
    try:
        call_page.bring_to_front()
    except Exception:  # noqa: BLE001
        pass
    try:
        fc = call_page.context.new_cdp_session(call_page)
    except Exception as exc:  # noqa: BLE001
        print(f"   ⚠️  renderer keepalive unavailable ({exc}); capture/hang-up may fail occluded")
        return None
    try:  # un-minimize: a minimized window hides ALL tabs (visibilityState='hidden')
        w = fc.send("Browser.getWindowForTarget")
        fc.send("Browser.setWindowBounds",
                {"windowId": w["windowId"], "bounds": {"windowState": "normal"}})
    except Exception:  # noqa: BLE001
        pass
    try:
        fc.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
    except Exception:  # noqa: BLE001
        pass
    try:
        fc.send("Page.setWebLifecycleState", {"state": "active"})
    except Exception:  # noqa: BLE001
        pass
    try:
        fc.send("Page.enable")

        def _ack_frame(params):  # keep frames flowing → compositor keeps ticking
            try:
                fc.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:  # noqa: BLE001
                pass

        fc.on("Page.screencastFrame", _ack_frame)
        fc.send("Page.startScreencast",
                {"format": "jpeg", "quality": 1,
                 "maxWidth": 64, "maxHeight": 64, "everyNthFrame": 1})
        print("   keepalive: focus-emulated + screencast (renderer kept awake even occluded)")
    except Exception as sc_exc:  # noqa: BLE001
        print(f"   keepalive: focus-emulated (screencast unavailable: {sc_exc})")
    return fc


# Cumulative INBOUND-RTP bytes, summed over every call PeerConnection's getStats() — the
# MOST ROBUST hang-up signal for this embedded huddle. Everything DOM-based fails here (the
# roster tiles only render when the OS window is visible; the survey + call iframe never
# tear down), and the SFU keeps the PeerConnection `connected` and the receiver tracks
# `live` after the remote leaves — so _webrtc_pc_dead / _webrtc_live_audio never drop. But
# the one thing that genuinely STOPS when the remote leaves is the media itself: no more RTP
# arrives, so bytesReceived FLATLINES. The caller diffs this per poll; a sustained flatline
# after media was flowing = the remote is gone (hang-up). getStats() is async, so the
# expression is an async IIFE (Playwright awaits the returned promise).
# Bare async FUNCTION (not an IIFE expression): Playwright detects `async () => …`, CALLS it,
# and awaits the returned promise. An `(async()=>{})()` IIFE-as-expression is NOT reliably
# awaited by evaluate() — that mis-form made _webrtc_inbound_bytes return -1 every poll, so the
# flatline hang-up signal never engaged (the bug).
_INBOUND_BYTES_FN = r"""
async () => {
  try {
    const pcs = window.__mcbPCs || [];
    let total = 0;
    for (const pc of pcs) {
      try {
        const stats = await pc.getStats();
        stats.forEach(function(r){
          if (r && r.type === 'inbound-rtp') total += (r.bytesReceived || 0);
        });
      } catch(e){}
    }
    return total;
  } catch(e) { return -1; }
}
"""


def _webrtc_inbound_bytes(page) -> int:
    """Max cumulative inbound-RTP bytesReceived across the meet/chat frames' PeerConnections
    (the globals installed by _WEBRTC_HOOK). Grows while the remote sends media; FLATLINES
    when the remote leaves. Returns -1 only if every frame's probe failed (inconclusive)."""
    best = -1
    for fr in _frames(page):
        u = fr.url or ""
        if "meet.google.com" not in u and "chat.google.com" not in u:
            continue
        try:
            v = int(fr.evaluate(_INBOUND_BYTES_FN))
        except Exception:  # noqa: BLE001 - probe must never raise into the loop
            continue
        if v > best:
            best = v
    return best


# WHY does the caller's ICE never connect? getStats() candidate-pair / candidate diagnostics.
# For each call PeerConnection this reports the iceConnectionState plus, from getStats:
# the candidate-pair states (succeeded/failed/in-progress), whether any pair was nominated,
# and the TYPE mix of gathered local/remote candidates (host/srflx/relay). The decisive reads:
#   • ics 'checking' with NO succeeded pair  → connectivity failure (network/firewall/TURN).
#   • zero 'relay' candidates + srflx-only failing → no TURN fallback reachable.
#   • a 'succeeded'/nominated pair but cs later 'closed' → media DID connect (real hang-up).
_ICE_STATS_FN = r"""
async () => {
  const out = [];
  const pcs = window.__mcbPCs || [];
  for (const pc of pcs) {
    const e = {ics: pc.iceConnectionState, igs: pc.iceGatheringState,
               pairs: [], loc: {}, rem: {}, sel: null, inB: 0};
    try {
      const stats = await pc.getStats();
      const cands = {};
      stats.forEach(function(r){
        if (!r) return;
        if (r.type === 'inbound-rtp') e.inB += (r.bytesReceived || 0);
        else if (r.type === 'local-candidate' || r.type === 'remote-candidate') cands[r.id] = r;
        else if (r.type === 'candidate-pair') {
          e.pairs.push({st: r.state, nom: !!r.nominated, br: r.bytesReceived || 0});
          if (r.selected || r.nominated) e.sel = r;
        }
      });
      stats.forEach(function(r){
        if (r && r.type === 'local-candidate') e.loc[r.candidateType] = (e.loc[r.candidateType]||0)+1;
        if (r && r.type === 'remote-candidate') e.rem[r.candidateType] = (e.rem[r.candidateType]||0)+1;
      });
      if (e.sel) {
        var L = cands[e.sel.localCandidateId], R = cands[e.sel.remoteCandidateId];
        e.sel = {st: e.sel.state, l: L && L.candidateType, r: R && R.candidateType};
      }
    } catch(err) { e.err = '' + err; }
    out.push(e);
  }
  return out;
}
"""


def _webrtc_ice_stats(page):
    """Per-PC ICE candidate-pair diagnostics across the call frames (why ICE won't connect).
    Returns a list of dicts (see _ICE_STATS_FN), or [] if nothing probed. Never raises."""
    for fr in _frames(page):
        u = fr.url or ""
        if "meet.google.com" not in u and "chat.google.com" not in u:
            continue
        try:
            v = fr.evaluate(_ICE_STATS_FN)
        except Exception:  # noqa: BLE001 - probe must never raise into the loop
            continue
        if v:
            return v
    return []


def _call_target_present(cdp_url: str, timeout: float = 2.0):
    """Query the browser's CDP target list (the /json HTTP endpoint) for the live Meet
    call iframe target (meet.google.com/call). Returns True/False, or None when the query
    itself failed (inconclusive — the caller must NOT advance its hang-up debounce on a
    None). This is the GROUND TRUTH for the call iframe's lifetime: a live run proved
    Playwright's page.frames retains a STALE detached OOPIF after hang-up (it kept reading
    the torn-down call frame as present, so the script ran to the 120s cap), whereas the
    CDP target disappears from /json the instant the huddle ends."""
    base = cdp_url.rstrip("/")
    if base.startswith("ws://"):
        base = "http://" + base[len("ws://"):]
    elif base.startswith("wss://"):
        base = "https://" + base[len("wss://"):]
    try:
        with urllib.request.urlopen(base + "/json", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        for t in data:
            if "meet.google.com/call" in (t.get("url") or ""):
                return True
        return False
    except Exception:  # noqa: BLE001
        return None


def _call_frame_present(page, cdp_url: str = ""):
    """True while the embedded Meet call iframe (meet.google.com/call) is attached; Chat
    tears it down when the huddle ends, so its disappearance is the hang-up signal that
    DOESN'T depend on the (unreachable-mid-call) WebRTC probes or the flaky DOM leave-
    control. Prefers the CDP target list (cdp_url) — the only RELIABLE view: Playwright's
    page.frames keeps a stale detached OOPIF after hang-up. Returns None when the CDP
    query is inconclusive so the caller leaves its debounce counters untouched. Falls back
    to page.frames only when no cdp_url is available (a Playwright-launched browser)."""
    if cdp_url:
        return _call_target_present(cdp_url)  # True / False / None (inconclusive)
    try:
        for fr in page.frames:
            if "meet.google.com/call" in (fr.url or ""):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


# Post-call rating survey — the EXPLICIT, RELIABLE hang-up signal (user's tip, confirmed
# live 2026-06-19). On this embedded Chat DM huddle the call iframe does NOT tear down when
# you hang up (CDP /json keeps listing it) and the in-call controls are flaky — but Google
# pops a "Rate the meeting N star(s) out of 5" survey the instant the call ends, so its
# appearance is the hang-up the script can actually SEE. The leading patterns match the
# REAL aria-labels dumped from the live page ("Rate the meeting 1 star out of 5.", …); the
# trailing call-quality forms keep other Meet UIs / locales covered. Anchored on rating
# copy so a DM call-HISTORY card ("Call ended") never false-positives.
_RATING_PATTERNS = (
    "rate the meeting",        # en: live aria-label "Rate the meeting N star(s) out of 5."
    "star out of 5",           #     "1 star out of 5"
    "stars out of 5",          #     "2..5 stars out of 5"
    "rate the call",
    "call quality",
    "how was the call",
    "how was the audio and video",  # en: live survey "How was the audio and video?"
    "how was the audio",
    "quality of the call",
    "quality of your call",
    "chất lượng cuộc gọi",     # vi: "call quality"
    "đánh giá cuộc gọi",       # vi: "rate the call"
    "đánh giá cuộc họp",       # vi: "rate the meeting"
)

# Gathers ON-SCREEN text + aria-labels (lowercased, capped) so we can match the survey copy
# regardless of which frame/document it renders in.
# ⚠️ VISIBILITY-AWARE (fixed 2026-06-19): this embedded Chat DM huddle does NOT remove the
# post-call "Rate the meeting …" survey from the DOM after it's dismissed — a stale survey
# from a PRIOR call lingers as HIDDEN nodes. A probe that scanned every [aria-label]
# regardless of visibility therefore reported the survey "always present", so the hang-up
# arm-after-absent guard could never arm and the script rode to the duration cap (the
# long-standing "hang-up never detected" bug). We now collect labels/text ONLY from elements
# that are actually rendered on screen, so a hidden leftover survey is ignored and a freshly
# shown survey (the real hang-up) registers.
_FEEDBACK_PROBE = r"""
(() => {
  try {
    function vis(el) {
      try {
        const r = el.getBoundingClientRect();
        if (r.width <= 1 || r.height <= 1) return false;
        const s = window.getComputedStyle(el);
        if (!s || s.visibility === 'hidden' || s.display === 'none') return false;
        if (parseFloat(s.opacity || '1') === 0) return false;
        return el.offsetParent !== null || s.position === 'fixed';
      } catch(e) { return false; }
    }
    const hay = [];
    try {
      document.querySelectorAll('[aria-label],[role="dialog"],[role="alertdialog"],button').forEach(function(el){
        if (!vis(el)) return;
        const a = (el.getAttribute && el.getAttribute('aria-label')) || '';
        if (a) hay.push(a);
        const t = (el.innerText || el.textContent || '');
        if (t) hay.push(t);
      });
    } catch(e){}
    return hay.join(' \n ').toLowerCase().slice(0, 30000);
  } catch(e) { return ''; }
})()
"""


def _post_call_feedback_present(page) -> str:
    """The matched survey phrase (truthy) if the post-call quality-rating prompt is on
    screen, else ''. Scans the main document AND every surviving frame (the survey can
    render in the Chat page or a leftover Meet frame). Never raises into the loop."""
    texts = []
    try:
        texts.append(page.evaluate(_FEEDBACK_PROBE) or "")
    except Exception:  # noqa: BLE001
        pass
    for fr in _frames(page):
        try:
            texts.append(fr.evaluate(_FEEDBACK_PROBE) or "")
        except Exception:  # noqa: BLE001
            continue
    blob = " \n ".join(texts)
    for pat in _RATING_PATTERNS:
        if pat in blob:
            return pat
    return ""


# OUTGOING-RING indicator (user's tip, observed live 2026-06-19): while the call is
# ringing the embedded huddle UI shows a "Calling…" string; the moment the callee picks
# up it DISAPPEARS. That transition is a pure-DOM pickup signal, independent of the WebRTC
# media layer — so it fires even when the media connection is flaky/throttled (the failure
# mode that made the webrtc track-count join signal unreliable). We treat the DISAPPEARANCE
# (after having SEEN it) as "answered", and that starts the monitor recorder.
_CALLING_PATTERNS = (
    "calling…",       # en: ringing screen "Calling…"
    "calling...",
    "calling",        # bare, last (broad) — fine: we only act on appear-then-disappear
    "ringing",
    "đang gọi",       # vi: "calling"
    "đổ chuông",      # vi: "ringing"
)


def _calling_indicator_present(page) -> str:
    """The matched 'Calling…/ringing' phrase (truthy) if the OUTGOING-ring indicator is on
    screen, else ''. Visibility-aware (reuses the survey probe, which collects only rendered
    text/aria-labels). Scans the main document and every surviving frame. Never raises."""
    texts = []
    try:
        texts.append(page.evaluate(_FEEDBACK_PROBE) or "")
    except Exception:  # noqa: BLE001
        pass
    for fr in _frames(page):
        try:
            texts.append(fr.evaluate(_FEEDBACK_PROBE) or "")
        except Exception:  # noqa: BLE001
            continue
    blob = " \n ".join(texts)
    for pat in _CALLING_PATTERNS:
        if pat in blob:
            return pat
    return ""


def _page_text_snippet(page, limit: int = 600) -> str:
    """A short flattened snippet of the call page's visible text — a one-shot diagnostic
    dumped at hang-up so we can read the EXACT survey wording and tighten _RATING_PATTERNS
    if Google's copy drifts. Best-effort; '' if unreadable."""
    parts = []
    try:
        parts.append(page.evaluate(_FEEDBACK_PROBE) or "")
    except Exception:  # noqa: BLE001
        pass
    for fr in _frames(page):
        try:
            parts.append(fr.evaluate(_FEEDBACK_PROBE) or "")
        except Exception:  # noqa: BLE001
            continue
    blob = " ".join(" ".join(parts).split())
    return blob[:limit]


def _sanitize_cookies(cookies: list[dict]) -> list[dict]:
    """Normalise cookies read over CDP for re-injection via add_cookies (keep the
    fields Playwright accepts; coerce session cookies; require the core keys)."""
    out: list[dict] = []
    for c in cookies:
        d = {k: c[k] for k in ("name", "value", "domain", "path", "expires",
                               "httpOnly", "secure") if k in c}
        ss = c.get("sameSite")
        if ss in ("Strict", "Lax", "None"):
            d["sameSite"] = ss
        if d.get("expires") is None:
            d["expires"] = -1
        if all(k in d for k in ("name", "value", "domain", "path")):
            out.append(d)
    return out


def _resolve_executable(args) -> "tuple[str | None, str]":
    """(executable_path, label). --browser-path wins; else map --browser. The
    'chromium' choice returns None → Playwright's bundled Chromium (no install)."""
    if args.browser_path:
        return args.browser_path, args.browser_path
    if args.browser == "chromium":
        return None, "Playwright bundled Chromium"
    path = _BROWSER_PATHS.get(args.browser)
    return path, str(path)


def _launch_isolated(pw, args):
    """Launch an isolated persistent-profile browser (its own window + user-data-dir,
    so it never touches the daily browser)."""
    profile_dir = _resolve_path(args.profile_dir)
    os.makedirs(profile_dir, exist_ok=True)
    exe, label = _resolve_executable(args)
    if exe is not None and not os.path.exists(exe):
        raise SystemExit(
            f"browser not found: {exe} (pass --browser-path, or use "
            "--browser chromium for Playwright's bundled Chromium — no install needed)."
        )
    print(f"  launching {label}")
    print(f"  profile: {profile_dir}")
    # Reduce the most obvious automation tells; keep it a normal headed window so
    # Google's sign-in works (headless Google login is reliably blocked).
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        *args.extra_arg,
    ]
    kwargs = dict(
        user_data_dir=profile_dir,
        headless=args.headless,
        args=launch_args,
        ignore_default_args=["--enable-automation"],
        no_viewport=True,
    )
    if exe is not None:
        kwargs["executable_path"] = exe
    return pw.chromium.launch_persistent_context(**kwargs)


def _open_context(pw, args):
    """Open a Playwright browser context: connect to a running browser over CDP
    (--cdp-url) or launch an isolated persistent-profile browser."""
    if args.cdp_url:
        print(f"  connecting to running browser at {args.cdp_url} (CDP) …")
        # connect_over_cdp's default timeout is INFINITE: if the browser's CDP attach
        # stalls (a suspended/occluded renderer among many targets doesn't ACK), the
        # whole script hangs silently and never places the call. Bound it + retry so a
        # transient stall fails fast and self-recovers instead of hanging forever.
        last = None
        for attempt in range(1, 4):
            try:
                browser = pw.chromium.connect_over_cdp(args.cdp_url, timeout=30000)
                context = (browser.contexts[0] if browser.contexts
                           else browser.new_context())
                if attempt > 1:
                    print(f"  CDP connected on attempt {attempt}.")
                return browser, context
            except Exception as exc:  # noqa: BLE001
                last = exc
                print(f"  ⚠️  CDP connect attempt {attempt}/3 failed "
                      f"({type(exc).__name__}). If this persists, focus the Brave window "
                      "(a suspended renderer can stall the CDP attach) — retrying …")
                time.sleep(3)
        raise RuntimeError(f"could not connect over CDP after 3 attempts: {last}")
    return None, _launch_isolated(pw, args)


def _import_cookies(pw, args, verify_url: str) -> int:
    """One-time setup: pull the live login from a running browser (the source given
    by --import-cookies, e.g. http://127.0.0.1:9222 — read-only) and inject those
    cookies into the isolated profile, so it's signed in WITHOUT a manual login and
    WITHOUT disturbing the source session. Verifies the result, then persists+exits."""
    src = args.import_cookies
    print(f"  reading cookies from running browser at {src} (read-only) …")
    try:
        live = pw.chromium.connect_over_cdp(src)
    except Exception as exc:  # noqa: BLE001
        print(
            f"ERROR: could not connect to {src}: {exc}\n"
            "  Launch the source browser with --remote-debugging-port=9222 first.",
            file=sys.stderr,
        )
        return 2
    ctx = live.contexts[0] if live.contexts else None
    if ctx is None:
        print("ERROR: source browser has no context to read cookies from.", file=sys.stderr)
        return 1
    cookies = _sanitize_cookies(ctx.cookies())
    google = sum(1 for c in cookies if "google" in c.get("domain", ""))
    print(f"  pulled {len(cookies)} cookies ({google} google)")
    if not cookies:
        print("ERROR: no cookies read from the source browser.", file=sys.stderr)
        return 1

    context = _launch_isolated(pw, args)
    context.add_cookies(cookies)
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(verify_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5000)
    except Exception:  # noqa: BLE001
        pass
    acct = ""
    try:
        loc = page.get_by_role("button", name=re.compile(r"Google Account", re.I))
        if loc.count():
            acct = (loc.first.get_attribute("aria-label") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    cur = page.url
    signed_in = "accounts.google.com" not in cur and "signin" not in cur.lower()
    print(f"  isolated session: account={acct!r}  url={cur}")
    context.close()
    if signed_in and acct:
        print(f"\n✅ Cookies imported — the isolated profile is signed in as {acct}.")
        print("   Place a call with (no --cdp-url, no manual login):")
        print(
            f"     python scripts/meet_call_browser.py --browser {args.browser} "
            f"--authuser {args.authuser} --duration 60"
        )
        return 0
    print(
        "\n⚠️  Imported cookies but couldn't confirm a signed-in session — Google may "
        "have rejected the transfer for this browser. Try --login instead.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None, *, on_join=None, on_pickup=None) -> int:
    """on_join / on_pickup: optional zero-arg callbacks for an external audio engine
    (scripts/gemini_call.py). Both fire AT MOST ONCE; a callback exception is swallowed,
    never crashes the call.

      • on_join  — fires when the join is FIRST detected (any signal, incl. the flaky
        WebRTC track-COUNT bump a ringback can cause). Use for early, side-effect-free
        prep only — it can fire DURING the ringback before the callee truly answers.
      • on_pickup — fires only on a CONFIRMED real answer (ringback-safe): the callee's
        roster tile appeared (tiles≥2), OR a remote audio track actually unmuted, OR a
        live remote audio track exists. This is the signal to GREET on — greeting on
        on_join would be lost into the ringback (pre-answer audio never reaches the
        callee), the exact bug where the model 'said hi' to nobody."""
    parser = argparse.ArgumentParser(
        prog="meet_call_browser",
        description="Place the native ringing Google Chat call by driving a real "
        "logged-in browser with Playwright (no API can ring — only the UI can).",
    )
    parser.add_argument(
        "--space",
        default="",
        help="Chat DM/space to call into (id or spaces/<id>). Default: "
        "GOOGLE_VOICE_SPACE from .env (the bot↔Duc DM).",
    )
    parser.add_argument(
        "--authuser",
        type=int,
        default=0,
        help="which signed-in Google account index in the profile (default 0; the "
        "captured request used authuser 1 for the 2nd account).",
    )
    parser.add_argument(
        "--url",
        default="",
        help="exact page URL to open (overrides --space). MOST RELIABLE: open the "
        "DM in the window and copy your address-bar URL here.",
    )
    parser.add_argument(
        "--browser",
        default=DEFAULT_BROWSER,
        choices=tuple(_BROWSER_PATHS),
        help="automation browser family: 'chromium' = Playwright's bundled Chromium "
        f"(no system install needed; default), 'brave'/'chrome' = system binary. "
        "Overridden by --browser-path.",
    )
    parser.add_argument(
        "--browser-path",
        default="",
        help="explicit browser executable path (overrides --browser).",
    )
    parser.add_argument(
        "--profile-dir",
        default="",
        help="persistent browser profile dir (holds the Google login; gitignored). "
        "Default: a per-browser dir (.chromium-profile / .browser-profile / "
        ".chrome-profile).",
    )
    parser.add_argument(
        "--import-cookies",
        default="",
        metavar="CDP_URL",
        help="ONE-TIME setup (alternative to --login): pull the live login from a "
        "running browser at this CDP url (e.g. http://127.0.0.1:9222) — read-only — "
        "and inject it into the isolated profile, so it's signed in with NO manual "
        "login and WITHOUT disturbing the source session.",
    )
    parser.add_argument(
        "--cdp-url",
        default="",
        help="connect to an ALREADY-RUNNING browser over CDP instead of launching "
        "(e.g. http://127.0.0.1:9222 after `brave-browser --remote-debugging-port=9222`).",
    )
    parser.add_argument(
        "--button-name",
        default="",
        help="exact accessible name of the call button (from the --dry-run dump) if "
        "the auto-detect patterns miss it.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=90.0,
        help="MAX seconds to stay before auto-leaving (default 90). The script "
        "exits EARLY the moment the callee hangs up — this is just a safety cap.",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="don't auto-leave; stay until Ctrl+C.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="load the DM and LOCATE the call button (dump buttons), but do NOT "
        "click it.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="ONE-TIME setup: open the isolated profile at chat.google.com and wait "
        "(Ctrl+C to finish) so you can sign in as mikmikb26. Cookies persist in "
        "--profile-dir; later runs are already signed in. Ignored with --cdp-url.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run headless (NOT recommended — Google sign-in is blocked headless).",
    )
    parser.add_argument(
        "--load-timeout",
        type=float,
        default=45.0,
        help="seconds to wait for the DM + call button to appear (default 45).",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="extra browser CLI flag (repeatable), e.g. for virtual-audio wiring "
        "in the voice phase: --extra-arg=--use-fake-ui-for-media-stream",
    )
    parser.add_argument(
        "--watch-join",
        action="store_true",
        help="REAL-TIME: once we're in the call, detect the instant the callee JOINS "
        "(answers) by watching the live Meet roster ([data-participant-id] 1→2 + the "
        "'X joined' toast). Prints a timestamped event + a machine-readable "
        "PARTICIPANT_JOINED <name> line. Latency = --join-poll (sub-second).",
    )
    parser.add_argument(
        "--join-poll",
        type=float,
        default=0.5,
        help="seconds between roster polls while waiting for the callee to join "
        "(default 0.5 → join detected within ~0.5s of answering).",
    )
    parser.add_argument(
        "--diag-pickup",
        action="store_true",
        help="log a compact per-poll signal snapshot while ringing (roster/toast/webrtc/"
        "calling + effective poll cadence) to diagnose greeting latency. Self-limiting: "
        "stops once the callee is detected as joined.",
    )
    parser.add_argument(
        "--no-foreground",
        action="store_true",
        help="do NOT bring the call tab to the front after placing the call. By default "
        "the call tab is foregrounded so the DOM participant roster renders (a backgrounded "
        "tab throttles the DOM → tiles stay 0 → the roster-collapse HANG-UP signal can never "
        "fire and the script rides to the duration cap). Use this only if you must keep the "
        "tab backgrounded and are relying on the WebRTC/survey hang-up signals instead.",
    )
    parser.add_argument(
        "--diag-structure",
        action="store_true",
        help="DIAGNOSTIC: dump the live DOM structure (candidate tile selectors, "
        "<video>/<audio> elements, visibilityState) + the audio-tap PC/track inventory "
        "every few polls from the moment the call connects (not gated behind join). Use to "
        "find the current participant-tile selector + where the live media is when capture "
        "is silent or the roster reads 0.",
    )
    parser.add_argument(
        "--watch-rest",
        action="store_true",
        help="after the call connects, extract its Meet meeting code and poll the "
        "Meet REST API (conferenceRecords + participants = the room's data / live "
        "roster) in a background thread until the call ends. Reuses meet_rest_watch.",
    )
    parser.add_argument(
        "--rest-token",
        default="secrets/token_bot.json",
        help="OAuth token file for the REST watch — MUST be the call ORGANIZER's "
        "account (the caller = the bot) to query its conferenceRecords (default the "
        "bot = mikmikb26).",
    )
    parser.add_argument(
        "--rest-poll",
        type=float,
        default=2.0,
        help="seconds between Meet REST polls (default 2).",
    )
    parser.add_argument(
        "--rest-self-id",
        default="users/116566195804326411461",
        help="our users/<id> for the REST roster (everyone else = REMOTE); default "
        "the bot/caller id.",
    )
    parser.add_argument(
        "--rest-find-timeout",
        type=float,
        default=60.0,
        help="seconds to wait for the conferenceRecord to appear in the REST API "
        "(it's created when the conference STARTS, then propagates; default 60).",
    )
    parser.add_argument(
        "--capture-audio",
        action="store_true",
        help="CAPTURE the call's audio (the remote voices) to a WAV while it's live — "
        "the INPUT path for a future Gemini Live loop. Creates a dedicated null sink, "
        "moves the browser's playback stream into it, records its monitor at 16kHz "
        "mono PCM (Gemini Live's format). Routing is always restored. Needs "
        "pactl+ffmpeg (PipeWire/PulseAudio). See scripts/meet_audio_capture.py.",
    )
    parser.add_argument(
        "--audio-out",
        default="",
        help="output WAV path for --capture-audio (default "
        "reports/meet_audio_<timestamp>.wav).",
    )
    parser.add_argument(
        "--audio-mode",
        default="allsinks",
        choices=("webrtc", "monitor", "isolate", "profile", "allsinks"),
        help="how --capture-audio captures (default allsinks): 'allsinks' = record EVERY "
        "output sink's monitor with one recorder each + mix at stop — the VERIFIED path "
        "for Meet (user-confirmed ring+voice capture); multi-sink-safe (the call audio can "
        "land on a different HDA sink between calls). 'webrtc' = tap the inbound WebRTC "
        "track IN the browser (BLIND on Google Meet — decoded audio isn't a tappable "
        "track); 'monitor' = one resolved sink's monitor (can MISS the voice if it lands "
        "on another sink); 'isolate'/'profile' = null-sink + move streams (the move FAILED "
        "live — avoid). See meet_audio_capture.py.",
    )
    parser.add_argument(
        "--audio-proc-match",
        default="",
        help="(profile mode) substring uniquely identifying the caller browser's process "
        "tree — its --user-data-dir path. Sink-inputs owned by that tree are captured; "
        "all others (the daily browser, other apps) are ignored. Auto-derived from the "
        "--cdp-url browser's command line when omitted.",
    )
    parser.add_argument(
        "--capture-from-ring",
        action="store_true",
        help="(OS modes: monitor/isolate/profile) start the recorder the moment the call "
        "is PLACED, so the WAV includes the ringback tone, not just the post-answer voice. "
        "Default OFF: with --watch-join the recorder defers to the answer to skip the "
        "~27s of ringback. Use this to verify capture spans the whole call (ring → voice).",
    )
    parser.add_argument(
        "--audio-all-apps",
        action="store_true",
        help="(forces isolate mode) capture EVERY playback stream, not just the browser's. "
        "Moves ALL sink-inputs into one null sink and records it — the robust fix on a "
        "multi-sink machine where the call's RING and VOICE land on DIFFERENT sinks (here "
        "the HDA codec exposes 4 sinks; 'monitor' locks one sink at ring time and misses "
        "the voice). No app/sink match → can't miss the voice. Mutes desktop audio for the "
        "capture (restored on stop); use for a FOCUSED call test (the call is all that plays).",
    )
    parser.add_argument(
        "--inject-audio",
        nargs="?",
        const="",
        default=None,
        metavar="FILE",
        help="THE 'AI MOUTH' PATH: make the CALLER play audio the CALLEE hears. Creates a "
        "virtual PulseAudio mic (ai_mic) the browser grabs as its microphone, then plays "
        "FILE into it (ffmpeg-decodable: wav/mp3/…). Bare --inject-audio (no FILE) plays a "
        "generated 4-note test tone — unmistakable on the callee's device vs the ringback. "
        "Fully reversible: the previous default mic + modules are restored on exit. Verify "
        "the chain offline first: python scripts/meet_audio_inject.py --verify.",
    )
    parser.add_argument(
        "--inject-at-join",
        action="store_true",
        help="(with --inject-audio) start playback only when the callee ANSWERS (clean "
        "start). Default: play from ring — robust (no dependency on join detection); the "
        "callee hears it the instant they pick up (pre-answer audio never reaches them).",
    )
    parser.add_argument(
        "--inject-once",
        action="store_true",
        help="(with --inject-audio) play the file ONCE instead of looping until hang-up.",
    )
    parser.add_argument(
        "--ensure-mic-on",
        action="store_true",
        help="On join, make sure the bot's mic is ON (unmute it if Meet remembered a "
        "muted state). Independent of --inject-audio — use it when an EXTERNAL engine "
        "(e.g. scripts/gemini_call.py) owns the mic audio via a virtual default-source, "
        "so meet_call_browser only places/holds the call. A muted track transmits "
        "silence no matter what feeds the virtual mic.",
    )
    args = parser.parse_args(argv)
    if not args.profile_dir:
        args.profile_dir = DEFAULT_PROFILE_DIRS.get(
            args.browser, DEFAULT_PROFILE_DIRS["chromium"]
        )

    # --- target resolution -------------------------------------------------------
    from gchat_agent.config import load_config  # lazy: project module

    cfg = load_config(os.path.join(_REPO_ROOT, ".env"))
    space = args.space or cfg.GOOGLE_VOICE_SPACE or cfg.GOOGLE_SPACE
    url = args.url.strip() or (_default_url(space, args.authuser) if space else "")
    if not url:
        print(
            "ERROR: no target — pass --url, or --space / set GOOGLE_VOICE_SPACE.",
            file=sys.stderr,
        )
        return 2

    # --- Playwright import (friendly hint if missing) ----------------------------
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "ERROR: Playwright is not installed.\n"
            "  conda run -n igaming pip install playwright\n"
            "  For --browser chromium (default) also: playwright install chromium.\n"
            "  For --browser brave/chrome the system binary is used (no browser DL).",
            file=sys.stderr,
        )
        return 2

    print(f"=== Ringing call via browser → {space or url} ===")
    print(f"  target URL: {url}")

    # --- import-cookies mode: one-time login transfer, then exit -----------------
    if args.import_cookies:
        with sync_playwright() as pw:
            try:
                return _import_cookies(pw, args, url)
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR: cookie import failed: {exc}", file=sys.stderr)
                return 1

    if not args.cdp_url and not args.login:
        print("  (not signed in? run --import-cookies <cdp-url> or --login once)")

    browser = None
    context = None
    with sync_playwright() as pw:
        try:
            browser, context = _open_context(pw, args)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - friendly one-liner
            print(f"ERROR: could not open the browser: {exc}", file=sys.stderr)
            return 1

        # Pre-grant A/V so no permission prompt blocks the call (best-effort: the
        # CDP path may not support per-context grants).
        try:
            context.grant_permissions(
                ["microphone", "camera"], origin="https://meet.google.com"
            )
        except Exception:  # noqa: BLE001
            pass

        # Install the WebRTC hook BEFORE we open the call tab, so it's present when the
        # Meet iframe's RTCPeerConnection is created. It powers (a) join detection — the
        # remote-track count survives a BACKGROUNDED tab where DOM/roster rendering lags
        # — and (b) the inbound-audio tap when --capture-audio is in webrtc mode. The
        # capture flag must be set BEFORE the hook so the iframe sees it. Best-effort.
        _webrtc_capture = args.capture_audio and args.audio_mode == "webrtc"
        if args.watch_join or _webrtc_capture:
            try:
                if _webrtc_capture:
                    context.add_init_script("window.__MCB_CAPTURE = true;")
                context.add_init_script(_WEBRTC_HOOK)
            except Exception:  # noqa: BLE001
                pass

        # In CDP mode we're attached to your REAL browser with your tabs open — open
        # a fresh tab instead of navigating one of yours away. For a launched
        # profile the lone initial blank page is ours to use.
        if args.cdp_url:
            page = context.new_page()
        else:
            page = context.pages[0] if context.pages else context.new_page()

        # --- one-time login: open chat, wait for the human to sign in -------------
        if args.login and not args.cdp_url:
            try:
                page.goto("https://chat.google.com/", wait_until="domcontentloaded", timeout=60_000)
            except Exception:  # noqa: BLE001
                pass
            print(
                "\n🔐 Sign in as mikmikb26 in this window, open the Duc DM once, then\n"
                "   press Ctrl+C here to save the session. (Isolated profile: "
                f"{_resolve_path(args.profile_dir)})"
            )
            try:
                while True:
                    page.wait_for_timeout(1000)
            except KeyboardInterrupt:
                print("\n✅ Session saved. Re-run without --login to place the call.")
            return 0

        # Join-timing anchor: stamp each step of the bot's OWN join flow (nav → call
        # button → call page → green-room "Join now") when --diag-pickup is on. This is
        # the window BEFORE t_call: the callee can answer during it, but the bot can't
        # speak until it's through — so a slow step here IS the greeting latency.
        t_place0 = time.monotonic()
        _diag = bool(getattr(args, "diag_pickup", False))
        def _jstamp(label):  # noqa: ANN001
            if _diag:
                print(f"   [join] +{time.monotonic() - t_place0:5.1f}s  {label}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: navigation failed: {exc}", file=sys.stderr)
            return 1
        _jstamp("navigation done (DM page domcontentloaded)")

        # --- wait for the call button to appear ----------------------------------
        deadline = time.monotonic() + args.load_timeout
        button = None
        while time.monotonic() < deadline:
            button = _find_call_button(page, args.button_name)
            if button is not None:
                break
            page.wait_for_timeout(1000)
        _jstamp("call button found" if button is not None else "call button NOT found")

        if button is None:
            print(
                "\nERROR: could not find the call button on this page.",
                file=sys.stderr,
            )
            print(
                f"  current URL: {page.url}\n  title: {page.title()!r}",
                file=sys.stderr,
            )
            labels = _dump_buttons(page)
            if labels:
                print("  visible buttons (pass the right one via --button-name):", file=sys.stderr)
                for n in labels:
                    print(f"    - {n!r}", file=sys.stderr)
            else:
                print(
                    "  (no buttons visible — are you signed in? is this the DM? try "
                    "--url with the exact address-bar URL.)",
                    file=sys.stderr,
                )
            if args.keep_open:
                print("  --keep-open set: leaving the window up for inspection. Ctrl+C to quit.")
                try:
                    while True:
                        page.wait_for_timeout(1000)
                except KeyboardInterrupt:
                    pass
            return 1

        # --- dry run: located it, don't click ------------------------------------
        if args.dry_run:
            try:
                label = button.get_attribute("aria-label") or button.inner_text()
            except Exception:  # noqa: BLE001
                label = "(call button)"
            print(f"\n✅ Found the call button: {label!r} (NOT clicking — --dry-run).")
            print("  Re-run without --dry-run to place the call.")
            if args.keep_open:
                try:
                    while True:
                        page.wait_for_timeout(1000)
                except KeyboardInterrupt:
                    pass
            return 0

        # --- optional: INJECT audio into the bot's mic (the "AI mouth" path) ------
        # Set up the virtual mic BEFORE the call button click, so the bot's getUserMedia
        # grabs ai_mic as its microphone (default-source swap only affects NEW streams).
        # Playback starts from ring by default (robust — no join-detection dependency;
        # pre-answer audio never reaches the callee), or at the answer (--inject-at-join).
        # atexit-guarded so the previous default source + modules are always restored.
        injector = None
        if args.inject_audio is not None:
            import atexit
            try:
                from meet_audio_inject import AudioInjector
                injector = AudioInjector(args.inject_audio or None,
                                         loop=not args.inject_once)
                if injector.setup():
                    atexit.register(injector.stop)
                    if not args.inject_at_join:
                        injector.play()  # from ring
                else:
                    injector = None
            except Exception as exc:  # noqa: BLE001 - injection is best-effort
                print(f"   ⚠️  audio injection unavailable: {exc}")
                injector = None

        # --- click it → this RINGS the callee ------------------------------------
        # In this embedded Chat DM the call opens IN-PLACE (a Meet iframe inside the Chat
        # page), so there is normally NO popup. Click the button EXACTLY ONCE: after the
        # in-place navigation the button element is DETACHED and no longer actionable, so a
        # second/retry click stalls on Playwright's ~30s actionability timeout. THAT (an 8s
        # popup wait + a 30s detached re-click) was the ~38s "bot takes 40s to join" delay —
        # NOT renderer suspension (the first click returns instantly, proving the button is
        # actionable). Wait only briefly for a popup; if none appears it's in-place (common).
        call_page = page
        clicked = False
        popup_page = None
        try:
            with context.expect_page(timeout=3_000) as popup:
                button.click(timeout=15_000)
                clicked = True
            popup_page = popup.value            # a real popup opened
        except Exception as exc:  # noqa: BLE001
            if not clicked:                     # the click ITSELF failed (not just "no popup")
                print(f"   ⚠️  call button click failed: {exc}")
        if popup_page is not None:
            call_page = popup_page
        elif call_page is page:                 # in-place: prefer a meet.google.com tab if any
            for p in context.pages:
                if "meet.google.com" in (p.url or ""):
                    call_page = p
                    break

        _jstamp("call button clicked (callee starts ringing once we Join)")
        try:
            call_page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass
        _jstamp("call page loaded (domcontentloaded)")

        # 🔑 Keep the renderer AWAKE NOW — the instant the call page is up, BEFORE the
        # answer (+~15s) and media setup. On GNOME-Wayland an occluded renderer is fully
        # suspended → ICE dies → silent capture + no hang-up. Doing this here (not after the
        # green-room wait, ~+30-50s, as before) is what keeps the call's media alive.
        screencast_cdp = None
        if not args.no_foreground:
            screencast_cdp = _keepalive_renderer(call_page)

        # A DM video call usually opens a Meet pre-join ("green room"); the callee is
        # only RUNG once the caller commits by clicking "Join now". Click it if shown
        # (search ALL frames — the green room may live in a meet.google.com iframe).
        # Short window: a direct DM ring shows NO green room, so don't burn 30s on it.
        joined = False
        deadline = time.monotonic() + 12
        join_pats = (r"join now", r"^join$", r"join call", r"ask to join",
                     r"\bjoin\b")
        while time.monotonic() < deadline and not joined:
            jb = _find_button_in_frames(call_page, join_pats)
            if jb is not None:
                try:
                    label = jb.get_attribute("aria-label") or jb.inner_text() or "Join"
                    jb.click()
                    joined = True
                    print(f"   clicked {label!r} → connecting/ringing")
                except Exception:  # noqa: BLE001
                    pass
            if not joined:
                call_page.wait_for_timeout(1000)
        if not joined:
            # Dump frame buttons so a recurrence is debuggable (the failure that left
            # us in the green room, never in the meeting → roster empty → no detection).
            print("   (no 'Join now' button seen in 12s — visible buttons across "
                  "frames, so the real label is debuggable next run):")
            for fr in _frames(call_page):
                for n in _dump_buttons(fr):
                    print(f"     - {n!r}")
        _jstamp("'Join now' clicked → bot in call" if joined
                else "green-room loop gave up after 12s (no Join button)")
        print(f"\n📞 Call placed — ringing Duc. Call page: {call_page.url}")
        t_call = time.monotonic()  # reference for "time until the callee answered"
        # NOTE: the renderer keepalive (focus-emulation + screencast) already fired ABOVE,
        # right after the call page loaded — it MUST precede the answer/media-setup window,
        # not run here. Keeping it awake is what makes the DOM roster render (tiles>0), the
        # media survive (no ICE death), and the hang-up signal fire on this embedded UI.

        # --- optional: CAPTURE the call audio (Gemini Live input path) -----------
        # Purely additive, bracketing the call from here to teardown. Two paths:
        #   webrtc  → BrowserAudioTap: drains the in-browser MediaRecorder (the REMOTE
        #             voice, tapped at the media layer). Recording already started via
        #             the hook when the inbound audio track arrived; we just drain it.
        #   monitor/isolate → AudioCapture: an OS-level ffmpeg recorder (the desktop
        #             output mix, coarser). atexit-guarded so routing is restored.
        audio_cap = None   # OS-level (monitor/isolate)
        audio_tap = None   # browser WebRTC inbound tap
        audio_start_at_join = False  # defer OS recorder start to answer (skip ringback)
        if args.capture_audio:
            out = args.audio_out or os.path.join(
                _REPO_ROOT, "reports",
                time.strftime("meet_audio_%Y%m%d_%H%M%S.wav"))
            try:
                if args.audio_mode == "webrtc" and not args.audio_all_apps:
                    from meet_audio_capture import BrowserAudioTap
                    audio_tap = BrowserAudioTap(call_page, out)
                    if not audio_tap.start():
                        audio_tap = None
                else:
                    import atexit
                    from meet_audio_capture import AudioCapture
                    # 'profile' = isolate scoped to ONLY the caller browser's process
                    # tree (call-only decoded audio — the robust path for Meet, whose
                    # WebRTC track the in-browser tap can't reach). proc_match is the
                    # caller's --user-data-dir: explicit flag, else auto-derived from CDP.
                    cap_mode = args.audio_mode
                    proc_match = None
                    match_all = False
                    if args.audio_all_apps:
                        # All-apps wins: force isolate + capture every stream (the robust
                        # multi-sink fix — see --audio-all-apps help). Ignores profile/proc.
                        cap_mode = "isolate"
                        print("   [audio] --audio-all-apps: capturing EVERY playback stream "
                              "via one null sink (multi-sink-safe; desktop audio muted for "
                              "the capture, restored after).")
                        match_all = True
                    elif args.audio_mode == "profile":
                        cap_mode = "isolate"
                        proc_match = (args.audio_proc_match.strip()
                                      or _derive_proc_match(args.cdp_url))
                        if proc_match:
                            print(f"   [audio] profile mode: scoping capture to caller "
                                  f"process tree (match: {proc_match!r})")
                        else:
                            print("   ⚠️  [audio] profile mode: could not derive the caller "
                                  "process match (pass --audio-proc-match) — falling back "
                                  "to broad isolate (NOT call-scoped).")
                    audio_cap = AudioCapture(out, mode=cap_mode, proc_match=proc_match,
                                             match_all=match_all)
                    atexit.register(audio_cap.stop)  # restore routing even on hard exit
                    # Start the OS recorder ONLY when the callee answers (join), so the
                    # WAV holds the CALL audio, not the ~27s of ringback before answer.
                    # Without join-watching we can't tell ringing from answered, so start
                    # now (old behavior). --capture-from-ring also forces an immediate
                    # start so the WAV spans the whole call (ringback → voice).
                    if args.watch_join and not args.capture_from_ring:
                        audio_start_at_join = True
                        print("   [audio] will start recording when the call is answered "
                              "(skipping the ringback) …")
                    else:
                        if args.capture_from_ring:
                            print("   [audio] --capture-from-ring: recording from NOW "
                                  "(includes the ringback tone) …")
                        if not audio_cap.start():
                            audio_cap = None
            except Exception as exc:  # noqa: BLE001 - audio is best-effort
                print(f"   ⚠️  audio capture unavailable: {exc}")
                audio_cap = audio_tap = None

        # --- confirm we're IN the call (a leave/hang-up control appears) ----------
        # 'in call' = WE joined and it's ringing/connected (we can't cleanly tell
        # ringing from answered). The point is to establish the leave control as a
        # BASELINE so its later disappearance reliably means the call ended.
        in_call = False
        connect_deadline = time.monotonic() + 25
        while time.monotonic() < connect_deadline:
            if _in_call(call_page):
                in_call = True
                break
            # Roster fallback: our own tile ([data-participant-id] ≥ 1) means we're in
            # the call even when the leave-control label isn't matched (it drifts).
            try:
                if _participant_join_state(call_page)[0] >= 1:
                    in_call = True
                    break
            except Exception:  # noqa: BLE001
                pass
            call_page.wait_for_timeout(1000)
        if in_call:
            print("   ✅ In the call (ringing/connected). Watching for it to end …")
        else:
            print(
                "   ⚠️  couldn't confirm the in-call controls (their label drifts) — "
                "hang-up is still auto-detected via the call iframe teardown."
            )

        # --- extract the Meet meeting code (the call's room id), for the REST API -
        # The URL settles a beat after Join; retry briefly and scan the page frames
        # as a fallback. If we still can't read it, REST falls back to --auto (it
        # finds the caller's currently-active conference without an explicit code).
        meeting_code = _extract_meeting_code(call_page.url)  # instant (no wait)
        # The retry loop below polls with 1s waits (up to 5s) of PURE DEAD TIME that
        # BLOCKS pickup/greet detection from starting. The meeting code only feeds the
        # REST room-watch, so only pay that cost when --watch-rest is on. Without REST
        # (the gemini_call greeting path) the pickup loop now starts ~5s SOONER — this
        # was the dominant cause of the "answered, then ~10s of silence before the AI
        # greeted" latency: the user picked up DURING this blocking setup.
        if not meeting_code and args.watch_rest:
            for _ in range(5):
                call_page.wait_for_timeout(1000)
                meeting_code = (_extract_meeting_code(call_page.url)
                                or _scan_page_for_code(call_page))
                if meeting_code:
                    break
        if meeting_code:
            print(f"   meeting code: {meeting_code}")
            print(f"MEETING_CODE {meeting_code}")  # machine-readable handoff line
        elif args.watch_rest:
            print("   ⚠️  couldn't parse a meeting code from the call URL — REST "
                  "will use --auto (the caller's active conference).")

        # --- optional: watch the room's data over REST while the call is live ----
        rest_stop = rest_thread = None
        if args.watch_rest:
            rest_stop, rest_thread = _start_rest_watch(cfg, args, meeting_code)

        # --- stay in the call, but EXIT THE MOMENT IT ENDS -----------------------
        # --duration is a MAX cap. PRIMARY end signal: the leave/hang-up control
        # DISAPPEARS after we'd seen it — the call dropped (in a 1:1 call, either side
        # hanging up ends it for both). SECONDARY: we linger alone. We deliberately do
        # NOT match 'Call ended' TEXT: a DM renders call-HISTORY cards with that exact
        # text in the same frame, which false-positives instantly (the bug we hit).
        ended_reason = None
        gone_for = 0   # consecutive polls the leave control has been absent
        alone_for = 0  # consecutive polls we've been alone in the call
        collapse_for = 0  # consecutive polls the roster collapsed to 0 (post-join)
        webrtc_gone_for = 0  # consecutive polls live remote audio dropped to 0 (post-join)
        audio_stream_gone_for = 0  # consecutive polls the caller's call audio stream was absent (OS/profile mode)
        peak_tiles = 0  # highest roster tile count seen (0 ⇒ roster never rendered here)
        peak_live = 0   # highest live remote-audio-track count seen (0 ⇒ no remote audio)
        # Inbound-RTP flatline = the robust hang-up signal (media stops when the remote leaves).
        prev_bytes = -1        # last inbound-RTP byte total seen
        bytes_flat_for = 0     # consecutive polls inbound bytes did NOT grow (post-flow)
        bytes_ever_grew = False  # has inbound media meaningfully flowed at least once?
        # media_connected: a LATCH set the first time we observe REAL inbound media —
        # a remote audio track 'unmute' event (RTP started flowing; the primary, can't-be-
        # missed signal — see the latch just after join detection), a live non-ended remote
        # audio track, or inbound-RTP bytes actually growing.
        # The hard invariant it enforces: you cannot "hang up" from a call that never
        # carried media. Every FRAGILE teardown signal (controls-disappeared, frame
        # torn down, PC closed, "alone") is gated on it — so the ring→connect UI flicker,
        # and the stale/already-CLOSED ringback PeerConnections the meet client leaves in
        # window.__mcbPCs (cs:closed, all receivers rs:ended), can no longer be misread as
        # a hang-up before the real post-answer media PC is even established. (The bug that
        # false-stopped the script ~2.5s after the callee answered, capturing silence.)
        media_connected = False
        join_fired = False  # have we already announced the callee joining?
        join_via_dom = False  # did the join fire via a real-answer DOM signal (not webrtc)?
        pickup_fired = False  # have we fired the CONFIRMED-answer (on_pickup) callback?
        t_join = 0.0        # when the join fired (for the post-join settle grace below)
        calling_seen = False   # have we seen the "Calling…/ringing" indicator on screen?
        calling_gone_for = 0   # consecutive polls it's been ABSENT after being seen
        calling_armed_logged = False  # one-shot "calling indicator armed" log
        frame_seen = False   # have we seen the Meet call iframe attached (post-join)?
        frame_gone_for = 0   # consecutive polls the call iframe has been absent
        frame_confirmed_logged = False  # one-shot "CDP frame armed" diagnostic log
        feedback_for = 0     # consecutive polls the post-call rating survey was visible
        survey_clear_seen = False  # have we seen NO survey since placing? (arms survey-as-hangup)
        survey_armed_logged = False  # one-shot "survey hang-up armed" log
        hangup_dump_done = False  # one-shot page-text dump at first end signal (diagnostic)
        dbg_polls = 0   # post-join diagnostic dumps emitted for the audio tap
        last_struct = 0.0  # --diag-structure throttle (dump live DOM/capture state every ~3s)
        # WebRTC baseline: absorb OUR OWN inbound tracks (the SFU allocates a few
        # receive tracks as the caller connects) during a short settle window after
        # the call is placed, so their ramp-up never reads as a remote join. After the
        # window, any track count ABOVE this baseline is the callee's media arriving.
        base_tracks = 0
        settle_until = t_call + 6
        # Faster cadence when watching for the join so latency ≈ --join-poll.
        wait_ms = max(100, int(args.join_poll * 1000)) if args.watch_join else 1000
        cap = None if args.keep_open else time.monotonic() + args.duration
        # Pickup-latency diagnostics: log a compact per-poll signal snapshot WHILE ringing
        # (self-limiting — stops the instant join fires) so we can see exactly when each
        # real-answer signal flips and what our EFFECTIVE poll cadence is (slow CDP evals
        # can stretch a nominal 0.5s poll to seconds → that, not Google, is the lag). Opt-in
        # via --diag-pickup so normal runs stay quiet.
        ring_last_poll = t_call
        try:
            while cap is None or time.monotonic() < cap:
                # REAL-TIME join detection — three independent signals, ANY fires it:
                #   (a) roster tile count rises to ≥2  — DOM, instant when foreground
                #   (b) an 'X joined' toast appears    — DOM, gives us the name
                #   (c) inbound remote-track count grows past the settled baseline —
                #       WebRTC media layer, which keeps firing even when the call tab
                #       is BACKGROUNDED (you switched to another tab), so this is the
                #       signal that doesn't lag while you work elsewhere.
                # Deliberately NOT gated on `in_call` — the probes are independent of
                # the (flaky) leave-control heuristic, so a false-negative in_call must
                # never disable join detection (the bug that missed a live join).
                if args.watch_join and not join_fired:
                    now = time.monotonic()
                    cnt, who = _participant_join_state(call_page)
                    trk = _webrtc_track_count(call_page)
                    if now < settle_until:
                        base_tracks = max(base_tracks, trk)  # absorb our own ramp
                    webrtc_join = now >= settle_until and trk > base_tracks
                    # (d) DOM "Calling…" indicator DISAPPEARS — the user-observed pickup
                    # signal. Pure-DOM, independent of the (flaky) WebRTC media layer, so it
                    # catches the answer even when media is throttled/never connects. Arm
                    # only after we've SEEN the indicator (so its absence on a page that
                    # never showed it can't false-fire), debounced 2 polls.
                    calling = _calling_indicator_present(call_page)
                    if calling:
                        if not calling_seen:
                            calling_seen = True
                        calling_gone_for = 0
                        if not calling_armed_logged:
                            calling_armed_logged = True
                            print(f"   [join] ring indicator on screen (matched: {calling!r}) "
                                  "— pickup will fire when it disappears")
                    elif calling_seen:
                        calling_gone_for += 1
                    calling_join = calling_seen and calling_gone_for >= 2
                    if getattr(args, "diag_pickup", False):
                        gap = now - ring_last_poll
                        ring_last_poll = now
                        print(f"   [ring] +{now - t_call:5.2f}s  tiles={cnt} "
                              f"toast={'Y' if who else '-'} trk={trk}/{base_tracks} "
                              f"webrtc={'Y' if webrtc_join else '-'} "
                              f"calling={'Y' if calling else '-'}(gone {calling_gone_for}) "
                              f"| poll_gap={gap:4.2f}s")
                    if cnt >= 2 or who or webrtc_join or calling_join:
                        join_fired = True
                        t_join = now
                        lat = now - t_call
                        name = who or "(participant)"
                        src = ("roster" if cnt >= 2 else "") + \
                              (" toast" if who else "") + \
                              (" webrtc" if webrtc_join else "") + \
                              (" calling-gone" if calling_join else "")
                        # Did the join come via a real-answer DOM signal (roster tile /
                        # 'joined' toast / 'Calling…' indicator gone)? Those mean the callee
                        # truly answered regardless of whether their MIC is on — so an
                        # external engine can greet even on a SILENT/muted pickup. The pure
                        # WebRTC track-COUNT bump (webrtc_join) is EXCLUDED: a ringback can
                        # cause it, so it alone is not proof of a real answer.
                        join_via_dom = (cnt >= 2) or bool(who) or calling_join
                        print(f"\n🔔 REMOTE JOINED: {name}  (+{lat:.1f}s after the "
                              f"call was placed; tiles={cnt}, tracks={trk}/base={base_tracks}, "
                              f"via={src.strip()})")
                        print(f"PARTICIPANT_JOINED {name}")  # machine-readable handoff
                        # Start the OS recorder NOW (call answered) — the WAV begins at
                        # the moment of answer, with no ringback prefix.
                        if audio_start_at_join and audio_cap is not None:
                            if not audio_cap.start():
                                audio_cap = None
                            audio_start_at_join = False
                        # AI-mouth: the callee answered → (1) make sure the bot mic is ON
                        # (a muted track transmits silence), (2) force its live mic
                        # source-output onto ai_mic (covers a getUserMedia that pinned a
                        # device instead of "default"), (3) start playback if deferred.
                        if injector is not None:
                            print(f"   [inject] bot mic: {_ensure_mic_on(call_page)}")
                            injector.move_browser_mic()
                            if args.inject_at_join:
                                injector.play()
                        elif args.ensure_mic_on:
                            # No local injector — an external engine (gemini_call.py)
                            # owns the mic via a virtual default-source. Just make sure
                            # the track isn't muted so that audio actually transmits.
                            print(f"   [mic] {_ensure_mic_on(call_page)}")
                        # Fire the answer callback ONCE (after the mic is unmuted), so an
                        # external engine can greet exactly when the callee picks up.
                        if on_join is not None:
                            try:
                                on_join()
                            except Exception as cb_exc:  # noqa: BLE001
                                print(f"   ⚠️  on_join callback error: {cb_exc}")
                            on_join = None

                # Latch "real media connected" as robustly as possible. media_connected
                # gates every FRAGILE teardown signal (so they can't false-fire on the
                # ringback). The polled peak_live / inbound-byte checks in the teardown
                # block latch it too — but they can MISS a real connection that comes and
                # goes BETWEEN polls (a call whose media flickered on then dropped, e.g. a
                # renderer suspend), leaving the gate shut so a genuine hang-up is never
                # detected and the call holds to the cap. (The bug a live human reporter
                # hit: answered, spoke ~30s, hung up — yet the WAV ran the full duration
                # and no hang-up fired.) An 'unmute' on a remote audio track is the missing
                # signal: monotonic (a poll can't miss it) and ringback-safe (a PC that
                # never carries media never fires it). Latch on it the moment it's seen.
                if join_fired and not media_connected and _webrtc_unmute_seen(call_page) >= 1:
                    media_connected = True
                    print("   [media] remote audio unmuted — media-connected latched "
                          "(hang-up detection armed)")

                # CONFIRMED-ANSWER callback for an external audio engine (gemini_call.py):
                # fire on_pickup the moment the callee TRULY answered, never on the ringback.
                # The contract: pick up → the AI greets immediately, even if the callee stays
                # SILENT (no need to speak / unmute first). So the trigger must NOT depend on
                # the callee's audio:
                #   • join_via_dom (roster tile / 'joined' toast / 'Calling…' indicator gone)
                #     is a real answer independent of the callee's mic → greet at once. This
                #     is the common foreground case (the caller window is kept visible).
                #   • else the join was only the WebRTC track-COUNT bump, which a ringback can
                #     cause — so require a ringback-safe confirmation (roster tile now, or a
                #     remote audio track unmuted / live) before greeting, so we never greet
                #     into the ring. NO settle-window gate here (these are real-answer signals,
                #     not the caller's own track ramp) so a FAST pickup greets without delay.
                if on_pickup is not None and not pickup_fired and join_fired:
                    real_answer = join_via_dom
                    if not real_answer:
                        try:
                            real_answer = (
                                _participant_join_state(call_page)[0] >= 2
                                or _webrtc_unmute_seen(call_page) >= 1
                                or _webrtc_live_audio(call_page) >= 1)
                        except Exception:  # noqa: BLE001
                            real_answer = False
                    if real_answer:
                        pickup_fired = True
                        print("   [pickup] callee answered for real — firing the "
                              f"pickup/greet callback (via_dom={join_via_dom})")
                        try:
                            on_pickup()
                        except Exception as cb_exc:  # noqa: BLE001
                            print(f"   ⚠️  on_pickup callback error: {cb_exc}")
                        on_pickup = None

                # OS-capture path (monitor/isolate/profile) is BLIND to the WebRTC media
                # layer on Google Meet, so the unmute/live-track/inbound-byte latches above
                # NEVER fire — media_connected would stay False forever and gate OFF every
                # teardown signal (the bug: every OS-mode run held to the duration cap with
                # no hang-up). Latch from OS evidence instead: in 'profile' mode the caller's
                # OWN audio sink-input being present post-join is real proof the call's
                # decoded media is flowing; otherwise fall back to join + a settle grace.
                if (join_fired and not media_connected and audio_cap is not None
                        and (audio_cap.stream_seen() or time.monotonic() - t_join >= 10)):
                    media_connected = True
                    print("   [media] OS-capture media-connected latched "
                          "(hang-up detection armed)")

                # PRIMARY hang-up signal for this embedded Chat DM huddle (user's tip,
                # confirmed live): the call iframe does NOT tear down on hang-up here (CDP
                # keeps listing it) and the in-call controls are flaky — but a "Rate the
                # meeting N star(s) out of 5" survey pops the instant the call ends. So we
                # watch for that survey EVERY poll, decoupled from join detection (a missed
                # join must never disable hang-up detection — the old bug). ARM-AFTER-ABSENT:
                # only treat the survey as a hang-up once we've seen it ABSENT since placing,
                # so a survey LEFT OVER from a previous call (it can linger on the page)
                # can't end the new call prematurely. Debounced 2 polls.
                try:
                    survey = _post_call_feedback_present(call_page)
                except Exception:  # noqa: BLE001
                    survey = ""
                if not survey:
                    if not survey_clear_seen:
                        survey_clear_seen = True
                    feedback_for = 0
                elif survey_clear_seen:
                    feedback_for += 1
                    if feedback_for == 1:
                        print(f"   [end] post-call rating survey detected "
                              f"(matched: {survey!r}) — confirming hang-up …")
                        if not hangup_dump_done:
                            hangup_dump_done = True
                            snip = _page_text_snippet(call_page)
                            if snip:
                                print(f"   [end-dbg] page text @ hang-up: {snip!r}")
                    if feedback_for >= 2:
                        ended_reason = "post-call rating survey shown (hung up)"
                        break
                # one-shot: confirm the survey signal is armed (active call, no survey up)
                if survey_clear_seen and not survey_armed_logged:
                    survey_armed_logged = True
                    print("   [end] active call confirmed (no rating survey up) — hang-up "
                          "will auto-stop the script when the rating survey appears")

                # Self-terminate once the call tears down, EVEN IF the in_call control
                # check never confirmed (it's flaky). CRITICAL: the END signal must
                # MIRROR the JOIN signal. In this embedded call UI the DOM roster never
                # renders tiles (join is caught via WebRTC), so a tiles==0 reading is the
                # STEADY STATE — treating it as a "collapse" tore the call down ~1.5s
                # after the callee answered, before any audio was captured (the bug).
                # So trust roster-collapse ONLY if the roster genuinely rose (peak_tiles
                # ≥ 1); otherwise end on the WebRTC media layer that detected the join:
                # the PC closing or the remote audio track ending (→ live count back to 0).
                # Post-join SETTLE GRACE: the in-call controls / call frame flicker during
                # the first seconds after answer (re-render), which false-fired "controls
                # disappeared (hung up)" ~30s before the callee actually left. Hold off the
                # teardown signals until the UI has stabilized.
                if join_fired and time.monotonic() - t_join >= 8:
                    end_now = False
                    # PRIMARY hang-up signal for this embedded UI: the Meet call iframe is
                    # torn down when the huddle ends. Checked FIRST and OUTSIDE the page-eval
                    # try below, because those evals (_participant_join_state / _webrtc_*)
                    # THROW once the call frame is detached at hang-up — and when they were
                    # ahead of this check in a shared try/except, the exception swallowed the
                    # whole block so this never ran and the call held to the cap (the bug).
                    # The check is a pure CDP /json HTTP GET, immune to a torn-down page; only
                    # armed once the frame was actually seen post-join; debounced.
                    fp = _call_frame_present(call_page, args.cdp_url)
                    if fp is True:
                        frame_seen = True
                        frame_gone_for = 0
                        if not frame_confirmed_logged:
                            frame_confirmed_logged = True
                            print("   [end] call frame confirmed via CDP — hang-up will "
                                  "auto-stop the script within ~1.5s of teardown")
                    elif fp is False and frame_seen and media_connected:
                        # gated on media_connected: a frame detach/re-render DURING connect
                        # (before any media flowed) is a transition, not a hang-up.
                        frame_gone_for += 1
                        if frame_gone_for == 1:
                            print("   [end] call frame disappeared — confirming hang-up …")
                        if frame_gone_for >= 3:  # debounce a transient detach/re-render
                            ended_reason = "call frame closed (hung up)"
                            end_now = True
                    # fp is None → CDP query inconclusive this poll; leave counters as-is
                    # OS/profile hang-up signal: the CALLER's own call-audio sink-input
                    # DISAPPEARED after being present (the call's audio element was torn
                    # down on hang-up → the stream is removed). WebRTC-independent — the
                    # robust end signal when the in-browser media layer is blind. Already
                    # inside the post-join settle grace, so the ringback→connect transition
                    # can't false-fire it; debounced 3 polls.
                    if not end_now and audio_cap is not None and audio_cap.lost_stream():
                        audio_stream_gone_for += 1
                        if audio_stream_gone_for == 1:
                            print("   [end] caller audio stream disappeared — "
                                  "confirming hang-up …")
                        if audio_stream_gone_for >= 3:
                            ended_reason = "caller audio stream ended (hung up)"
                            end_now = True
                    elif audio_cap is not None:
                        audio_stream_gone_for = 0
                    # Secondary WebRTC/roster end signals — best-effort; may throw once the
                    # call frame is gone, so they're isolated in their own try and run only
                    # while the primary signal hasn't already fired.
                    if not end_now:
                        try:
                            cnt_now = _participant_join_state(call_page)[0]
                            live_now = _webrtc_live_audio(call_page)
                            peak_tiles = max(peak_tiles, cnt_now)
                            peak_live = max(peak_live, live_now)
                            # Latch real-media-connected the moment a live remote audio
                            # track is observed (the RTP-bytes branch below latches it too).
                            if peak_live >= 1:
                                media_connected = True
                            if peak_tiles >= 1 and cnt_now == 0:
                                collapse_for += 1
                                if collapse_for >= 3:  # debounce a transient re-render
                                    ended_reason = "roster collapsed to 0 (call ended)"
                                    end_now = True
                            elif peak_tiles >= 1:
                                collapse_for = 0
                            # PC-closed is only a hang-up AFTER real media flowed. The meet
                            # client leaves stale ringback PeerConnections in __mcbPCs already
                            # closed (all receivers ended) during connect — ungated, those
                            # false-fired this the instant the call was placed.
                            if not end_now and media_connected and _webrtc_pc_dead(call_page):
                                ended_reason = "WebRTC connection closed (call ended)"
                                end_now = True
                            if not end_now and peak_live >= 1 and live_now == 0:
                                webrtc_gone_for += 1
                                if webrtc_gone_for >= 3:  # debounce a brief mute/reneg
                                    ended_reason = "remote audio track ended (call ended)"
                                    end_now = True
                            elif peak_live >= 1:
                                webrtc_gone_for = 0
                            # MOST ROBUST hang-up signal: inbound RTP flatlines when the
                            # remote leaves (the SFU keeps the PC/tracks alive, so only the
                            # media itself stops). Independent of any DOM/rendering.
                            if not end_now:
                                b = _webrtc_inbound_bytes(call_page)
                                if b >= 0:
                                    if prev_bytes >= 0 and b > prev_bytes + 500:
                                        bytes_ever_grew = True   # media is/was flowing
                                        media_connected = True
                                        bytes_flat_for = 0
                                    elif prev_bytes >= 0 and bytes_ever_grew:
                                        bytes_flat_for += 1      # no growth → remote silent/gone
                                        if bytes_flat_for == 1:
                                            print(f"   [end] inbound media stopped (bytes={b}) — "
                                                  "confirming hang-up …")
                                        if bytes_flat_for >= 6:  # ~3s of no media (post-flow)
                                            ended_reason = "inbound media stopped (hung up)"
                                            end_now = True
                                    prev_bytes = b
                        except Exception:  # noqa: BLE001
                            pass
                    if end_now:
                        break
                # Same post-join settle grace as above: the in-call controls flicker for the
                # first seconds after answer, which false-fired "controls disappeared" ~30s
                # before the callee actually left. Only trust this teardown signal once the
                # join fired AND the UI has had time to stabilize. (Before join, in_call is the
                # ringing/connecting state — never treat its flicker as a hang-up.)
                # Gated on media_connected: the in-call control bar flickers absent during
                # the ring→connect re-render (right as the callee answers). Before any media
                # has flowed that flicker is NOT a hang-up — it false-stopped the script ~2.5s
                # after answer, before the real media PC was established (capturing silence).
                if in_call and join_fired and media_connected and time.monotonic() - t_join >= 8:
                    if not _in_call(call_page):
                        gone_for += 1
                        if gone_for >= 3:  # debounce a transient re-render
                            ended_reason = "call controls disappeared (hung up)"
                            break
                    else:
                        gone_for = 0
                        if _alone_signal(call_page):
                            alone_for += 1
                            if alone_for >= 5:  # callee left; we weren't auto-dropped
                                ended_reason = "callee left (we were alone)"
                                break
                        else:
                            alone_for = 0
                # Drain the in-browser audio recorder's chunks out to disk as they
                # arrive (webrtc capture mode); cheap no-op between 1s chunks.
                if audio_tap is not None:
                    audio_tap.drain()
                    # One-shot diagnostic: dump the in-page capture state a few times
                    # right after join so a "no audio captured" outcome is debuggable
                    # (which frame has the PC, did the recorder start, chunks growing?).
                    if join_fired and dbg_polls < 9 and not args.diag_structure:
                        dbg_polls += 1
                        if dbg_polls in (1, 4, 9):
                            for u, st in audio_tap.debug_state():
                                print(f"   [audio-dbg p{dbg_polls}] {u} → {st}")
                # --diag-structure: dump live DOM + capture state from call-start (ungated by
                # the late join) so a silent-capture / tiles=0 run shows the LIVE structure.
                if args.diag_structure:
                    _tnow = time.monotonic()
                    if _tnow - last_struct >= 3:
                        last_struct = _tnow
                        rel = _tnow - t_call
                        print(f"   --- diag @ +{rel:.0f}s (join_fired={join_fired}) ---")
                        _dump_structure(call_page, f"+{rel:.0f}s")
                        # ICE candidate-pair diagnostics — the decisive WHY-no-media read.
                        try:
                            ice = _webrtc_ice_stats(call_page)
                        except Exception:  # noqa: BLE001
                            ice = []
                        for i, e in enumerate(ice):
                            print(f"   [ice +{rel:.0f}s] pc{i} ics={e.get('ics')} "
                                  f"igs={e.get('igs')} loc={e.get('loc')} rem={e.get('rem')} "
                                  f"sel={e.get('sel')} pairs={e.get('pairs')} inB={e.get('inB')}")
                        if audio_tap is not None:
                            for u, st in audio_tap.debug_state():
                                print(f"   [audio-dbg +{rel:.0f}s] {u[:50]} → {st}")
                call_page.wait_for_timeout(wait_ms)
        except KeyboardInterrupt:
            print("\n   Ctrl+C — leaving the call.")

        if ended_reason:
            print(f"\n📴 Call ended — {ended_reason}. Stopping.")
        elif not args.keep_open:
            print(f"\n⏱  Reached the {args.duration:.0f}s cap with no hang-up detected — leaving.")

        # Stop the screencast keepalive (auto-stops on tab close too; best-effort).
        if screencast_cdp is not None:
            try:
                screencast_cdp.send("Page.stopScreencast")
            except Exception:  # noqa: BLE001
                pass

        # Signal the REST watch thread to stop, then let it drain its final output.
        if rest_stop is not None:
            rest_stop.set()
        if rest_thread is not None:
            rest_thread.join(timeout=10)

        # Stop the audio capture: finalize the WAV (+ restore routing for OS modes).
        # The webrtc tap must flush BEFORE we hang up / close the tab (it evaluates JS
        # on the still-open call page to stop the recorder + drain the tail).
        audio_path = None
        if audio_tap is not None:
            audio_path = audio_tap.stop()
        elif audio_cap is not None:
            audio_path = audio_cap.stop()
        if audio_path:
            print(f"🎙  Call audio saved: {audio_path}")
            print(f"CALL_AUDIO {audio_path}")  # machine-readable handoff (→ Gemini)

        # Stop the AI-mouth injection: kill the player, restore the previous default
        # mic, unload the virtual-mic modules (atexit also covers a hard exit).
        if injector is not None:
            injector.stop()
            print("   [inject] virtual mic torn down (default source restored).")

        # Best-effort hang up on OUR side (frame-aware) if still connected.
        leave = _find_button_in_frames(call_page, _IN_CALL_PATTERNS)
        if leave is not None:
            try:
                leave.click()
                call_page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001
                pass

        # In CDP mode we attached to YOUR browser — close ONLY the tab(s) WE opened so
        # the script doesn't leave a trail of call/DM tabs behind (a launched isolated
        # profile is torn down with its context, so skip it there).
        if args.cdp_url:
            for p in {page, call_page}:
                try:
                    p.close()
                except Exception:  # noqa: BLE001
                    pass

        print("✅ Done.")
        return 0


# AUDIO (next phase) — make the AI actually TALK on this call
# -----------------------------------------------------------
# Playwright drives the PAGE; the call's audio is WebRTC, routed by the OS. To put
# an AI voice on the call once this browser is IN it:
#   1. Create virtual PulseAudio/PipeWire devices:
#        pactl load-module module-null-sink sink_name=ai_speaker      # AI hears Meet
#        pactl load-module module-null-sink sink_name=ai_mic_sink     # AI speaks in
#        pactl load-module module-remap-source source_name=ai_mic master=ai_mic_sink.monitor
#   2. Launch this browser pinned to them (per-app routing via pavucontrol, or run
#      under a PULSE_SINK/PULSE_SOURCE env so Brave uses ai_mic as its microphone
#      and ai_speaker as its output).
#   3. Run the Gemini Live loop from scripts/demo_incident_call.py with pyaudio
#      input = ai_speaker.monitor (Meet audio in) and output = ai_mic_sink (AI voice
#      out). The existing loop already does 16 kHz in / 24 kHz out bidirectional.
# That keeps the ring (this script) and the brain (Gemini Live) decoupled.
if __name__ == "__main__":
    raise SystemExit(main())
