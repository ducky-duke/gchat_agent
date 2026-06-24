#!/usr/bin/env bash
#
# demo_live_apigw.sh — focused LIVE demo of the "API gateway timeout" incident,
# end to end: QA clarification → resolve → outbound VOICE CALL.
#
# This is the call-centric cousin of demo_live.sh. demo_live.sh proves the
# GitHub-export + voice-DM story but leaves the outbound call as a SILENT side
# effect (it never waits for, surfaces, or confirms it). This script makes the
# CALL the headline: it drives the QA loop on the single `apigw` scenario, then
# detects the bot placing the call, surfaces the call PID + log, and tails the
# call live so you can watch the AI relay the incident over a real Chat call.
#
# What it shows, start to finish:
#   1. a staff persona ('apigw', posting as the on-call engineer's account) seeds
#      the incident — "API gateway timing out (504s) in prod" — into GOOGLE_SPACE;
#   2. the bot detects it and asks clarifying questions; the staff answers in
#      character (one fact per reply); the bot RESOLVES the issue (the QA loop);
#   3. on resolve the bot — CALL_ON_RESOLVE is on and GEMINI_API_KEY is set —
#      spawns call/gemini_call.py --incident-file <json>: Gemini Live calls you
#      (the callee) on a real Google Chat call and relays the CLARIFIED report
#      (the facts the bot actually extracted in step 2, NOT a static script);
#   4. this script surfaces the call (PID + log), tears down the bot (the call is
#      a detached process that survives), and tails the call log so you can watch
#      ring → pickup → relay → hang-up.
#   GitHub export + the voice DM still fire on the same resolve and are reported
#   as secondary confirmations.
#
# How this differs from `call_apigw.sh`: call_apigw.sh dials the call directly
# from the STATIC apigw scenario facts (no bot, no QA). Here the call is placed
# BY THE BOT from the facts it clarified live in the QA loop — the full pipeline.
#
# Usage:
#   ./demo_live_apigw.sh                  # English; callee name auto-read from the GOOGLE_CHAT_REPORT_SPACE DM
#   ./demo_live_apigw.sh --language vi    # the AI relays in Vietnamese (vi/ru/uk also ok)
#   ./demo_live_apigw.sh --callee Bob     # force the callee name (else it's auto-resolved)
#   ./demo_live_apigw.sh --timeout 900    # wait up to 15 min for the QA to resolve
#   ./demo_live_apigw.sh --call-wait 300  # keep tailing the live call up to 5 min
#   ./demo_live_apigw.sh --token secrets/token_promo.json  # seed as a specific account
#   ./demo_live_apigw.sh --keep-running   # leave the poller up after the call is placed
#
# Requirements (all already set up in this checkout — demo machine only):
#   * .env with a live GEMINI_API_KEY — it powers BOTH the LLM transport
#     (LLM_PROVIDER=gemini) AND the outbound call gate, so it is a HARD requirement
#     here (the call is the whole point of this demo); GOOGLE_SPACE +
#     GOOGLE_CHAT_REPORT_SPACE (the DM the call rings), and the OAuth tokens under
#     secrets/ (token_bot.json + a staff token);
#   * the dedicated caller Brave profile (.browser-profile-caller) signed in as
#     the bot account (first run, gemini_call.py prints the one-time sign-in cmd);
#   * a VISIBLE desktop session — native Wayland suspends an occluded renderer,
#     which drops the call. Keep the caller window on screen;
#   * `jq` and (optionally) the `gh` CLI to confirm the GitHub issue server-side.
#
# Override the interpreter if your conda env lives elsewhere:
#   IGAMING_PYTHON=/path/to/python ./demo_live_apigw.sh
#
set -euo pipefail

# Always run from the repo root so .env, secrets/, scripts/, call/ and data/ resolve.
cd "$(dirname "${BASH_SOURCE[0]}")"

