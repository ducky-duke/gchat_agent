#!/usr/bin/env bash
# UNATTENDED end-to-end self-test of the native Chat DM call loop — nobody touches a second
# device. Drives TWO browsers over CDP on this machine:
#   • CALLER  = daily Brave on :9222, signed in as mikmikb26  (meet_call_browser.py rings + captures)
#   • CALLEE  = 2nd Brave  on :9223, signed in as Duc          (auto_answer.py answers + leaves)
# The callee's Leave pops the caller's "Rate the meeting" survey, which the caller uses to
# self-stop — so this verifies ring → answer → talk(fake tone) → hang-up → survey-stop + audio
# capture, all hands-off.
#
# ONE-TIME callee setup (do once, then leave it running) — PLAIN Brave so Google's
# "browser may not be secure" block doesn't bite, FAKE mic so it transmits a test tone:
#   brave-browser --user-data-dir="$PWD/.browser-profile-callee" \
#     --remote-debugging-port=9223 \
#     --use-fake-ui-for-media-stream --use-fake-device-for-media-stream
#   → sign in as Duc (trantrongducqt@gmail.com), open the DM, leave it running.
#
# Usage:  scripts/selftest_call.sh [--answer-seconds 20] [--duration 90] [--dry-run-answer]
set -u

CALLER_CDP="http://127.0.0.1:9222"
CALLEE_CDP="http://127.0.0.1:9223"
CALLER_URL="https://chat.google.com/u/0/app/chat/qtotjoAAAAE"   # mikmikb26 = u/0 (daily Brave)
# NOTE (2026-06-19): glo.com was REMOVED from the daily Brave entirely, shifting account
# indices DOWN — mikmikb26 moved u/1 → u/0; u/1 is now trantrongducqt (Duc, = the callee).
# Verified live: contacts.google.com/u/0 → mikmikb26, /u/1 → Duc, /u/2+ → redirect to u/0.
# Using the OLD u/1 made the caller ring AS Duc (same identity as the callee) → no ring ever
# reached the callee browser ("no Answer match" for the whole run). Re-probe with
# scratchpad/probe_accounts.py if the ring stops landing — these indices can drift again.
CALLEE_URL="https://chat.google.com/u/0/app/chat/qtotjoAAAAE"   # Duc = u/0 (fresh callee profile)
ANSWER_SECONDS=90   # callee HOLD before leaving. Must comfortably exceed the caller's connect lag
                    # (the Xvfb caller takes ~30s to get into the call) so the two OVERLAP long
                    # enough to capture audio + then exercise hang-up. 20s lost the race (caller
                    # arrived after the callee already left → recvLive=0, nothing captured).
DURATION=200   # cap only — the caller self-stops on hang-up well before this; big cap = headroom
               # for the Xvfb caller's slow connect + the longer 90s hold.
DRY_ANSWER=0
PREFLIGHT=0   # --preflight: bring up the Xvfb caller, LOCATE the call button (dry-run, NO ring,
              # NO callee), tear down, exit. Confirms the caller profile is signed in + ready.
NO_CALLEE=0   # --no-callee: a HUMAN answers on ANOTHER device (no auto-answer callee, no Xvfb
              # callee). The caller rings + captures; you pick up on your phone/other machine and
              # SPEAK; the caller self-stops on your hang-up. The audio verdict still runs — this
              # is the real test of "capture the remote human's voice from the call". The caller's
              # own "REMOTE JOINED" line is the pick-up proof (there's no callee log to read).
DIAG=0   # --diag: pass --diag-structure to the caller (live DOM/capture dumps every ~3s)
# profile = capture ONLY the dedicated caller browser's OWN decoded audio (scoped by its
#   --user-data-dir process tree) → call-only, and the robust path for Google Meet (whose
#   WebRTC track the in-browser 'webrtc' tap can't reach: recvLive stays 0). The caller
#   profile plays nothing but the call, so its audio output IS the call.
# webrtc = tap the inbound WebRTC track in-browser — BLIND on Meet (kept for diagnostics).
# monitor = the OS desktop mix (coarse). isolate = move ANY browser stream (not call-scoped).
AUDIO_MODE=profile

