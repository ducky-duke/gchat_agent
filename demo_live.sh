#!/usr/bin/env bash
#
# demo_live.sh — end-to-end LIVE demo of the Google Chat issue-spotter.
#
# What it shows, start to finish:
#   1. a staff persona posts a technical incident — "API gateway timing out
#      (504s) in prod" — into the real Chat space (GOOGLE_SPACE);
#   2. the bot detects it, asks clarifying questions, the staff answers in
#      character (one fact per reply), and the bot RESOLVES the issue;
#   3. on resolve the bot files a GitHub issue (resolution report + the collected
#      thread transcript) into the PRIVATE repo (GITHUB_REPO, ducky-duke);
#   4. the voice report (audio MP3 + spoken transcript) is delivered to the DM
#      space (GOOGLE_VOICE_SPACE);
#   5. CONTROL CASE — a second account ("noise" persona) drops benign small talk
#      (lunch, last night's match) into the space at the same time. The bot must
#      NOT open or file an issue for it. The demo proves this by reading the bot's
#      own state at the end: it opened EXACTLY ONE issue (the incident), ignoring
#      the chatter. This is the "does it have judgment, or file everything?" proof.
#
# The script drives all live participants for you — it starts the poller (the
# bot) and the staff personas as background processes, then WATCHES until a brand
# new GitHub issue appears on the server (server-side proof) and the poller log
# confirms the voice DM. It tears every process down cleanly on exit.
#
# Usage:
#   ./demo_live.sh                       # default: apigw persona + noise control
#   ./demo_live.sh --persona apigw       # API gateway timeout (the requested demo)
#   ./demo_live.sh --persona ops         # Skrill payout webhook timeout
#   ./demo_live.sh --no-noise            # skip the control case (incident only)
#   ./demo_live.sh --timeout 900         # wait up to 15 min for the resolve
#   ./demo_live.sh --token secrets/token_promo.json   # post as a specific account
#
# Requirements (all already set up in this checkout):
#   * .env with GITHUB_ISSUES=true, REPORT_DELIVERY=voice|both, a live
#     OPENROUTER_API_KEY, GOOGLE_SPACE + GOOGLE_VOICE_SPACE, and the OAuth tokens
#     under secrets/ (token_bot.json + a staff token);
#   * the `gh` CLI logged in to the GITHUB_ACCOUNT (ducky-duke) so the script can
#     read the private repo to confirm the filed issue;
#   * `jq`.
#
# Override the interpreter if your conda env lives elsewhere:
#   IGAMING_PYTHON=/path/to/python ./demo_live.sh
#
set -euo pipefail

# Always run from the repo root so .env, secrets/, scripts/ and data/ resolve.
cd "$(dirname "${BASH_SOURCE[0]}")"

# --- defaults + arg parsing -------------------------------------------------
PERSONA="apigw"
TIMEOUT=600
STAFF_TOKEN=""        # auto-derived from the persona unless overridden
KEEP_RUNNING=0        # 1 = leave the poller running after the resolve
NOISE_ENABLED=1       # 1 = also seed the benign "noise" control persona

usage() { sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --persona) PERSONA="${2:?--persona needs a value}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout needs seconds}"; shift 2 ;;
    --token)   STAFF_TOKEN="${2:?--token needs a path}"; shift 2 ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    --no-noise) NOISE_ENABLED=0; shift ;;
    -h|--help) usage 0 ;;
    *) echo "demo_live: unknown arg '$1'" >&2; usage 1 ;;
  esac
done

# A staff persona posts as a real authenticated Gmail account (its OAuth token).
# We have token_ops.json + token_promo.json; the API-gateway persona reuses the
# ops account as "the on-call engineer" unless --token overrides it.
if [ -z "$STAFF_TOKEN" ]; then
  case "$PERSONA" in
    promo) STAFF_TOKEN="secrets/token_promo.json" ;;
    *)     STAFF_TOKEN="secrets/token_ops.json" ;;
  esac
fi

# The noise control posts as the OTHER account so the banter reads as a different
# person chatting alongside the incident reporter.
case "$STAFF_TOKEN" in
  secrets/token_promo.json) NOISE_TOKEN="secrets/token_ops.json" ;;
  *)                        NOISE_TOKEN="secrets/token_promo.json" ;;
esac