# --- defaults + arg parsing -------------------------------------------------
PERSONA="apigw"       # this demo is the API-gateway-timeout incident, full stop
TIMEOUT=600           # seconds to wait for the QA loop to resolve the issue
CALL_WAIT=260         # seconds to keep tailing the live call after it is placed
                      # (gemini_call default duration is 180s; + buffer)
STAFF_TOKEN=""        # the account the incident is reported from (default: ops)
CALL_LANGUAGE_OVR=""  # --language → exported as CALL_LANGUAGE for the poller
CALL_CALLEE_OVR=""    # --callee   → exported as CALL_CALLEE for the poller
KEEP_RUNNING=0        # 1 = leave the poller running after the call is placed

# Print the header comment block (lines 2..first non-comment line), `# `-stripped.
usage() { awk 'NR>=2{ if($0 !~ /^#/) exit; sub(/^# ?/,""); print }' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --timeout)   TIMEOUT="${2:?--timeout needs seconds}"; shift 2 ;;
    --call-wait) CALL_WAIT="${2:?--call-wait needs seconds}"; shift 2 ;;
    --token)     STAFF_TOKEN="${2:?--token needs a path}"; shift 2 ;;
    --language)  CALL_LANGUAGE_OVR="${2:?--language needs en|vi|ru|uk}"; shift 2 ;;
    --callee)    CALL_CALLEE_OVR="${2:?--callee needs a name}"; shift 2 ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "demo_live_apigw: unknown arg '$1'" >&2; usage 1 ;;
  esac
done

# The apigw persona reports as the ops account ("the on-call engineer") unless
# --token overrides it.
[ -n "$STAFF_TOKEN" ] || STAFF_TOKEN="secrets/token_ops.json"

# --- small helpers ----------------------------------------------------------
log()  { printf '\033[1;36m[demo]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[demo] FAIL:\033[0m %s\n' "$*" >&2; exit 1; }

# Read a KEY=value from .env, stripping any inline `# comment` and trailing space.
envget() { sed -nE "s/^$1=([^#]*).*/\1/p" .env | head -1 | sed -E 's/[[:space:]]+$//'; }

# --- resolve config from .env ----------------------------------------------
[ -f .env ] || die ".env not found (run from the repo root)."
GOOGLE_SPACE="$(envget GOOGLE_SPACE)"
VOICE_SPACE="$(envget GOOGLE_CHAT_REPORT_SPACE)"
REPORT_DELIVERY="$(envget REPORT_DELIVERY)"
GITHUB_REPO="$(envget GITHUB_REPO)"
GITHUB_ISSUES="$(envget GITHUB_ISSUES)"
GITHUB_ACCOUNT="$(envget GITHUB_ACCOUNT)"; GITHUB_ACCOUNT="${GITHUB_ACCOUNT:-ducky-duke}"

PY="${IGAMING_PYTHON:-$HOME/miniconda3/envs/igaming/bin/python}"

# --- preflight --------------------------------------------------------------
log "Preflight (apigw incident → QA → resolve → CALL)"
[ -x "$PY" ] || die "Python interpreter not found at $PY (set IGAMING_PYTHON)."
ok "interpreter: $PY ($("$PY" --version 2>&1))"

[ -f "$STAFF_TOKEN" ]          || die "staff token not found: $STAFF_TOKEN"
[ -f secrets/token_bot.json ]  || die "bot token not found: secrets/token_bot.json"
[ -f data/scenarios.json ]     || die "data/scenarios.json missing."
[ -f call/gemini_call.py ]     || die "call/gemini_call.py missing (CALL_SCRIPT)."
"$PY" - "$PERSONA" <<'PY' || die "persona '$PERSONA' not found in data/scenarios.json"
import json, sys
data = json.load(open("data/scenarios.json"))
sys.exit(0 if sys.argv[1] in data else 1)
PY
ok "persona '$PERSONA' present; reporting as $STAFF_TOKEN"

