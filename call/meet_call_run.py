"""The call lifecycle for meet_call_browser: argument parsing + main(), which places
the native ringing call and holds it until hang-up, plus the background Meet REST
watch. Moved verbatim from the original monolith (behavior-preserving)."""
from __future__ import annotations

import argparse
import os
import sys
import time

from meet_call_js import _WEBRTC_HOOK
from meet_call_signals import (
    _IN_CALL_PATTERNS,
    _REPO_ROOT,
    _alone_signal,
    _call_frame_present,
    _calling_indicator_present,
    _dump_buttons,
    _dump_structure,
    _ensure_mic_on,
    _extract_meeting_code,
    _find_button_in_frames,
    _find_call_button,
    _frames,
    _in_call,
    _page_text_snippet,
    _participant_join_state,
    _post_call_feedback_present,
    _resolve_path,
    _scan_page_for_code,
    _webrtc_ice_stats,
    _webrtc_inbound_bytes,
    _webrtc_outbound_bytes,
    _webrtc_live_audio,
    _webrtc_pc_dead,
    _webrtc_track_count,
    _webrtc_unmute_seen,
)
from meet_call_setup import (
    DEFAULT_BROWSER,
    DEFAULT_PROFILE_DIRS,
    _BROWSER_PATHS,
    _default_url,
    _derive_proc_match,
    _import_cookies,
    _keepalive_renderer,
    _open_context,
)


def _start_rest_watch(cfg, args, meeting_code: "str | None"):
    """Spin up the Meet REST room-data watch in a DAEMON thread. It is urllib-only
    (reuses call/meet_rest_watch), so it never touches Playwright objects from the
    main thread — only the OAuth token + the Meet REST API. Returns (stop_event,
    thread); (None, None) if the watcher can't be started."""
    import threading
    try:
        import meet_rest_watch as mrw  # sibling script (call/ is on sys.path)
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


def main(argv: list[str] | None = None, *, on_join=None, on_pickup=None,
         stop_event=None) -> int:
    """on_join / on_pickup: optional zero-arg callbacks for an external audio engine
    (call/gemini_call.py). Both fire AT MOST ONCE; a callback exception is swallowed,
    never crashes the call.

    stop_event: an optional threading.Event. When set, the hold loop ends the call on
    its next poll (the external engine asked to hang up — e.g. gemini_call's end_call
    tool, when the AI judged the callee was done). It's the only externally-driven way
    to end the call short of a real hang-up / the duration cap.

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
        "--media-flatline-secs",
        type=float,
        default=3.0,
        help="seconds of MUTUAL media silence (NEITHER side sending RTP) before treating "
        "the inbound-flatline as a hang-up (default 3). One-sided silence — you listening "
        "while the bot/AI talks (outbound still flowing) — never counts, so a conversation "
        "isn't cut mid-answer. Raise it for an interactive AI call where natural "
        "think-pauses are longer (e.g. gemini_call passes 30); the roster-collapse signal "
        "still catches a clean hang-up fast when the window is visible.",
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
        "pactl+ffmpeg (PipeWire/PulseAudio). See call/meet_audio_capture.py.",
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
        "the chain offline first: python call/meet_audio_inject.py --verify.",
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
        "(e.g. call/gemini_call.py) owns the mic audio via a virtual default-source, "
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
        # But it must be MUTUAL: a poll only counts as flat when NEITHER side is sending media.
        # While WE talk (outbound growing) and the callee just listens (inbound flat) the call
        # is alive — counting that as flat dropped the call mid-answer on an AI call (the bug).
        prev_bytes = -1        # last inbound-RTP byte total seen
        prev_out_bytes = -1    # last outbound-RTP byte total seen (media we send)
        bytes_flat_for = 0     # consecutive polls of MUTUAL silence (post-flow)
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
                # External hang-up: an audio engine (gemini_call's end_call tool) asked to
                # end the call. Checked first so it ends within one poll, then falls through
                # to the normal teardown (leave-button click, audio finalize, tab cleanup).
                if stop_event is not None and stop_event.is_set():
                    ended_reason = "ended by AI assistant (callee asked to wrap up)"
                    break
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
                            # media itself stops). Independent of any DOM/rendering. BUT we
                            # require MUTUAL silence: if WE are still sending media (outbound
                            # growing — e.g. the AI is mid-sentence and the callee is just
                            # listening), inbound silence is NOT a hang-up. Only when neither
                            # side has sent media for --media-flatline-secs do we end. This
                            # stops a one-sided AI monologue from dropping the call mid-answer.
                            if not end_now:
                                b = _webrtc_inbound_bytes(call_page)
                                ob = _webrtc_outbound_bytes(call_page)
                                in_grew = prev_bytes >= 0 and b > prev_bytes + 500
                                out_grew = prev_out_bytes >= 0 and ob > prev_out_bytes + 500
                                if b >= 0:
                                    if in_grew:
                                        bytes_ever_grew = True   # remote media is/was flowing
                                        media_connected = True
                                    # poll silence resets the moment EITHER side sends media
                                    if in_grew or out_grew:
                                        bytes_flat_for = 0
                                    elif prev_bytes >= 0 and bytes_ever_grew:
                                        bytes_flat_for += 1      # mutual silence (both flat)
                                        flat_needed = max(
                                            2, int(args.media_flatline_secs / (wait_ms / 1000.0)))
                                        # Diagnostic-only heartbeat: prints on every entry
                                        # into a mutual-silence window. During an interactive
                                        # AI call there are MANY such windows (every natural
                                        # think-pause), and each one shreds the live transcript
                                        # (which streams with end=""). Gate it behind
                                        # --diag-pickup so a normal run keeps a clean transcript;
                                        # the hang-up DECISION below is unaffected by this gate.
                                        if bytes_flat_for == 1 and _diag:
                                            print(f"   [end] no media either way (in={b} out={ob}) — "
                                                  f"watching for hang-up ({args.media_flatline_secs:g}s) …")
                                        if bytes_flat_for >= flat_needed:
                                            ended_reason = "media silent both ways (hung up)"
                                            end_now = True
                                    prev_bytes = b
                                    if ob >= 0:
                                        prev_out_bytes = ob
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