# --- Xvfb caller (DEFAULT) -----------------------------------------------------------------
# Run the caller Brave on a VIRTUAL display so its renderer can never be occluded → never
# suspended by the GNOME-Wayland compositor (the proven root cause of every failed run: an
# occluded caller's PeerConnections close, tracks end, capture goes silent, join lags to +33s).
# Under Xvfb there is no compositor and nothing to cover the window, so the renderer is always
# 'visible' and stays awake — FULLY UNATTENDED (nobody keeps a window in front). One-time prereq:
# a dedicated caller profile signed in as mikmikb26 (the script prints the command if it's
# missing). --caller-real reverts to the old real-display :9222 path (needs a babysat window).
CALLER_XVFB=1
# --caller-headed: run the DEDICATED clean profile (single account mikmikb26, u/0) HEADED on the
# REAL GPU display — a real Brave window (honors "caller = Brave THẬT") but with NO other tabs to
# confuse call-frame discovery and NO account-index drift. This is the reliable real-Brave path:
# the daily Brave on :9222 accumulates stray tabs across runs (a run once attached to a blank
# chrome://newtab and never found the call) and its account indices shift when accounts are
# added/removed. Renderer-occlusion caveat still applies (keep the window visible).
CALLER_HEADED=0
# --caller-xwayland: the OCCLUSION-IMMUNE real-GPU path. Run the dedicated clean profile HEADED on
# the real display but FORCED onto X11/XWayland (--ozone-platform=x11) WITHOUT the swiftshader
# override, so it still uses the real GPU. Why this should beat both other modes for STABLE media:
#   • native-Wayland headed (--caller-headed): media connects but DROPS when the window is covered —
#     Mutter stops sending wl_surface.frame to the occluded surface, a layer BELOW Chromium, so the
#     --disable-features=CalculateNativeWinOcclusion flag is a no-op and the renderer suspends.
#   • Xvfb (--caller-xvfb default): occlusion-proof BUT software GL (swiftshader) appears to break
#     Meet's WebRTC media/ICE path — media NEVER connects (ICE gathers, no candidate pairs).
#   • XWayland (THIS): Chromium runs as an X11 client, so occlusion is decided by Chromium's OWN
#     CalculateNativeWinOcclusion — which the disable-flag DOES suppress → renderer stays awake even
#     when covered → no drop. And XWayland exposes the real GPU → media connects (no swiftshader).
# HYPOTHESIS (NOT yet live-verified) targeting the connects-then-drops failure that killed the live
# human-callee run. Same one-time prereq + clean-profile benefits as --caller-headed.
CALLER_XWAYLAND=0
CALLER_DISPLAY=":99"
CALLER_PROFILE="$PWD/.browser-profile-caller"
CALLER_PORT=9322
# In a FRESH single-account caller profile mikmikb26 is the ONLY account ⇒ u/0 (NOT u/1 — u/1 is a
# daily-profile artifact where u/0=glo.com is revoked). glo.com is NEVER added to this profile.
CALLER_URL_XVFB="https://chat.google.com/u/0/app/chat/qtotjoAAAAE"
CALLER_URL_SET=0
XVFB_PID=""
CALLER_BRAVE_PID=""

# Callee on its OWN virtual display too (DEFAULT) — so Duc's renderer is never occluded → it
# answers fast + transmits its fake-mic reliably (an occluded callee on the real screen can be
# throttled → slow answer / no RTP → caller captures silence). Zero sign-in: the callee profile
# is already signed in as Duc. The script stops any callee Brave already holding the profile and
# relaunches it under Xvfb. --callee-real reverts to expecting a pre-running callee on :9223.
CALLEE_XVFB=1
CALLEE_DISPLAY=":100"
CALLEE_PROFILE="$PWD/.browser-profile-callee"
XVFB_CALLEE_PID=""
CALLEE_BRAVE_PID=""

