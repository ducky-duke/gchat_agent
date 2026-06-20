"""Browser plumbing for meet_call_browser: resolve/launch the automation browser
(isolated persistent profile or CDP attach), one-time cookie import, the renderer
keepalive, and Chat-space URL / process-match helpers."""
from __future__ import annotations

import os
import re
import sys
import time

from meet_call_signals import _REPO_ROOT, _resolve_path


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