# The outbound Gemini Live call rides the resolve regardless of REPORT_DELIVERY
# (the legacy TTS voice-DM report is retired), so it is no longer required to be
# voice|both. GOOGLE_CHAT_REPORT_SPACE is the DM the call rings — required here.
[ -n "$GOOGLE_SPACE" ] || die "GOOGLE_SPACE is empty in .env."
[ -n "$VOICE_SPACE" ]  || die "GOOGLE_CHAT_REPORT_SPACE is empty — the outbound call has nowhere to ring."
ok "chat space: $GOOGLE_SPACE   call DM: $VOICE_SPACE   delivery: $REPORT_DELIVERY"

# The CALL gate: GEMINI_API_KEY. Without it the bot self-gates the call to a
# SILENT skip — which would make this demo pass the QA but never call. So we
# treat a missing key as a HARD failure here (mirrors call_apigw.sh's preflight).
GEMINI_OK=0
if [ -n "${GEMINI_API_KEY:-}" ] || [ -n "${GOOGLE_API_KEY:-}" ]; then
  GEMINI_OK=1
elif grep -qE '^[[:space:]]*(GEMINI_API_KEY|GOOGLE_API_KEY)[[:space:]]*=[[:space:]]*[^[:space:]#]' .env 2>/dev/null; then
  GEMINI_OK=1
fi
[ "$GEMINI_OK" -eq 1 ] || die "no GEMINI_API_KEY (checked env + .env) — the bot would skip the call. Set it, then re-run."
ok "GEMINI_API_KEY present — the bot will place the call on resolve (CALL_ON_RESOLVE default ON)"

# Soft checks for the call hardware: the dedicated caller profile + a visible
# desktop. Don't fail (first run signs the profile in; the user knows their
# display), just warn loudly so a dropped call isn't a mystery.
[ -d .browser-profile-caller ] || warn "no .browser-profile-caller yet — first call run prints the one-time sign-in command (the call may just open a login window)."
warn "keep the caller browser window VISIBLE — Wayland suspends an occluded renderer and drops the call."

# Optional CALL overrides → exported so the poller's Config picks them up
# (os.environ overrides .env in load_config).
if [ -n "$CALL_LANGUAGE_OVR" ]; then
  case "$CALL_LANGUAGE_OVR" in en|vi|ru|uk) ;; *) die "--language must be en|vi|ru|uk (got '$CALL_LANGUAGE_OVR')." ;; esac
  export CALL_LANGUAGE="$CALL_LANGUAGE_OVR"
  ok "call language override: $CALL_LANGUAGE"
fi
if [ -n "$CALL_CALLEE_OVR" ]; then
  export CALL_CALLEE="$CALL_CALLEE_OVR"
  ok "call callee override: $CALL_CALLEE"
fi

# Optional GitHub server-side confirmation (best-effort — not required for this
# call-focused demo). If gh + a token are present we snapshot a baseline so we
# can confirm the resolve filed a new issue too.
GH_OK=0
GH_DUCKY_TOKEN=""
if [ "$GITHUB_ISSUES" = "true" ] && [ -n "$GITHUB_REPO" ] && command -v gh >/dev/null 2>&1; then
  GH_DUCKY_TOKEN="$(gh auth token --user "$GITHUB_ACCOUNT" 2>/dev/null || true)"
  if [ -n "$GH_DUCKY_TOKEN" ]; then GH_OK=1; fi
fi
gh_q() { GH_TOKEN="$GH_DUCKY_TOKEN" gh "$@"; }
issue_max() { gh_q issue list -R "$GITHUB_REPO" --state all --limit 30 --json number --jq '[.[].number] | max // 0' 2>/dev/null || echo 0; }
BASELINE=0
if [ "$GH_OK" -eq 1 ]; then
  BASELINE="$(issue_max)"; BASELINE="${BASELINE:-0}"
  ok "github: $GITHUB_ACCOUNT can read $GITHUB_REPO (baseline issue #$BASELINE)"
else
  warn "skipping GitHub server-side confirmation (GITHUB_ISSUES!=true, no repo, or no gh token) — call demo unaffected."
fi