# --- small helpers ----------------------------------------------------------
log()  { printf '\033[1;36m[demo]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[demo] FAIL:\033[0m %s\n' "$*" >&2; exit 1; }

# Read a KEY=value from .env, stripping any inline `# comment` and trailing space.
envget() { sed -nE "s/^$1=([^#]*).*/\1/p" .env | head -1 | sed -E 's/[[:space:]]+$//'; }

# --- resolve config from .env ----------------------------------------------
[ -f .env ] || die ".env not found (run from the repo root)."
GITHUB_REPO="$(envget GITHUB_REPO)"
GITHUB_ACCOUNT="$(envget GITHUB_ACCOUNT)"; GITHUB_ACCOUNT="${GITHUB_ACCOUNT:-ducky-duke}"
GOOGLE_SPACE="$(envget GOOGLE_SPACE)"
VOICE_SPACE="$(envget GOOGLE_VOICE_SPACE)"
REPORT_DELIVERY="$(envget REPORT_DELIVERY)"
GITHUB_ISSUES="$(envget GITHUB_ISSUES)"

PY="${IGAMING_PYTHON:-$HOME/miniconda3/envs/igaming/bin/python}"

# --- preflight --------------------------------------------------------------
log "Preflight"
[ -x "$PY" ] || die "Python interpreter not found at $PY (set IGAMING_PYTHON)."
ok "interpreter: $PY ($("$PY" --version 2>&1))"
command -v jq >/dev/null 2>&1 || die "jq not found (apt install jq)."
command -v gh >/dev/null 2>&1 || die "gh CLI not found."

[ -f "$STAFF_TOKEN" ]            || die "staff token not found: $STAFF_TOKEN"
[ -f secrets/token_bot.json ]   || die "bot token not found: secrets/token_bot.json"
[ -f data/scenarios.json ]      || die "data/scenarios.json missing."
"$PY" - "$PERSONA" <<'PY' || die "persona not found in data/scenarios.json"
import json, sys
data = json.load(open("data/scenarios.json"))
sys.exit(0 if sys.argv[1] in data else 1)
PY
ok "persona '$PERSONA' present; posting as $STAFF_TOKEN"

# Noise control persona: needs the 'noise' entry in scenarios.json and a second
# account token. If either is missing, degrade gracefully (skip, don't fail).
NOISE_COUNT=0
if [ "$NOISE_ENABLED" -eq 1 ]; then
  if ! "$PY" - <<'PY'
import json, sys
data = json.load(open("data/scenarios.json"))
sys.exit(0 if "noise" in data else 1)
PY
  then
    warn "no 'noise' persona in scenarios.json — skipping the control case."
    NOISE_ENABLED=0
  elif [ ! -f "$NOISE_TOKEN" ] || [ "$NOISE_TOKEN" = "$STAFF_TOKEN" ]; then
    warn "no distinct second account token ($NOISE_TOKEN) — skipping the control case."
    NOISE_ENABLED=0
  else
    NOISE_COUNT="$("$PY" -c 'import json;print(len(json.load(open("data/scenarios.json"))["noise"]["seed_messages"]))')"
    ok "control case ON: 'noise' persona posts $NOISE_COUNT benign message(s) as $NOISE_TOKEN"
  fi
fi

[ "$GITHUB_ISSUES" = "true" ] || die "GITHUB_ISSUES is not 'true' in .env — the GitHub export is off."
case "$REPORT_DELIVERY" in voice|both) ;; *) die "REPORT_DELIVERY='$REPORT_DELIVERY' — set it to voice|both for the audio DM." ;; esac
[ -n "$GOOGLE_SPACE" ]  || die "GOOGLE_SPACE is empty in .env."
[ -n "$VOICE_SPACE" ]   || warn "GOOGLE_VOICE_SPACE empty — the voice report will land in the issue thread, not a DM."
[ -n "$GITHUB_REPO" ]   || die "GITHUB_REPO is empty in .env."
ok "chat space: $GOOGLE_SPACE   voice DM: ${VOICE_SPACE:-<issue thread>}"
ok "github repo: $GITHUB_REPO   delivery: $REPORT_DELIVERY"

