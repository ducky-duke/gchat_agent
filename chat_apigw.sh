#!/usr/bin/env bash
#
# chat_apigw.sh — CHAT with the AI about the "API gateway timeout" incident in your
# report DM, and ask it to CALL you back by just texting.
#
# This is the conversational front-end to ./call_apigw.sh. Instead of a call that
# rings once and leaves the process hanging when you miss it, you keep a chat open
# in GOOGLE_CHAT_REPORT_SPACE: ask the AI about the incident (it answers from the
# apigw scenario facts), and text "call me" / "gọi lại" to make it place the real
# outbound voice call (it spawns ./call_apigw.sh). If a call is MISSED it offers to
# ring you again.
#
# Usage:
#   ./chat_apigw.sh                     # chat about apigw; call-back speaks English
#   ./chat_apigw.sh --language vi       # call-back speaks Vietnamese
#   ./chat_apigw.sh --persona ops       # chat about a different scenario
#   ./chat_apigw.sh --once              # one poll cycle, then exit
#
# It services the DM in GOOGLE_CHAT_REPORT_SPACE (.env). The CHAT uses the
# configured LLM (LLM_PROVIDER); the CALL additionally needs GEMINI_API_KEY + the
# demo caller browser (see ./call_apigw.sh) — without a key the chat still works and
# a call-back politely declines.
#
# ⚠️  Do NOT also run the poller's REPORT_ASSISTANT (./start_bot.sh with
# REPORT_ASSISTANT=true) pointed at the SAME DM — both would answer every message.
# This is the manual/demo alternative to that always-on path. Every extra flag is
# passed straight through to scripts/apigw_chat.py.
#
# Override the interpreter if your conda env lives elsewhere:
#   IGAMING_PYTHON=/path/to/python ./chat_apigw.sh
#
set -euo pipefail

# Always run from the repo root so .env, secrets/ and call/ resolve no matter
# where the script is invoked from.
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  # Print the header comment block (skip the shebang; stop at the first code line).
  awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "${BASH_SOURCE[0]}"
  exit 0
fi

ARGS=("$@")

echo "→ chatting about the API gateway timeout incident (apigw) in your report DM."
echo "  Ask about it, or text 'call me' / 'gọi lại' to have it call you. Ctrl-C to stop."

# Prefer the env's Python directly (no `conda run` buffering); fall back to conda.
PY="${IGAMING_PYTHON:-$HOME/miniconda3/envs/igaming/bin/python}"
if [ -x "$PY" ]; then
  exec "$PY" -u scripts/apigw_chat.py ${ARGS[@]+"${ARGS[@]}"}
fi

if ! command -v conda >/dev/null 2>&1; then
  for base in "$HOME/miniconda3" "$HOME/anaconda3" /opt/conda; do
    if [ -f "$base/etc/profile.d/conda.sh" ]; then
      # shellcheck disable=SC1091
      source "$base/etc/profile.d/conda.sh"
      break
    fi
  done
fi
exec conda run --no-capture-output -n igaming python -u scripts/apigw_chat.py ${ARGS[@]+"${ARGS[@]}"}
