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
import os
import re
import sys
import time

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
        browser = pw.chromium.connect_over_cdp(args.cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        return browser, context
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


def main(argv: list[str] | None = None) -> int:
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

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: navigation failed: {exc}", file=sys.stderr)
            return 1

        # --- wait for the call button to appear ----------------------------------
        deadline = time.monotonic() + args.load_timeout
        button = None
        while time.monotonic() < deadline:
            button = _find_call_button(page, args.button_name)
            if button is not None:
                break
            page.wait_for_timeout(1000)

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

        # --- click it → this RINGS the callee ------------------------------------
        # The click usually navigates to a meet.google.com/call page (same tab or a
        # popup). Capture either so we can watch / leave the call.
        call_page = page
        try:
            with context.expect_page(timeout=8_000) as popup:
                button.click()
            call_page = popup.value
        except Exception:  # noqa: BLE001 - no popup => it navigated in-place
            try:
                button.click()
            except Exception:
                pass  # already clicked inside the expect_page block
            for p in context.pages:
                if "meet.google.com" in (p.url or ""):
                    call_page = p
                    break

        try:
            call_page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass

        # A DM video call usually opens a Meet pre-join ("green room"); the callee is
        # only RUNG once the caller commits by clicking "Join now". Click it if shown
        # (search ALL frames — the green room may live in a meet.google.com iframe).
        joined = False
        deadline = time.monotonic() + 20
        join_pats = (r"join now", r"^join$", r"join call", r"ask to join")
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
            print("   (no 'Join now' button seen — the call may already be ringing)")
        print(f"\n📞 Call placed — ringing Duc. Call page: {call_page.url}")

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
            call_page.wait_for_timeout(1000)
        if in_call:
            print("   ✅ In the call (ringing/connected). Watching for it to end …")
        else:
            print(
                "   ⚠️  couldn't confirm the in-call controls — can't auto-detect a "
                "hang-up; will hold to the cap. (Re-run with --dry-run to dump buttons.)"
            )

        # --- stay in the call, but EXIT THE MOMENT IT ENDS -----------------------
        # --duration is a MAX cap. PRIMARY end signal: the leave/hang-up control
        # DISAPPEARS after we'd seen it — the call dropped (in a 1:1 call, either side
        # hanging up ends it for both). SECONDARY: we linger alone. We deliberately do
        # NOT match 'Call ended' TEXT: a DM renders call-HISTORY cards with that exact
        # text in the same frame, which false-positives instantly (the bug we hit).
        ended_reason = None
        gone_for = 0   # consecutive seconds the leave control has been absent
        alone_for = 0  # consecutive seconds we've been alone in the call
        cap = None if args.keep_open else time.monotonic() + args.duration
        try:
            while cap is None or time.monotonic() < cap:
                if in_call:
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
                call_page.wait_for_timeout(1000)
        except KeyboardInterrupt:
            print("\n   Ctrl+C — leaving the call.")

        if ended_reason:
            print(f"\n📴 Call ended — {ended_reason}. Stopping.")
        elif not args.keep_open:
            print(f"\n⏱  Reached the {args.duration:.0f}s cap with no hang-up detected — leaving.")

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
