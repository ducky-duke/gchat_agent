#!/usr/bin/env python3
"""ai_call.py — minimal "AI mouth" call: a dedicated, PRE-GRANTED caller browser
that rings the callee and injects audio they hear.

This is the focused entry point for the AI-voice-on-a-call direction. It adds the
ONE thing the general call script can't do on the daily browser — a fresh browser
whose microphone permission is granted UP FRONT (no manual "allow microphone"
click) — and then reuses everything else:

  1. launch (or reuse) a DEDICATED caller Brave: its own profile + debug port,
     launched with --use-fake-ui-for-media-stream so getUserMedia is auto-accepted
     AND bound to the DEFAULT capture device — which is the virtual mic (ai_mic)
     once injection makes ai_mic the default source (before the call is placed);
  2. delegate the proven ring + WebRTC-join + audio-injection engine
     (meet_call_browser.main, over CDP) to that browser, playing a 4-note test tone
     (or a file) into the call so the CALLEE hears it.

Why a dedicated browser (vs. the daily Brave via CDP): the daily Brave wasn't
launched with --use-fake-ui-for-media-stream, so the first injected call needed a
manual "allow mic device" click AND a retry to move the late-appearing capture
stream onto ai_mic. Launching our own browser with the flag + ai_mic preset as the
default source means the call grabs ai_mic from the start, permission-free.

Everything call-specific (find the call button, click Join now, detect the answer,
virtual-mic setup/teardown, hang-up detection) is REUSED — this script is a thin
launcher. Later: swap the static tone for a Gemini Live TTS stream into
ai_mic_sink (see meet_audio_inject) so the AI actually talks on the call.

  conda run --no-capture-output -n igaming python -u call/ai_call.py
  # first run only: sign the dedicated profile in as mikmikb26 (the script tells you how)

⚠️  Automates the Google UI (ToS / account-flag risk) — demo accounts only. Keep
the caller window VISIBLE while the call is live (native Wayland suspends an
occluded renderer → the call drops).
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, _THIS_DIR)            # so `import meet_call_browser` works
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

import meet_call_browser  # noqa: E402  (thin reuse of the proven ring+inject engine)

_BRAVE = "/usr/bin/brave-browser"
# Dedicated caller profile (single account → mikmikb26 = u/0). Gitignored; holds the
# Google login. Shared with selftest_call.sh's caller, so a one-time sign-in here
# serves both.
_DEFAULT_PROFILE = os.path.join(_REPO_ROOT, ".browser-profile-caller")
_DEFAULT_PORT = 9333
# The bot↔Duc DM (GOOGLE_VOICE_SPACE = spaces/qtotjoAAAAE). u/0 = mikmikb26 in a
# single-account dedicated profile.
_DEFAULT_URL = "https://chat.google.com/u/0/app/chat/qtotjoAAAAE"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _cdp_up(port: int, *, timeout: float = 2.0) -> bool:
    """True if a browser is already exposing CDP on this port."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ):
            return True
    except Exception:  # noqa: BLE001
        return False


