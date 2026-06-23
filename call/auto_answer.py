#!/usr/bin/env python
"""Auto-answer the CALLEE side of a native Google Chat DM call — for UNATTENDED self-tests.

There is NO API to answer a Google Chat call (not OAuth, not Meet REST, not the Meet Media
API — receive-only / dev-preview). The ONLY way to answer is a logged-in client clicking
"Answer". So this drives a SECOND browser (signed in as the callee) over CDP: it opens the
DM, clicks the incoming-call Answer button, holds the call for --answer-seconds, then clicks
Leave. The Leave is what pops the caller's "Rate the meeting … out of 5" survey, which the
caller script (meet_call_browser.py) uses to detect hang-up — so this exercises the whole
ring → answer → talk → hang-up → survey-stop loop with nobody touching a second device.

ONE-TIME SETUP — launch the callee browser yourself (own profile + debug port + FAKE mic so
it transmits a test tone for the caller to capture), sign in as the callee, open the DM, and
leave it running:

  brave-browser --user-data-dir="$PWD/.browser-profile-callee" \
    --remote-debugging-port=9223 \
    --use-fake-ui-for-media-stream --use-fake-device-for-media-stream

(The login is a one-time manual step — Claude can't click OAuth consent. Use a PLAIN Brave
launch like the above so Google's "browser may not be secure" block doesn't bite, the same
recipe that works for the caller's daily Brave.)

Then auto-answer (callee = Duc → fresh profile makes Duc u/0, NOT glo.com):

  python call/auto_answer.py --cdp-url http://127.0.0.1:9223 \
    --url https://chat.google.com/u/0/app/chat/qtotjoAAAAE --answer-seconds 20

Discovery: on first use the Answer/Leave button labels are unknown — run with --dry-run to
DUMP every visible button label (while the call is ringing) so you can pass the right one via
--answer-name / --leave-name.
"""
import argparse
import re
import sys
import time

# Accessible-name patterns for the incoming-call ACCEPT control. DISCOVERED LIVE
# (2026-06-19) on this callee (Vietnamese UI): the ringing dialog's answer button is
# **'Trả lời cuộc gọi'** ("Answer the call") and decline is 'Từ chối'. The dialog even
# instructs "Vui lòng dùng hộp thoại đổ chuông để tham gia hoặc từ chối cuộc gọi" — i.e.
# you MUST use the ringing dialog's answer button, NOT the generic in-DM 'Join'.
#
# ⚠️ Ordered most-specific first. We deliberately DO NOT match the bare 'trả lời' (it also
# means "Reply" → a per-message Reply button on the chat/home view false-answered at +0.1s
# before any ring), nor a bare 'join'/'tham gia' (the in-DM 'Join' affordance is NOT the
# ring-answer and Google tells you not to use it). Pin the exact label with --answer-name
# when in doubt; --dry-run dumps every visible label to (re)discover it.
ANSWER_PATTERNS = [
    r"trả lời cuộc gọi",   # vi: "answer the call" — the ringing dialog's accept button
    r"answer call",
    r"answer the call",
    r"\banswer\b",
    r"\baccept\b",
    r"nghe máy",           # vi: pick up
]
# The LEAVE / hang-up control (inside the connected Meet call IFRAME). DISCOVERED LIVE:
# the real button is **'Rời khỏi cuộc gọi'** ("leave the call"). ⚠️ Do NOT match 'kết thúc'
# / 'end call': while a call is active the DM header's "start video call" button turns
# DISABLED with aria-label "Hãy kết thúc cuộc gọi trước khi bắt đầu cuộc gọi khác"
# ("end the call before starting another") — it lives in the MAIN frame (checked before the
# call iframe) and contains "kết thúc cuộc gọi", so a 'kết thúc' pattern matched that disabled
# decoy and the click hung forever ("element is not enabled"). The real leave button does NOT
# contain 'kết thúc', so we key on 'rời khỏi' instead (and _find_button now skips disabled).
LEAVE_PATTERNS = [
    r"rời khỏi cuộc gọi",   # vi: "leave the call" — the exact in-call hang-up button
    r"rời khỏi",
    r"rời cuộc gọi",
    r"leave call",
    r"\bleave\b",
    r"hang ?up",
]
# UNMUTE the callee mic after answering so the fake-device test tone is actually transmitted
# (Chat huddles can join MUTED → the caller captures −91 dB silence; observed intermittently).
# ⚠️ These match ONLY the "turn ON the mic" control (present when MUTED). We must NEVER match
# 'tắt micrô' / 'turn off microphone' (present when already ON) — clicking that would MUTE.
UNMUTE_PATTERNS = [
    r"bật micrô",            # vi: "turn on microphone"
    r"bật tiếng",            # vi: "unmute"
    r"^unmute",
    r"turn on micro",        # en: "turn on microphone"
]
# Turn the CAMERA on after answering: a fake-device camera streams a continuous test pattern
# = steady inbound video RTP at the caller, INDEPENDENT of the audio mute state. The caller's
# inbound-RTP-flatline hang-up signal needs media to actually flow (bytes growing, then
# flatlining on leave); audio alone is fragile (huddles join muted), so camera-on is the
# reliable media source. ⚠️ Match ONLY the "turn ON" control (present while the camera is OFF);
# never 'tắt máy ảnh' / 'turn off camera' (clicking that would turn it OFF).
CAMERA_ON_PATTERNS = [
    r"bật máy ảnh",          # vi: "turn on camera"
    r"^turn on camera",
    r"turn on the camera",
]