# A token for the private repo so we can confirm the filed issue server-side.
GH_DUCKY_TOKEN="$(gh auth token --user "$GITHUB_ACCOUNT" 2>/dev/null || true)"
[ -n "$GH_DUCKY_TOKEN" ] || die "no gh token for account '$GITHUB_ACCOUNT' (run: gh auth login --user $GITHUB_ACCOUNT)."
gh_q() { GH_TOKEN="$GH_DUCKY_TOKEN" gh "$@"; }
gh_q repo view "$GITHUB_REPO" --json name >/dev/null 2>&1 || die "cannot read $GITHUB_REPO as $GITHUB_ACCOUNT."
ok "github access: $GITHUB_ACCOUNT can read $GITHUB_REPO"

# --- process bookkeeping + cleanup -----------------------------------------
RUN_DIR="$(mktemp -d /tmp/gchat-demo.XXXXXX)"
POLLER_LOG="$RUN_DIR/poller.log"
STAFF_LOG="$RUN_DIR/staff.log"
NOISE_LOG="$RUN_DIR/noise.log"
POLLER_PID=""
STAFF_PID=""
NOISE_PID=""

stop_pid() { # graceful SIGINT (clean lock release + background drain), then KILL
  local pid="$1"
  [ -n "$pid" ] || return 0
  kill -INT "$pid" 2>/dev/null || return 0
  for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.5; done
  kill -KILL "$pid" 2>/dev/null || true
}

cleanup() {
  if [ "$KEEP_RUNNING" -eq 1 ] && [ -n "$POLLER_PID" ]; then
    log "Leaving the poller running (PID $POLLER_PID) — stop it with: kill -INT $POLLER_PID"
    stop_pid "$NOISE_PID"
    stop_pid "$STAFF_PID"
    return
  fi
  log "Shutting down (staff + poller)…"
  stop_pid "$NOISE_PID"
  stop_pid "$STAFF_PID"
  stop_pid "$POLLER_PID"
  log "Logs kept at: $RUN_DIR"
}
trap cleanup EXIT INT TERM

# --- GitHub baseline (so we detect a NEW issue, not an old one) -------------
issue_field() { # $1=jq expr → first/all issues as JSON, newest first
  gh_q issue list -R "$GITHUB_REPO" --state all --limit 30 \
    --json number,title,url,labels,state --jq "$1" 2>/dev/null || echo ""
}
BASELINE="$(issue_field '[.[].number] | max // 0')"
BASELINE="${BASELINE:-0}"
log "GitHub baseline: highest existing issue number is #$BASELINE"

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
# A fresh per-run suffix makes the seed/answer request_ids unique to THIS run, so
# the demo can be re-run against the same space without the staff's posts deduping
# to a previous run's (old) messages the no-backfill bot would never re-detect.
SEED_SUFFIX="$(date +%H%M%S)-$$"
log "Starting staff persona '$PERSONA' — it will report the incident and answer the bot…"
"$PY" -u scripts/run_staff.py --persona "$PERSONA" --token "$STAFF_TOKEN" \
  --seed-suffix "$SEED_SUFFIX" >"$STAFF_LOG" 2>&1 &
STAFF_PID=$!
sleep 2
kill -0 "$STAFF_PID" 2>/dev/null || die "staff exited early — see $STAFF_LOG:
$(tail -n 30 "$STAFF_LOG")"
ok "staff is live (PID $STAFF_PID); incident seeded into the space"

# --- launch the noise control (benign chatter the bot must ignore) ---------
# `--once` seeds the small talk and exits; the persona holds no facts so it never
# answers. A distinct seed-suffix keeps it re-runnable alongside the incident.
if [ "$NOISE_ENABLED" -eq 1 ]; then
  log "Seeding the noise control — benign small talk that must NOT become an issue…"
  "$PY" -u scripts/run_staff.py --persona noise --token "$NOISE_TOKEN" --once \
    --seed-suffix "${SEED_SUFFIX}-noise" >"$NOISE_LOG" 2>&1 &
  NOISE_PID=$!
  ok "noise control seeded ($NOISE_COUNT message(s) as a second account)"
fi

# --- watch for the resolve → GitHub issue ----------------------------------
log "Watching for the bot to resolve the issue and file it to GitHub (timeout ${TIMEOUT}s)…"
log "  live bot log:   tail -f $POLLER_LOG"
log "  live staff log: tail -f $STAFF_LOG"