def _launch_caller_brave(port: int, profile: str) -> "subprocess.Popen | None":
    """Plain-launch a dedicated caller Brave (NOT via Playwright → fewer automation
    tells → the Google login survives better). Headed on the real display/GPU — the
    config the working injected call used. Returns the launcher Popen, or None on
    failure. Note: `brave-browser` forks a separate process tree, so this Popen is
    just the launcher; teardown scans /proc by profile path instead."""
    args = [
        _BRAVE,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        # THE point of a dedicated browser: auto-accept getUserMedia + bind it to the
        # DEFAULT capture device (= ai_mic once injection sets it). No manual allow.
        "--use-fake-ui-for-media-stream",
        # Keep the renderer alive if the window is covered (best-effort; native
        # Wayland still gates frames — keep the window visible to be safe).
        "--disable-features=CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows",
        "--disable-background-timer-throttling",
        "--autoplay-policy=no-user-gesture-required",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    _log(f"  launching dedicated caller Brave (port {port}, profile "
         f"{os.path.basename(profile)}, mic auto-granted) …")
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001
        _log(f"  ⚠️  could not launch Brave: {exc}")
        return None
    for _ in range(60):  # up to ~30s for CDP to come up
        if _cdp_up(port):
            _log(f"  caller Brave ready on http://127.0.0.1:{port}")
            return proc
        time.sleep(0.5)
    _log(f"  ⚠️  Brave never exposed CDP on :{port}")
    return proc  # return it anyway so teardown can stop it


def _ensure_logged_in(port: int, url: str, *, wait_s: float) -> bool:
    """Open the DM in the dedicated browser and confirm it's signed in. If it lands
    on the Google sign-in page, poll (the headed window lets the user sign in) until
    signed in or wait_s elapses. Reuses Playwright over CDP, then releases the
    connection WITHOUT closing the browser (the call engine reconnects)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        _log(f"  ⚠️  playwright not importable ({exc}); skipping login check")
        return True
    cdp = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp, timeout=30_000)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(3000)
            deadline = time.time() + wait_s
            warned = False
            while True:
                cur = (page.url or "").lower()
                if "accounts.google.com" not in cur and "signin" not in cur:
                    _log("  caller signed in ✓")
                    return True
                if not warned:
                    _log("  ⚠️  caller profile is NOT signed in. In the Brave window "
                         "that just opened, sign in as mikmikb26@gmail.com ONLY "
                         "(no other account), then I'll continue automatically …")
                    warned = True
                if time.time() > deadline:
                    _log("  ⚠️  still not signed in after waiting — sign in, then re-run.")
                    return False
                page.wait_for_timeout(2000)
    except Exception as exc:  # noqa: BLE001
        _log(f"  ⚠️  login check failed ({type(exc).__name__}: {exc}); proceeding anyway")
        return True


def _kill_profile_braves(profile: str) -> None:
    """SIGTERM (then SIGKILL) every brave process whose cmdline holds this profile
    path. /proc scan, NOT `pkill -f` — pkill -f <profile> would also match THIS
    python process (the path is in our argv) and could kill us."""
    me = os.getpid()
    targets: "list[int]" = []
    for pid_s in os.listdir("/proc"):
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        if "brave" in cmd.lower() and profile in cmd:
            targets.append(pid)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(1.0)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if targets:
        _log(f"  caller Brave stopped ({len(targets)} proc).")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ai_call",
        description="Place a ringing call from a dedicated, mic-pre-granted browser "
        "and inject audio the callee hears (the 'AI mouth' path).",
    )
    ap.add_argument("--audio", default=None, metavar="FILE",
                    help="audio file to inject (ffmpeg-decodable). Default: a "
                         "generated 4-note test tone.")
    ap.add_argument("--duration", type=float, default=90.0,
                    help="MAX seconds to hold the call (exits early on hang-up; default 90).")
    ap.add_argument("--at-join", action="store_true",
                    help="start playback only when the callee ANSWERS (default: play "
                         "from ring — proven robust; pre-answer audio never reaches them).")
    ap.add_argument("--once", action="store_true",
                    help="play the audio ONCE instead of looping until hang-up.")
    ap.add_argument("--url", default=_DEFAULT_URL,
                    help="exact Chat DM URL to call into (default: the bot↔Duc DM, u/0).")
    ap.add_argument("--port", type=int, default=_DEFAULT_PORT,
                    help=f"CDP/debug port for the dedicated caller Brave (default {_DEFAULT_PORT}).")
    ap.add_argument("--profile", default=_DEFAULT_PROFILE,
                    help="dedicated caller profile dir (holds the Google login).")
    ap.add_argument("--login-wait", type=float, default=180.0,
                    help="seconds to wait for a manual sign-in if the profile isn't "
                         "signed in (default 180).")
    ap.add_argument("--quit-browser", action="store_true",
                    help="stop the dedicated caller Brave on exit. Default: leave it "
                         "running so the login persists and the next call is instant.")
    a = ap.parse_args(argv)

    profile = os.path.abspath(a.profile)
    if not os.path.isdir(profile):
        _log(f"ERROR: caller profile not found: {profile}")
        _log("  ONE-TIME setup — sign it in as mikmikb26 (plain browser, NO automation):")
        _log(f'    brave-browser --user-data-dir="{profile}"')
        _log("  → sign in as mikmikb26@gmail.com ONLY (no glo.com), open the Duc DM "
             "once, close the window, then re-run this script.")
        return 2

    _log("=== ai_call → pre-granted caller, inject audio the callee hears ===")
    launched = None
    if _cdp_up(a.port):
        _log(f"  reusing caller Brave already on http://127.0.0.1:{a.port}")
    else:
        launched = _launch_caller_brave(a.port, profile)
        if not _cdp_up(a.port):
            _log("ERROR: caller Brave is not reachable over CDP — aborting.")
            if launched and a.quit_browser:
                _kill_profile_braves(profile)
            return 2

    if not _ensure_logged_in(a.port, a.url, wait_s=a.login_wait):
        _log("  (leaving the browser open so you can finish signing in.)")
        return 2

    # Delegate the ring + WebRTC-join + audio-injection to the proven engine.
    mcb_argv = [
        "--cdp-url", f"http://127.0.0.1:{a.port}",
        "--url", a.url,
        "--watch-join",
        "--duration", str(a.duration),
        "--inject-audio",
    ]
    if a.audio:
        mcb_argv.append(a.audio)        # bare --inject-audio (no value) = test tone
    if a.at_join:
        mcb_argv.append("--inject-at-join")
    if a.once:
        mcb_argv.append("--inject-once")

    _log("  handing off to meet_call_browser (ring + inject) …\n")
    try:
        rc = meet_call_browser.main(mcb_argv)
    finally:
        if launched and a.quit_browser:
            _kill_profile_braves(profile)
        elif launched:
            _log(f"\n  caller Brave left running on :{a.port} "
                 "(login persists; --quit-browser to stop it).")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