# --- process bookkeeping + cleanup -----------------------------------------
RUN_DIR="$(mktemp -d /tmp/gchat-apigw-demo.XXXXXX)"
POLLER_LOG="$RUN_DIR/poller.log"
STAFF_LOG="$RUN_DIR/staff.log"
POLLER_PID=""
STAFF_PID=""
TAIL_PID=""

stop_pid() { # graceful SIGINT (clean lock release + background drain), then KILL
  local pid="$1"
  [ -n "$pid" ] || return 0
  kill -INT "$pid" 2>/dev/null || return 0
  for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.5; done
  kill -KILL "$pid" 2>/dev/null || true
}

cleanup() {
  # The voice CALL is a DETACHED process (start_new_session) — we deliberately do
  # NOT kill it here; it must outlive the poller so you can finish the call.
  [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null || true
  if [ "$KEEP_RUNNING" -eq 1 ] && [ -n "$POLLER_PID" ]; then
    log "Leaving the poller running (PID $POLLER_PID) — stop it with: kill -INT $POLLER_PID"
    stop_pid "$STAFF_PID"
    return
  fi
  log "Shutting down (staff + poller; the call keeps running if live)…"
  stop_pid "$STAFF_PID"
  stop_pid "$POLLER_PID"
  log "Logs kept at: $RUN_DIR"
}
trap cleanup EXIT INT TERM

# --- fresh session ----------------------------------------------------------
log "Resetting previous-session state (.state/) for a clean run"
rm -rf .state
ok "state cleared (poll cursor + issue store)"

# --- launch the bot (poller) ------------------------------------------------
log "Starting the issue-spotter bot (poller)…"
"$PY" -u scripts/run_poller.py >"$POLLER_LOG" 2>&1 &
POLLER_PID=$!
# Wait for the poller's first cycle to pin the poll cursor to "now" (it writes
# .state/issues.json each cycle). Seeding BEFORE this would post under the cursor
# and be skipped (the bot does no history backfill).
for _ in $(seq 1 30); do
  [ -f .state/issues.json ] && break
  kill -0 "$POLLER_PID" 2>/dev/null || die "poller exited early — see $POLLER_LOG:
$(tail -n 30 "$POLLER_LOG")"
  sleep 1
done
[ -f .state/issues.json ] || die "poller never pinned its cursor — see $POLLER_LOG"
ok "bot is polling $GOOGLE_SPACE (PID $POLLER_PID); cursor pinned"

# --- launch the staff persona (seeds the incident, then answers) -----------
# A fresh per-run suffix makes the seed/answer request_ids unique to THIS run so
# the demo is re-runnable against the same space (the no-backfill bot would never
# re-detect posts Chat deduped to a prior run's messages).
SEED_SUFFIX="$(date +%H%M%S)-$$"
log "Starting staff persona '$PERSONA' — it reports the incident and answers the bot…"
"$PY" -u scripts/run_staff.py --persona "$PERSONA" --token "$STAFF_TOKEN" \
  --seed-suffix "$SEED_SUFFIX" >"$STAFF_LOG" 2>&1 &
STAFF_PID=$!
sleep 2
kill -0 "$STAFF_PID" 2>/dev/null || die "staff exited early — see $STAFF_LOG:
$(tail -n 30 "$STAFF_LOG")"
ok "staff is live (PID $STAFF_PID); incident seeded into the space"

# --- watch the QA loop until the bot resolves AND places the call -----------
log "Watching the QA loop until the bot resolves the incident and places the call (timeout ${TIMEOUT}s)…"
log "  live bot log:   tail -f $POLLER_LOG"
log "  live staff log: tail -f $STAFF_LOG"

CALL_LINE=""
RESOLVED_GH=0
START=$SECONDS
LAST_NOTE=""
while [ $((SECONDS - START)) -lt "$TIMEOUT" ]; do
  # Surface QA progress from the bot log as it happens (deduped).
  note="$(grep -hoE 'cycle [^(]*' "$POLLER_LOG" 2>/dev/null | tail -n 1 || true)"
  if [ -n "$note" ] && [ "$note" != "$LAST_NOTE" ]; then
    printf '      bot: %s\n' "$(echo "$note" | sed -E 's/[[:space:]]+$//')"
    LAST_NOTE="$note"
  fi

  # PRIMARY signal: the bot logged that it placed the voice call (resolve fired
  # and the detached gemini_call.py launched).
  CALL_LINE="$(grep -h 'placing voice call for issue' "$POLLER_LOG" 2>/dev/null | tail -n 1 || true)"
  [ -n "$CALL_LINE" ] && break

  # Secondary server-side proof of the resolve (the call rides the same resolve;
  # if it appears we keep looping a few beats for the call line to flush).
  if [ "$GH_OK" -eq 1 ]; then
    latest="$(issue_max)"; latest="${latest:-0}"
    [ "$latest" -gt "$BASELINE" ] && RESOLVED_GH=1
  fi

  # Bail fast if the bot died; the staff exiting is fine (it may be done answering).
  kill -0 "$POLLER_PID" 2>/dev/null || die "poller died mid-run — see $POLLER_LOG:
$(tail -n 30 "$POLLER_LOG")"
  kill -0 "$STAFF_PID"  2>/dev/null || true
  sleep 4
done

# If the resolve clearly happened (GitHub issue) but the call line hasn't shown
# yet, give it a short grace window to flush before diagnosing.
if [ -z "$CALL_LINE" ] && [ "$RESOLVED_GH" -eq 1 ]; then
  for _ in 1 2 3 4 5; do
    CALL_LINE="$(grep -h 'placing voice call for issue' "$POLLER_LOG" 2>/dev/null | tail -n 1 || true)"
    [ -n "$CALL_LINE" ] && break
    sleep 2
  done
fi

# --- classify the outcome ---------------------------------------------------
if [ -z "$CALL_LINE" ]; then
  # No call was placed. Explain why if the bot logged a reason; otherwise it
  # probably just hasn't resolved within the timeout.
  echo
  if grep -q 'already in progress' "$POLLER_LOG" 2>/dev/null; then
    warn "the bot SKIPPED the call (a prior call was still in flight) — serialized by design."
  elif grep -q 'voice call launch failed' "$POLLER_LOG" 2>/dev/null; then
    warn "the call FAILED to launch:"
    warn "$(grep -h 'voice call launch failed' "$POLLER_LOG" | tail -n 1)"
  elif grep -q 'was not found' "$POLLER_LOG" 2>/dev/null; then
    warn "the bot could not find the call script (CALL_SCRIPT):"
    warn "$(grep -h 'was not found' "$POLLER_LOG" | tail -n 1)"
  fi
  die "no voice call was placed within ${TIMEOUT}s.
Last bot log lines:
$(tail -n 25 "$POLLER_LOG")
Last staff log lines:
$(tail -n 10 "$STAFF_LOG")"
fi

# --- the call was placed — surface it --------------------------------------
# Line shape: "[issue-spotter] placing voice call for issue <id> (pid N) — relaying
#              to <callee> in <lang>; call log → logs/call-issue-<id>.log"
CALL_PID="$(printf '%s\n' "$CALL_LINE" | grep -oE 'pid [0-9]+' | grep -oE '[0-9]+' | head -1)"
CALL_LOG="$(printf '%s\n' "$CALL_LINE" | sed -n 's/.*call log → //p' | head -1)"

echo
log "📞 CALL PLACED — the bot resolved the incident and is calling the human"
ok "$(printf '%s' "$CALL_LINE" | sed 's/^\[issue-spotter\] //')"
[ -n "$CALL_PID" ] && ok "call process PID: $CALL_PID (detached — it survives the bot shutdown)"
[ -n "$CALL_LOG" ] && ok "call log:         $CALL_LOG"

# Confirm the in-thread resolution + the side channels from the bot log.
if grep -q "RESOLVED" "$POLLER_LOG" 2>/dev/null; then
  ok "issue resolved in-thread (the QA loop reached clarity)"
fi

# --- stop the bot; the call is detached and keeps going ---------------------
# We no longer need the poller for this single-incident demo. Stopping it now
# (unless --keep-running) drains the background voice + GitHub pools cleanly,
# while the detached call continues. Then we report those side channels.
if [ "$KEEP_RUNNING" -eq 0 ]; then
  log "Stopping the bot (the call is detached and continues)…"
  stop_pid "$STAFF_PID"; STAFF_PID=""
  stop_pid "$POLLER_PID"; POLLER_PID=""
  ok "bot stopped; background voice/GitHub pools drained"
fi

echo
log "Side channels on the same resolve"
# Spoken delivery IS the call (tailed above); the legacy TTS voice-DM report is
# retired, so there is no separate voice-report confirmation here.
# GitHub export (best-effort + optional server-side confirmation).
if grep -q "filed GitHub issue for" "$POLLER_LOG" 2>/dev/null; then
  ok "github: $(grep -h 'filed GitHub issue for' "$POLLER_LOG" | tail -n 1 | sed 's/^\[issue-spotter\] //')"
fi
if [ "$GH_OK" -eq 1 ]; then
  NEW_NUM="$(issue_max)"; NEW_NUM="${NEW_NUM:-0}"
  if [ "$NEW_NUM" -gt "$BASELINE" ]; then
    URL="$(gh_q issue view "$NEW_NUM" -R "$GITHUB_REPO" --json url --jq '.url' 2>/dev/null || true)"
    ok "github (server-side): new issue #$NEW_NUM filed — ${URL:-$GITHUB_REPO#$NEW_NUM}"
  fi
fi

# --- tail the live call -----------------------------------------------------
echo
log "Live call (watch your phone / the caller window) — tailing the call log up to ${CALL_WAIT}s"
log "  full log: tail -f ${CALL_LOG:-logs/call-issue-*.log}"
if [ -n "$CALL_LOG" ]; then
  # Wait for the log file to exist (the child may take a moment to open it).
  for _ in 1 2 3 4 5; do [ -f "$CALL_LOG" ] && break; sleep 1; done
fi
if [ -n "$CALL_LOG" ] && [ -f "$CALL_LOG" ]; then
  # Follow the call log in the background; stop following when the call process
  # exits or CALL_WAIT elapses. The call (gemini_call.py) prints its own lifecycle
  # (ring/pickup/transcript/hang-up) to this log.
  tail -n +1 -F "$CALL_LOG" 2>/dev/null & TAIL_PID=$!
  CSTART=$SECONDS
  while [ $((SECONDS - CSTART)) -lt "$CALL_WAIT" ]; do
    # The call ended (process gone) → stop tailing.
    if [ -n "$CALL_PID" ]; then
      kill -0 "$CALL_PID" 2>/dev/null || break
    fi
    sleep 2
  done
  kill "$TAIL_PID" 2>/dev/null || true; TAIL_PID=""
  echo
  if [ -n "$CALL_PID" ] && kill -0 "$CALL_PID" 2>/dev/null; then
    warn "call still in progress after ${CALL_WAIT}s (PID $CALL_PID) — it keeps running; tail -f $CALL_LOG to follow, or kill -INT $CALL_PID to hang up."
  else
    ok "call finished (process exited). Full transcript: $CALL_LOG"
  fi
else
  warn "could not locate the call log to tail; check logs/ (CALL_LOG_DIR) for call-issue-*.log."
  # Best-effort: still wait on the PID so we don't tear down a live call.
  if [ -n "$CALL_PID" ]; then
    CSTART=$SECONDS
    while [ $((SECONDS - CSTART)) -lt "$CALL_WAIT" ] && kill -0 "$CALL_PID" 2>/dev/null; do sleep 3; done
  fi
fi

# --- final summary ----------------------------------------------------------
echo
log "Demo complete — apigw incident: detected → clarified → resolved → CALLED"
log "  • Chat space (QA thread):   $GOOGLE_SPACE"
log "  • Call DM:                  $VOICE_SPACE"
[ -n "$CALL_LOG" ] && log "  • Call transcript:          $CALL_LOG"
log "  • Bot log:                  $POLLER_LOG"
exit 0