NEW_NUM=0
START=$SECONDS
LAST_NOTE=""
while [ $((SECONDS - START)) -lt "$TIMEOUT" ]; do
  # Surface resolve progress from the bot log as it happens (deduped).
  note="$(grep -hoE 'cycle [^(]*' "$POLLER_LOG" 2>/dev/null | tail -n 1 || true)"
  if [ -n "$note" ] && [ "$note" != "$LAST_NOTE" ]; then
    printf '      bot: %s\n' "$(echo "$note" | sed -E 's/[[:space:]]+$//')"
    LAST_NOTE="$note"
  fi

  # Server-side proof: a brand new issue number above the baseline.
  latest="$(issue_field '[.[].number] | max // 0')"; latest="${latest:-0}"
  if [ "$latest" -gt "$BASELINE" ]; then NEW_NUM="$latest"; break; fi

  # Bail fast if either process died.
  kill -0 "$POLLER_PID" 2>/dev/null || die "poller died mid-run — see $POLLER_LOG:
$(tail -n 30 "$POLLER_LOG")"
  kill -0 "$STAFF_PID"  2>/dev/null || warn "staff process exited (it may be done revealing facts)"
  sleep 4
done

[ "$NEW_NUM" -gt 0 ] || die "no new GitHub issue appeared within ${TIMEOUT}s.
Last bot log lines:
$(tail -n 20 "$POLLER_LOG")
Last staff log lines:
$(tail -n 10 "$STAFF_LOG")"

# --- report the result ------------------------------------------------------
ISSUE_JSON="$(gh_q issue view "$NEW_NUM" -R "$GITHUB_REPO" --json number,title,url,labels,body)"
TITLE="$(echo "$ISSUE_JSON" | jq -r '.title')"
URL="$(echo "$ISSUE_JSON" | jq -r '.url')"
LABELS="$(echo "$ISSUE_JSON" | jq -r '[.labels[].name] | join(", ")')"
HAS_TRANSCRIPT="$(echo "$ISSUE_JSON" | jq -r 'if (.body|test("## Collected messages")) then "yes" else "no" end')"

echo
log "✅ RESOLVED — GitHub issue filed to the private repo"
ok "issue:      #$NEW_NUM  $TITLE"
ok "url:        $URL"
ok "labels:     $LABELS"
ok "transcript: collected messages embedded in body = $HAS_TRANSCRIPT"

# Confirm the GitHub-export + voice-DM from the bot log (the bot logs both on success).
if grep -q "filed GitHub issue for" "$POLLER_LOG"; then
  ok "bot log:    $(grep -h 'filed GitHub issue for' "$POLLER_LOG" | tail -n 1)"
fi

# --- precision check: did the bot SEE the noise and still ignore it? --------
# A weak check ("0 issues from the noise") can pass for the WRONG reason — if the
# noise came from the bot's own account it is self-filtered and never judged. So
# we prove three things: the bot opened exactly the incident, it provably FETCHED
# the noise (it is in the bot's seen-id window), and it posted NOTHING into the
# noise thread (no engagement). scripts/verify_precision.py does the last two.
if [ "$NOISE_ENABLED" -eq 1 ]; then
  echo
  log "Precision check — did the bot SEE the $NOISE_COUNT non-issue message(s) and still ignore them?"
  # Give the cycle that resolved the incident a moment to also finish seeing the
  # noise thread, so a verdict isn't declared a beat before the bot would react.
  sleep 6
  # (1) Authoritative: the bot's OWN state. A resolved issue stays in `issues`,
  # so on a fresh session the count of distinct issues it opened == len(issues).
  OPENED="$("$PY" - <<'PY'
import json
try:
    d = json.load(open(".state/issues.json"))
    iss = d.get("issues", []) or []
    print(len(iss))
    for i in iss:
        print("  - %s [%s]" % ((i.get("title") or "(untitled)")[:70], i.get("status")))
except Exception as exc:  # noqa: BLE001
    print("ERR", exc)