while [ $# -gt 0 ]; do
  case "$1" in
    --answer-seconds) ANSWER_SECONDS="$2"; shift 2;;
    --duration) DURATION="$2"; shift 2;;
    --callee-url) CALLEE_URL="$2"; shift 2;;
    --caller-url) CALLER_URL="$2"; CALLER_URL_SET=1; shift 2;;
    --caller-real) CALLER_XVFB=0; shift;;          # use the old real-display :9222 caller (babysat)
    --caller-headed) CALLER_HEADED=1; CALLER_XVFB=0; shift;;  # dedicated clean profile, HEADED on real GPU display
    --caller-xwayland) CALLER_XWAYLAND=1; CALLER_HEADED=0; CALLER_XVFB=0; shift;;  # headed real-GPU on XWayland (occlusion-immune)
    --callee-real) CALLEE_XVFB=0; shift;;          # expect a pre-running callee on :9223 (not Xvfb)
    --caller-profile) CALLER_PROFILE="$2"; shift 2;;
    --preflight) PREFLIGHT=1; shift;;              # no-ring: just verify the caller profile is ready
    --no-callee) NO_CALLEE=1; CALLEE_XVFB=0; shift;;  # human answers on another device (no auto-callee)
    --audio-mode) AUDIO_MODE="$2"; shift 2;;  # profile (default, call-only) | webrtc | monitor | isolate
    --diag) DIAG=1; shift;;                    # caller dumps live DOM + capture state every ~3s
    --dry-run-answer) DRY_ANSWER=1; shift;;   # discovery: dump callee button labels, don't click
    *) echo "unknown arg: $1"; exit 2;;
  esac
done
DIAG_FLAG=""; [ "$DIAG" = "1" ] && DIAG_FLAG="--diag-structure"
# Keep foreground ON for BOTH paths. Under Xvfb the WINDOW is always visible, but the keepalive
# also bring_to_front()s the call tab + focus-emulates + setWebLifecycleState(active) — that's what
# makes the call tab/iframe RENDER (a non-active tab is visibilityState='hidden' → DOM throttled →
# tiles stay 0 → the roster-collapse hang-up signal can't fire). The renderer-awake part is just
# redundant-but-harmless on Xvfb; the tab-focus part is still essential. So never --no-foreground here.
FG_FLAG=""

TS=$(date +%Y%m%d_%H%M%S)
OUT="reports/selftest_${TS}"
mkdir -p "$OUT"
CALLER_LOG="$OUT/caller.log"
CALLEE_LOG="$OUT/callee.log"
AUDIO_OUT="$OUT/call.wav"

cdp_account() {  # best-effort: print the signed-in Google account label from a CDP browser
  curl -s --max-time 3 "$1/json" >/dev/null 2>&1 && echo "reachable" || echo "UNREACHABLE"
}

