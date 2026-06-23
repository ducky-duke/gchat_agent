"""Page-probe helpers for meet_call_browser: read the live Google Meet / Chat
call page (buttons, roster, WebRTC stats, survey/ring text) and the small path +
pattern constants they share. Pure helpers — no browser launch, no CLI."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

from meet_call_js import (
    _FEEDBACK_PROBE,
    _ICE_STATS_FN,
    _INBOUND_BYTES_FN,
    _JOIN_PROBE,
    _OUTBOUND_BYTES_FN,
    _STRUCTURE_PROBE,
)


# Allow running straight from a checkout without installing the package.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))


_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))


sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))


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


def _webrtc_outbound_bytes(page) -> int:
    """Max cumulative outbound-RTP bytesSent across the call PeerConnections — the media WE
    send to the callee. Grows while we send (e.g. the AI is speaking); flatlines when we go
    silent. The caller pairs this with _webrtc_inbound_bytes so a one-sided AI monologue (we
    talk, they listen → inbound flat, outbound growing) is NOT misread as a hang-up. Returns
    -1 only if every frame's probe failed (inconclusive)."""
    best = -1
    for fr in _frames(page):
        u = fr.url or ""
        if "meet.google.com" not in u and "chat.google.com" not in u:
            continue
        try:
            v = int(fr.evaluate(_OUTBOUND_BYTES_FN))
        except Exception:  # noqa: BLE001 - probe must never raise into the loop
            continue
        if v > best:
            best = v
    return best


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