PY
)"
  COUNT="$(printf '%s\n' "$OPENED" | head -1)"
  GH_NEW="$(issue_field "[.[] | select(.number > $BASELINE)] | length")"; GH_NEW="${GH_NEW:-?}"

  # (2) The noise thread + message ids the noise persona reported into NOISE_LOG.
  NOISE_THREAD="$(grep -m1 '^SEEDED_THREAD ' "$NOISE_LOG" 2>/dev/null | awk '{print $2}')"
  mapfile -t NOISE_MSGS < <(grep '^SEEDED_MSG ' "$NOISE_LOG" 2>/dev/null | awk '{print $2}')
  VERDICT="INCONCLUSIVE"; VERIFY_OUT=""
  if [ -n "$NOISE_THREAD" ] && [ "${#NOISE_MSGS[@]}" -gt 0 ]; then
    VARGS=(--noise-thread "$NOISE_THREAD" --state .state/issues.json --bot-token secrets/token_bot.json)
    for mid in "${NOISE_MSGS[@]}"; do VARGS+=(--noise-msg "$mid"); done
    VERIFY_OUT="$("$PY" scripts/verify_precision.py "${VARGS[@]}" 2>/dev/null || true)"
    VERDICT="$(printf '%s\n' "$VERIFY_OUT" | sed -n 's/^VERDICT //p' | tail -1)"; VERDICT="${VERDICT:-INCONCLUSIVE}"
  else
    warn "noise persona did not report its seeded ids — cannot prove the bot saw the noise."
  fi
  DELIVERED="$(printf '%s\n' "$VERIFY_OUT" | sed -n 's/^DELIVERED //p' | tail -1)"
  BOT_REPLIES="$(printf '%s\n' "$VERIFY_OUT" | sed -n 's/^BOT_REPLIES //p' | tail -1)"
  NOISE_SEEN="$(printf '%s\n' "$VERIFY_OUT" | sed -n 's/^NOISE_SEEN //p' | tail -1)"

  if [ "$COUNT" = "1" ] && [ "$VERDICT" = "PASS" ]; then
    ok "bot opened EXACTLY 1 issue (the incident); server-side: $GH_NEW new issue(s) above #$BASELINE"
    ok "noise from a NON-bot account: delivered ${DELIVERED:-?}, bot fetched ${NOISE_SEEN:-?}, bot replies in its thread ${BOT_REPLIES:-?}"
    ok "→ the bot SAW the small talk and ignored it by judgment ✅"
  elif [ "$VERDICT" = "REGRESSION" ] || { [ -n "$COUNT" ] && [ "$COUNT" != "1" ]; }; then
    warn "PRECISION REGRESSION — the bot engaged the noise:"
    printf '%s\n' "$OPENED" | tail -n +2 | while IFS= read -r ln; do warn "$ln"; done
    [ -n "${BOT_REPLIES:-}" ] && warn "bot posted ${BOT_REPLIES} message(s) into the noise thread"
    warn "opened ${COUNT:-?} issue(s); server-side: $GH_NEW new issue(s) above #$BASELINE"
  else
    ok "bot opened EXACTLY 1 issue (the incident); server-side: $GH_NEW new issue(s) above #$BASELINE"
    warn "could not POSITIVELY confirm the bot fetched the noise (delivered ${DELIVERED:-?}, seen ${NOISE_SEEN:-?}, bot replies ${BOT_REPLIES:-0}); the opened-issue proof still holds"
  fi
fi

echo
log "Voice report (audio + transcript) → DM"
# Voice runs on a background pool after the in-thread close; give it a few extra
# seconds to land, then read the bot's success log.
for _ in $(seq 1 8); do
  grep -q "posted voice report" "$POLLER_LOG" && break
  sleep 3
done
if grep -q "posted voice report" "$POLLER_LOG"; then
  ok "$(grep -h 'posted voice report' "$POLLER_LOG" | tail -n 1)"
  ok "check the DM space ${VOICE_SPACE:-<issue thread>} for the MP3 + spoken transcript"
elif grep -q "voice report delivery failed" "$POLLER_LOG"; then
  warn "voice delivery failed (the report fell back to disk under reports/):"
  warn "$(grep -h 'voice report delivery failed' "$POLLER_LOG" | tail -n 1)"
else
  warn "no voice confirmation yet — it may still be synthesizing; tail $POLLER_LOG"
fi

echo
log "Demo complete. Open the issue: $URL"
log "  • Chat space (clarification thread): $GOOGLE_SPACE"
log "  • Voice DM (audio + transcript):     ${VOICE_SPACE:-<issue thread>}"
[ "$NOISE_ENABLED" -eq 1 ] && log "  • Control case: bot ignored the small talk, filed only the incident"
exit 0