teardown_caller() {  # tear down a caller Brave WE launched (Xvfb OR headed dedicated profile).
  # Guards on CALLER_BRAVE_PID being set, so --caller-real (daily :9222, we launched nothing)
  # is a no-op and the user's daily Brave is never touched.
  [ -n "$CALLER_BRAVE_PID" ] || return 0
  kill -INT "$CALLER_BRAVE_PID" 2>/dev/null; sleep 1
  # `brave-browser` is a launcher that FORKS a separate /opt/brave.com/brave process tree, so
  # killing the launcher PID ($!) orphans the real browser (it kept holding the profile lock and
  # blocked the next run). Sweep every process still on OUR dedicated profile by its path. Safe:
  # only brave processes carry --user-data-dir=<profile> in their cmdline; the selftest's own
  # bash ("bash scripts/selftest_call.sh …") and the meet_call python do NOT, and pkill skips
  # its own PID — so this never self-matches. NOT run for --caller-real (returned above).
  pkill -f "$CALLER_PROFILE" 2>/dev/null; sleep 1; pkill -9 -f "$CALLER_PROFILE" 2>/dev/null
  [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null
}

teardown_callee() {  # tear down the Xvfb callee (virtual display + its Brave) if we started one
  [ "$CALLEE_XVFB" = "1" ] || return 0   # --callee-real (pre-running callee) → never touch it
  [ -n "$CALLEE_BRAVE_PID" ] && { kill -INT "$CALLEE_BRAVE_PID" 2>/dev/null; sleep 1; kill "$CALLEE_BRAVE_PID" 2>/dev/null; }
  # Same wrapper-orphan bug as the caller: kill the launcher PID and the real brave tree survives,
  # holding the profile + an undead Xvfb. Sweep the profile (safe — only brave carries the path).
  # Guarded by CALLEE_XVFB=1: a pre-existing real-display callee was already stopped at start-up to
  # free the profile, so this only reaps OUR Xvfb callee, never a --callee-real one.
  pkill -f "$CALLEE_PROFILE" 2>/dev/null; sleep 1; pkill -9 -f "$CALLEE_PROFILE" 2>/dev/null
  [ -n "$XVFB_CALLEE_PID" ] && kill "$XVFB_CALLEE_PID" 2>/dev/null
}

echo "=== self-test call → $OUT ==="

# --- bring up the caller -------------------------------------------------------------------
if [ "$CALLER_XWAYLAND" = "1" ]; then
  # Dedicated clean profile, HEADED on the real display but forced onto X11/XWayland with the
  # occlusion calc DISABLED — and NO swiftshader override, so it keeps the real GPU. Goal: media
  # connects (real GPU, unlike Xvfb) AND survives the window being covered (Chromium's own occlusion
  # calc is suppressed on X11, unlike native Wayland where Mutter gates frames below the flag).
  CALLER_CDP="http://127.0.0.1:${CALLER_PORT}"
  [ "$CALLER_URL_SET" = "0" ] && CALLER_URL="$CALLER_URL_XVFB"   # dedicated profile: mikmikb26 = u/0
  if [ ! -d "$CALLER_PROFILE" ]; then
    echo "ERROR: caller profile not found: $CALLER_PROFILE — sign it in as mikmikb26 once:"
    echo "    brave-browser --user-data-dir=\"$CALLER_PROFILE\"   (mikmikb26 ONLY, open the DM, close)"
    exit 2
  fi
  if pgrep -f "$CALLER_PROFILE" >/dev/null 2>&1; then
    echo "ERROR: another Brave already holds $CALLER_PROFILE — close it first. Holder(s):"
    pgrep -af "$CALLER_PROFILE" | grep -v pgrep | sed 's/^/    /'
    exit 2
  fi
  echo ">> launching caller Brave HEADED on XWayland (x11 ozone, occlusion-disabled, real GPU) …"
  env -u WAYLAND_DISPLAY brave-browser \
    --user-data-dir="$CALLER_PROFILE" \
    --remote-debugging-port="$CALLER_PORT" \
    --ozone-platform=x11 \
    --disable-features=CalculateNativeWinOcclusion \
    --disable-backgrounding-occluded-windows --disable-background-timer-throttling \
    --autoplay-policy=no-user-gesture-required \
    --no-first-run --no-default-browser-check about:blank >/dev/null 2>&1 &
  CALLER_BRAVE_PID=$!
  up=0
  for _ in $(seq 1 60); do
    curl -s --max-time 2 "$CALLER_CDP/json/version" >/dev/null 2>&1 && { up=1; break; }
    sleep 0.5
  done
  if [ "$up" != "1" ]; then
    echo "ERROR: caller Brave never came up on $CALLER_CDP (xwayland)."
    teardown_caller; exit 2
  fi
  echo "  caller ready: HEADED XWayland · port $CALLER_PORT · profile $(basename "$CALLER_PROFILE") · url $CALLER_URL"
elif [ "$CALLER_HEADED" = "1" ]; then
  # Dedicated clean profile, HEADED on the real GPU display (real Brave, no Xvfb, no x11/swiftshader
  # override → uses the system Wayland/GPU exactly like the daily Brave). Clean profile ⇒ no stray
  # tabs to misdirect call-frame discovery, and single-account ⇒ u/0 is always mikmikb26.
  CALLER_CDP="http://127.0.0.1:${CALLER_PORT}"
  [ "$CALLER_URL_SET" = "0" ] && CALLER_URL="$CALLER_URL_XVFB"   # dedicated profile: mikmikb26 = u/0
  if [ ! -d "$CALLER_PROFILE" ]; then
    echo "ERROR: caller profile not found: $CALLER_PROFILE"
    echo "  ONE-TIME setup — sign it in as mikmikb26 (plain browser, NO CDP):"
    echo "    brave-browser --user-data-dir=\"$CALLER_PROFILE\""
    echo "  → sign in as mikmikb26@gmail.com ONLY, open the DM once, close the window. Then re-run."
    exit 2
  fi
  if pgrep -f "$CALLER_PROFILE" >/dev/null 2>&1; then
    echo "ERROR: another Brave already holds $CALLER_PROFILE — close it first. Holder(s):"
    pgrep -af "$CALLER_PROFILE" | grep -v pgrep | sed 's/^/    /'
    exit 2
  fi
  echo ">> launching caller Brave HEADED on the real display (dedicated mikmikb26 profile, GPU) …"
  brave-browser \
    --user-data-dir="$CALLER_PROFILE" \
    --remote-debugging-port="$CALLER_PORT" \
    --autoplay-policy=no-user-gesture-required \
    --no-first-run --no-default-browser-check about:blank >/dev/null 2>&1 &
  CALLER_BRAVE_PID=$!
  up=0
  for _ in $(seq 1 60); do
    curl -s --max-time 2 "$CALLER_CDP/json/version" >/dev/null 2>&1 && { up=1; break; }
    sleep 0.5
  done
  if [ "$up" != "1" ]; then
    echo "ERROR: caller Brave never came up on $CALLER_CDP (headed)."
    teardown_caller; exit 2
  fi
  echo "  caller ready: HEADED real display · port $CALLER_PORT · profile $(basename "$CALLER_PROFILE") · url $CALLER_URL"
elif [ "$CALLER_XVFB" = "1" ]; then
  CALLER_CDP="http://127.0.0.1:${CALLER_PORT}"
  [ "$CALLER_URL_SET" = "0" ] && CALLER_URL="$CALLER_URL_XVFB"
  if [ ! -d "$CALLER_PROFILE" ]; then
    echo "ERROR: caller profile not found: $CALLER_PROFILE"
    echo "  ONE-TIME setup — sign a DEDICATED caller profile in as mikmikb26 (plain browser, NO CDP,"
    echo "  on your real screen so you can click the OAuth consent):"
    echo "    brave-browser --user-data-dir=\"$CALLER_PROFILE\""
    echo "  → sign in as mikmikb26@gmail.com (ONLY that account — do NOT add glo.com), open the DM"
    echo "    once, then close the window. Re-run this script afterward (it'll run it under Xvfb)."
    exit 2
  fi
  command -v Xvfb >/dev/null 2>&1 || { echo "ERROR: Xvfb not installed (apt install xvfb)."; exit 2; }
  if pgrep -f "$CALLER_PROFILE" >/dev/null 2>&1; then
    echo "ERROR: another Brave already holds $CALLER_PROFILE — close it first"
    echo "  (e.g. the one-time sign-in window). A locked profile makes the new launch hand off"
    echo "  to the existing instance instead of opening its own CDP. Current holder(s):"
    pgrep -af "$CALLER_PROFILE" | grep -v pgrep | sed 's/^/    /'
    exit 2
  fi
  echo ">> starting virtual display $CALLER_DISPLAY + caller Brave (occlusion-proof, unattended) …"
  Xvfb "$CALLER_DISPLAY" -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &
  XVFB_PID=$!
  sleep 2
  env -u WAYLAND_DISPLAY DISPLAY="$CALLER_DISPLAY" brave-browser \
    --user-data-dir="$CALLER_PROFILE" \
    --remote-debugging-port="$CALLER_PORT" \
    --ozone-platform=x11 --use-gl=angle --use-angle=swiftshader \
    --disable-features=CalculateNativeWinOcclusion \
    --autoplay-policy=no-user-gesture-required \
    --no-first-run --no-default-browser-check about:blank >/dev/null 2>&1 &
  CALLER_BRAVE_PID=$!
  up=0
  for _ in $(seq 1 60); do
    curl -s --max-time 2 "$CALLER_CDP/json/version" >/dev/null 2>&1 && { up=1; break; }
    sleep 0.5
  done
  if [ "$up" != "1" ]; then
    echo "ERROR: caller Brave never came up on $CALLER_CDP under $CALLER_DISPLAY."
    kill "$CALLER_BRAVE_PID" "$XVFB_PID" 2>/dev/null
    exit 2
  fi
  echo "  caller ready: virtual display $CALLER_DISPLAY · port $CALLER_PORT · profile $(basename "$CALLER_PROFILE") · url $CALLER_URL"
else
  echo "  caller (real display :9222) = $(cdp_account "$CALLER_CDP")"
  if ! curl -s --max-time 3 "$CALLER_CDP/json/version" >/dev/null 2>&1; then
    echo "ERROR: caller browser not reachable on $CALLER_CDP."
    echo "  Launch the daily Brave with --remote-debugging-port=9222 (signed in as mikmikb26),"
    echo "  or drop --caller-real to run the caller under Xvfb instead (unattended, recommended)."
    exit 2
  fi
fi

# --- preflight: verify the caller profile (NO ring, NO callee), then exit -------------------
if [ "$PREFLIGHT" = "1" ]; then
  echo ">> PREFLIGHT (no ring): locating the call button on $CALLER_URL …"
  conda run --no-capture-output -n igaming python -u scripts/meet_call_browser.py \
    --cdp-url "$CALLER_CDP" --url "$CALLER_URL" --dry-run 2>&1 | tee "$OUT/preflight.log"
  PF=${PIPESTATUS[0]}
  teardown_caller
  echo ""
  if grep -q "Found the call button" "$OUT/preflight.log"; then
    echo "✅ PREFLIGHT: caller profile signed in + DM loaded + call button located — ready to ring."
  else
    echo "❌ PREFLIGHT: could not locate the call button (signed out / wrong account / DM not loaded)."
    echo "   See $OUT/preflight.log for the dumped button labels."
  fi
  exit "$PF"
fi

# --- bring up the callee --------------------------------------------------------------------
if [ "$NO_CALLEE" = "1" ]; then
  echo ">> NO callee — a HUMAN answers on another device. Skipping callee bring-up + auto-answer."
elif [ "$CALLEE_XVFB" = "1" ]; then
  command -v Xvfb >/dev/null 2>&1 || { echo "ERROR: Xvfb not installed (apt install xvfb)."; teardown_caller; exit 2; }
  if [ ! -d "$CALLEE_PROFILE" ]; then
    echo "ERROR: callee profile not found: $CALLEE_PROFILE — sign a 2nd Brave in as Duc once."
    teardown_caller; exit 2
  fi
  # free the profile: stop any callee Brave already holding it (e.g. a real-display one)
  if pgrep -f "$CALLEE_PROFILE" >/dev/null 2>&1; then
    echo ">> stopping the existing callee Brave to free its profile for the Xvfb relaunch …"
    pkill -f "$CALLEE_PROFILE" 2>/dev/null; sleep 2
    rm -f "$CALLEE_PROFILE"/Singleton* 2>/dev/null
  fi
  echo ">> starting virtual display $CALLEE_DISPLAY + callee Brave (occlusion-proof, fake mic/cam) …"
  Xvfb "$CALLEE_DISPLAY" -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &
  XVFB_CALLEE_PID=$!
  sleep 2
  env -u WAYLAND_DISPLAY DISPLAY="$CALLEE_DISPLAY" brave-browser \
    --user-data-dir="$CALLEE_PROFILE" \
    --remote-debugging-port=9223 \
    --ozone-platform=x11 --use-gl=angle --use-angle=swiftshader \
    --autoplay-policy=no-user-gesture-required \
    --use-fake-ui-for-media-stream --use-fake-device-for-media-stream \
    --disable-features=CalculateNativeWinOcclusion \
    --no-first-run --no-default-browser-check about:blank >/dev/null 2>&1 &
  CALLEE_BRAVE_PID=$!
  up=0
  for _ in $(seq 1 60); do
    curl -s --max-time 2 "$CALLEE_CDP/json/version" >/dev/null 2>&1 && { up=1; break; }
    sleep 0.5
  done
  if [ "$up" != "1" ]; then
    echo "ERROR: callee Brave never came up on $CALLEE_CDP under $CALLEE_DISPLAY."
    teardown_callee; teardown_caller; exit 2
  fi
  echo "  callee ready: virtual display $CALLEE_DISPLAY · port 9223 · profile $(basename "$CALLEE_PROFILE")"
else
  echo "  callee (:9223) = $(cdp_account "$CALLEE_CDP")"
  if ! curl -s --max-time 3 "$CALLEE_CDP/json/version" >/dev/null 2>&1; then
    echo "ERROR: callee browser not reachable on $CALLEE_CDP."
    echo "  ONE-TIME callee setup — launch a 2nd Brave (signed in as Duc) once:"
    echo "    brave-browser --user-data-dir=\"\$PWD/.browser-profile-callee\" \\"
    echo "      --remote-debugging-port=9223 \\"
    echo "      --use-fake-ui-for-media-stream --use-fake-device-for-media-stream"
    echo "  Then sign in as Duc (trantrongducqt@gmail.com), open the DM, and re-run this."
    exit 2
  fi
fi

# --- discovery mode: just dump the callee's button labels (find Answer/Leave names) -------
if [ "$DRY_ANSWER" = "1" ]; then
  echo ">> DISCOVERY: starting auto-answer in --dry-run (dumps callee buttons), then ringing."
  conda run --no-capture-output -n igaming python -u scripts/auto_answer.py \
    --cdp-url "$CALLEE_CDP" --url "$CALLEE_URL" --dry-run --timeout "$DURATION" \
    > "$CALLEE_LOG" 2>&1 &
  ANS=$!
  sleep 2
  conda run --no-capture-output -n igaming python -u scripts/meet_call_browser.py \
    --cdp-url "$CALLER_CDP" --url "$CALLER_URL" --watch-join --join-poll 0.5 \
    --duration "$DURATION" > "$CALLER_LOG" 2>&1 &
  CALL=$!
  wait "$CALL" 2>/dev/null
  kill -INT "$ANS" 2>/dev/null; sleep 1; kill "$ANS" 2>/dev/null
  echo "=== callee buttons seen (find the Answer label) ==="
  grep -E "buttons:|      - " "$CALLEE_LOG" | sort -u
  echo "callee log: $CALLEE_LOG"
  teardown_caller; teardown_callee
  exit 0
fi

# --- real self-test -----------------------------------------------------------------------
# 1) start the auto-answerer FIRST so it's already watching when the ring lands.
if [ "$NO_CALLEE" = "1" ]; then
  ANS=""
  echo ">> waiting for a HUMAN to answer on another device — pick up the ring + SPEAK, then hang up."
  echo "   (the caller rings now; it self-stops when you hang up. Cap = ${DURATION}s.)"
else
  echo ">> starting callee auto-answer (answers, holds ${ANSWER_SECONDS}s, then leaves) …"
  conda run --no-capture-output -n igaming python -u scripts/auto_answer.py \
    --cdp-url "$CALLEE_CDP" --url "$CALLEE_URL" --answer-seconds "$ANSWER_SECONDS" \
    --timeout "$DURATION" > "$CALLEE_LOG" 2>&1 &
  ANS=$!
  sleep 2
fi

# 2) place the call on the caller side WITH audio capture. Default mode=profile captures
#    ONLY the dedicated caller browser's OWN decoded audio (scoped by its --user-data-dir
#    process tree, auto-derived from --cdp-url) → the remote voice, call-only, never the
#    daily browser or other apps. The robust path for Meet (the in-browser 'webrtc' tap is
#    blind to Meet's decoded audio). Routing is always restored on stop.
echo ">> ringing (caller captures audio [$AUDIO_MODE] to $AUDIO_OUT) …"
conda run --no-capture-output -n igaming python -u scripts/meet_call_browser.py \
  --cdp-url "$CALLER_CDP" --url "$CALLER_URL" --watch-join --join-poll 0.5 \
  --duration "$DURATION" --capture-audio --audio-mode "$AUDIO_MODE" --audio-out "$AUDIO_OUT" \
  $DIAG_FLAG $FG_FLAG \
  > "$CALLER_LOG" 2>&1 &
CALL=$!

# 3) wait for the caller to self-terminate (on the survey) or hit the cap.
wait "$CALL" 2>/dev/null
[ -n "$ANS" ] && { kill -INT "$ANS" 2>/dev/null; sleep 1; kill "$ANS" 2>/dev/null; }

# Tear down the caller + callee Braves BEFORE the final reap. The `brave-browser` launcher
# stays alive as the PARENT of the real browser on this machine (it does NOT exec/detach), so
# the browser we launched with `&` is a live background job — a bare `wait` here would BLOCK on
# it until teardown kills it, which is a DEADLOCK (the script hung at end-of-run). Kill first,
# THEN reap the now-dead job entries (returns immediately).
teardown_caller
teardown_callee
wait 2>/dev/null

# --- verdict ------------------------------------------------------------------------------
echo ""
echo "=== RESULT ==="
JOIN=$(grep -m1 "REMOTE JOINED" "$CALLER_LOG" || true)
if [ "$NO_CALLEE" = "1" ]; then
  # No callee log — a human answered on another device. The caller's own "REMOTE JOINED"
  # line is the pick-up proof, and "called left" is implied by the hang-up end-reason.
  ANSWERED="$JOIN"; LEFT=""
else
  ANSWERED=$(grep -m1 "^ANSWERED " "$CALLEE_LOG" || true)
  LEFT=$(grep -m1 "^LEFT" "$CALLEE_LOG" || true)
fi
# Any hang-up end-reason the caller prints ("📴 Call ended — <reason>. Stopping.") counts as
# hang-up DETECTED — the survey is ONE such signal, but "call controls disappeared", a frame
# teardown, or a roster collapse are equally valid and may win the race on this embedded UI.
ENDREASON=$(grep -m1 "📴 Call ended" "$CALLER_LOG" | sed -E 's/.*Call ended — //' || true)
SURVEY=$(grep -m1 "rating survey shown (hung up)" "$CALLER_LOG" || true)
CAP=$(grep -m1 "Reached the .* cap" "$CALLER_LOG" || true)

echo "  callee answered : ${ANSWERED:-NO}"
echo "  caller saw join : ${JOIN:-NO}   (note: WebRTC join can false-fire on ringback)"
echo "  callee left     : ${LEFT:-NO}"
echo "  caller end-reason: ${ENDREASON:-NONE}"
[ -n "$SURVEY" ] && echo "    ↳ ✓ via the post-call rating survey (the suggested signal)"
[ -n "$CAP" ] && echo "  ⚠️  caller hit the duration cap: $CAP"

# audio: was a non-silent WAV captured? (silence ≈ -91 dB)
APATH=$(grep -m1 "^CALL_AUDIO " "$CALLER_LOG" | awk '{print $2}')
[ -z "$APATH" ] && APATH="$AUDIO_OUT"
AUDIO_OK=0
if [ -f "$APATH" ]; then
  VOL=$(ffmpeg -hide_banner -nostats -i "$APATH" -af volumedetect -f null - 2>&1 \
        | grep -E "mean_volume" | head -1)
  MEAN=$(echo "$VOL" | grep -oE "\-?[0-9.]+ dB" | head -1 | grep -oE "\-?[0-9.]+")
  # non-silent ≈ mean louder than -80 dB (true digital silence is ~-91 dB)
  if [ -n "$MEAN" ] && awk "BEGIN{exit !($MEAN > -80)}"; then AUDIO_OK=1; fi
  echo "  audio captured  : $APATH  ($VOL)"
else
  echo "  audio captured  : NO FILE ($APATH)"
fi

echo ""
# Two independent verdicts: (1) hang-up detection (the user's core ask), (2) audio capture.
if [ -n "$ANSWERED" ] && [ -n "$ENDREASON" ] && [ -z "$CAP" ]; then
  echo "✅ HANG-UP DETECTION: PASS — rang, auto-answered, and self-stopped on hang-up"
  echo "                       (\"$ENDREASON\"), unattended — NOT on the duration cap."
else
  echo "❌ HANG-UP DETECTION: CHECK — answered=${ANSWERED:+yes} end=${ENDREASON:-none} cap=${CAP:+HIT}"
fi
if [ "$AUDIO_OK" = "1" ]; then
  echo "✅ AUDIO CAPTURE: PASS — non-silent WAV captured (the remote voice the bot heard)."
elif [ "$NO_CALLEE" = "1" ]; then
  echo "❌ AUDIO CAPTURE: CHECK — WAV silent/missing (did the human actually SPEAK on the call?)."
else
  echo "❌ AUDIO CAPTURE: CHECK — WAV silent/missing (callee fake-mic may not be transmitting)."
fi
echo ""
echo "  caller log: $CALLER_LOG"
[ "$NO_CALLEE" = "1" ] || echo "  callee log: $CALLEE_LOG"
echo "  OUT=$OUT"