def _frames(page) -> list:
    """All frames (the call UI is a meet.google.com IFRAME; the incoming-call toast is in
    the main frame) — so we must search both. Falls back to the page itself."""
    try:
        return list(page.frames)
    except Exception:  # noqa: BLE001
        return [page]


def _dump_buttons(page) -> list[str]:
    """Every visible button's accessible name across ALL frames — the discovery lifeline
    so the Answer/Leave labels are knowable when a pattern misses."""
    out: list[str] = []
    seen: set[str] = set()
    for fr in _frames(page):
        try:
            handles = fr.get_by_role("button").all()
        except Exception:  # noqa: BLE001
            continue
        for h in handles[:300]:
            try:
                if not h.is_visible():
                    continue
                name = (h.get_attribute("aria-label") or h.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                name = ""
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _find_button(page, patterns):
    """First VISIBLE role=button across ALL frames whose accessible name matches any
    pattern → its Locator, else None."""
    for fr in _frames(page):
        for pat in patterns:
            rx = re.compile(pat, re.I)
            try:
                loc = fr.get_by_role("button", name=rx)
                cnt = loc.count()
            except Exception:  # noqa: BLE001
                continue
            for i in range(min(cnt, 5)):
                cand = loc.nth(i)
                try:
                    # Must be visible AND enabled: a disabled decoy (e.g. the DM header's
                    # "end the call first" button, which contains 'kết thúc cuộc gọi') would
                    # otherwise be returned and clicked forever ("element is not enabled").
                    if cand.is_visible() and cand.is_enabled():
                        return cand, pat
                except Exception:  # noqa: BLE001
                    continue
    return None, None


def _space_id(url: str) -> str:
    """The DM/space id segment of a Chat URL (…/app/chat/<id>), else ''."""
    m = re.search(r"/app/chat/([\w-]+)", url or "")
    return m.group(1) if m else ""


def _pick_chat_page(context, url: str):
    """Reuse an already-open chat.google.com tab if present, but NAVIGATE it to the DM —
    a reused tab parked on /app/home scrapes the home view's per-message 'Reply' buttons
    (which falsely matched the old 'trả lời' answer pattern). The incoming-call ringing
    dialog renders reliably when we're actually IN the DM, so always land on it."""
    want = _space_id(url)
    page = None
    for p in context.pages:
        try:
            if "chat.google.com" in (p.url or ""):
                page = p
                break
        except Exception:  # noqa: BLE001
            continue
    if page is None:
        page = context.new_page()
    # Navigate unless the tab is already showing the target DM.
    if url and (not want or _space_id(page.url) != want):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)
        except Exception:  # noqa: BLE001
            pass
    return page


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-answer the callee side of a Chat call.")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9223",
                        help="CDP endpoint of the callee browser (its --remote-debugging-port).")
    parser.add_argument("--url", default="",
                        help="DM deep link to open as the callee, e.g. "
                             "https://chat.google.com/u/0/app/chat/<spaceId>.")
    parser.add_argument("--answer-seconds", type=float, default=20.0,
                        help="How long to stay in the call after answering, before Leaving "
                             "(simulates the talk window; the Leave triggers the caller's survey).")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="Max seconds to wait for an incoming call before giving up.")
    parser.add_argument("--poll", type=float, default=0.5, help="Poll cadence (seconds).")
    parser.add_argument("--answer-name", default="",
                        help="Exact accessible name of the Answer button (overrides patterns).")
    parser.add_argument("--leave-name", default="",
                        help="Exact accessible name of the Leave button (overrides patterns).")
    parser.add_argument("--no-leave", action="store_true",
                        help="Answer + hold, but DON'T auto-leave (the caller will hang up).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't click — just DUMP visible button labels each poll "
                             "(discovery mode to find the Answer/Leave labels).")
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: Playwright not installed → conda run -n igaming pip install playwright",
              file=sys.stderr)
        return 2

    answer_pats = [re.escape(args.answer_name)] if args.answer_name else ANSWER_PATTERNS
    leave_pats = [re.escape(args.leave_name)] if args.leave_name else LEAVE_PATTERNS

    with sync_playwright() as pw:
        print(f"  connecting to callee browser at {args.cdp_url} (CDP) …")
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp_url)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: could not connect to {args.cdp_url}: {exc}\n"
                  "  Launch the callee browser with --remote-debugging-port=9223 first.",
                  file=sys.stderr)
            return 2
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = _pick_chat_page(context, args.url)
        print(f"  callee page: {page.url}")

        t0 = time.monotonic()
        deadline = t0 + args.timeout
        answered_at = None
        media_logged = False

        if args.dry_run:
            print("  [dry-run] dumping button labels until --timeout (no clicks).")
        else:
            print(f"  waiting for an incoming call (≤{args.timeout:.0f}s) → will answer, "
                  f"hold {args.answer_seconds:.0f}s, then leave.")

        last_dump = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if args.dry_run:
                if now - last_dump >= 3:
                    last_dump = now
                    labels = _dump_buttons(page)
                    print(f"  [dry-run +{now - t0:4.0f}s] {len(labels)} buttons:")
                    for n in labels:
                        print(f"      - {n!r}")
                page.wait_for_timeout(int(args.poll * 1000))
                continue

            if answered_at is None:
                btn, pat = _find_button(page, answer_pats)
                if btn is not None:
                    try:
                        btn.click(timeout=3000)
                        answered_at = time.monotonic()
                        print(f"\n✅ ANSWERED  (+{answered_at - t0:.1f}s; matched {pat!r})")
                        print(f"ANSWERED {answered_at - t0:.1f}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"   answer click failed ({exc}) — retrying", file=sys.stderr)
                elif now - last_dump >= 5:
                    # No Answer button matched yet — dump what's visible so a missed label is
                    # fixable from this one run (pass it via --answer-name) without a 2nd ring.
                    last_dump = now
                    labels = _dump_buttons(page)
                    print(f"   [waiting +{now - t0:4.0f}s] no Answer match; {len(labels)} buttons: "
                          + ", ".join(repr(n) for n in labels))
            else:
                # Make the callee TRANSMIT media so the caller receives RTP — needed both for
                # audio capture AND for the caller's inbound-RTP-flatline hang-up signal (which
                # only engages once media has flowed). Turn the camera ON (steady fake-video
                # RTP, mute-independent) and unmute the mic. The "turn on" controls exist only
                # while OFF, so once on they stop matching → safe to retry each poll. Retried
                # through a 16s window because the in-call controls render a few seconds after
                # answering. mic/cam button state is logged once for debuggability.
                if now - answered_at <= 16:
                    if not media_logged and now - answered_at >= 3:
                        media_logged = True
                        mc = [n for n in _dump_buttons(page)
                              if re.search(r"micr|máy ảnh|camera|microphone|tiếng", n, re.I)]
                        print(f"   [media] mic/cam buttons @+{now - answered_at:.0f}s: {mc}")
                    cbtn, cpat = _find_button(page, CAMERA_ON_PATTERNS)
                    if cbtn is not None:
                        try:
                            cbtn.click(timeout=2000)
                            print(f"📷 turned ON the callee camera (matched {cpat!r}) — steady video RTP")
                        except Exception:  # noqa: BLE001
                            pass
                    ubtn, upat = _find_button(page, UNMUTE_PATTERNS)
                    if ubtn is not None:
                        try:
                            ubtn.click(timeout=2000)
                            print(f"🔊 unmuted the callee mic (matched {upat!r})")
                        except Exception:  # noqa: BLE001
                            pass
                if args.no_leave:
                    if now - answered_at >= args.answer_seconds:
                        print("   held the call; leaving teardown to the caller (--no-leave).")
                        print("HELD")
                        return 0
                elif now - answered_at >= args.answer_seconds:
                    btn, pat = _find_button(page, leave_pats)
                    if btn is not None:
                        try:
                            btn.click(timeout=3000)
                            print(f"\n📴 LEFT the call  (+{now - t0:.1f}s; matched {pat!r}) — "
                                  "this pops the caller's rating survey.")
                            print("LEFT")
                            page.wait_for_timeout(1500)
                            return 0
                        except Exception as exc:  # noqa: BLE001
                            print(f"   leave click failed ({exc}) — retrying", file=sys.stderr)
                    else:
                        # Couldn't find a Leave control — dump once so it's debuggable.
                        labels = _dump_buttons(page)
                        print("   ⚠️  no Leave button matched; visible buttons:", file=sys.stderr)
                        for n in labels:
                            print(f"      - {n!r}", file=sys.stderr)
                        # Don't spin forever on the dump; nudge the window so we retry next poll.
            page.wait_for_timeout(int(args.poll * 1000))

        if answered_at is None:
            print(f"\n⏱  No incoming call within {args.timeout:.0f}s — nothing to answer.",
                  file=sys.stderr)
            print("NO_CALL")
            return 1
        print("\n⏱  Answered but never found a Leave control before timeout.", file=sys.stderr)
        print("ANSWERED_NO_LEAVE")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
