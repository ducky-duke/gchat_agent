#!/usr/bin/env bash
#
# call_apigw.sh — start an AI VOICE CALL that reports the "API gateway timeout"
# incident (the `apigw` scenario: API gateway timing out, 504s in prod).
#
# Gemini Live is the CALLER and you are the callee on a real Google Chat call.
# On pickup the AI introduces itself as the incident-duty assistant and relays
# the apigw incident on behalf of the on-call engineer (Dave), answering the
# callee's questions strictly from the scenario's facts. It is a thin wrapper
# around `call/gemini_call.py --persona apigw` (which uses the _INCIDENT_SYSTEM_*
# prompt, NOT the generic DEFAULT_SYSTEM).
#
# Usage:
#   ./call_apigw.sh                  # call Duc in English (the defaults)
#   ./call_apigw.sh --language vi    # report in Vietnamese (speaks vi-VN)
#   ./call_apigw.sh --callee Bob     # address a different callee
#   ./call_apigw.sh --duration 240   # longer call (default 180s)
# Every extra flag is passed straight through to call/gemini_call.py
# (--voice, --model, --no-greet, --quit-browser, --diag-pickup, ...).
#
# Prerequisites (demo machine only — this automates the Google UI, ToS risk):
#   * GEMINI_API_KEY in .env or the environment (the gate; distinct from
#     OPENROUTER_API_KEY).
#   * The dedicated caller Brave profile (.browser-profile-caller) signed in as
#     the bot account. First run, gemini_call.py prints the one-time sign-in cmd.
#   * A VISIBLE desktop session — native Wayland suspends an occluded renderer,
#     which drops the call. Keep the caller window on screen.
#
# Override the interpreter if your conda env lives elsewhere:
#   IGAMING_PYTHON=/path/to/python ./call_apigw.sh
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

# Everything the caller passes is forwarded after the fixed --persona apigw.
ARGS=("$@")

# Preflight the one gate that otherwise fails late: the Gemini key. Accept it
# from the environment OR a non-empty, non-comment line in .env (mirrors
# gemini_voice.load_gemini_key, which also reads GOOGLE_API_KEY as a fallback).
if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${GOOGLE_API_KEY:-}" ] \
   && ! grep -qE '^[[:space:]]*(GEMINI_API_KEY|GOOGLE_API_KEY)[[:space:]]*=[[:space:]]*[^[:space:]#]' .env 2>/dev/null; then
  echo "ERROR: no GEMINI_API_KEY found (checked env + .env)." >&2
  echo "  Set GEMINI_API_KEY in .env or export it, then re-run." >&2
  exit 2
fi

echo "→ calling about the API gateway timeout incident (apigw). Keep the caller"
echo "  window VISIBLE; press Ctrl-C to hang up."

# Prefer the env's Python directly: no `conda run` (which buffers stdout and
# needs conda on PATH) and works even when launched as ./call_apigw.sh.
PY="${IGAMING_PYTHON:-$HOME/miniconda3/envs/igaming/bin/python}"
if [ -x "$PY" ]; then
  exec "$PY" -u call/gemini_call.py --persona apigw ${ARGS[@]+"${ARGS[@]}"}
fi

# Fallback: go through conda, sourcing it first if it isn't already on PATH.
if ! command -v conda >/dev/null 2>&1; then
  for base in "$HOME/miniconda3" "$HOME/anaconda3" /opt/conda; do
    if [ -f "$base/etc/profile.d/conda.sh" ]; then
      # shellcheck disable=SC1091
      source "$base/etc/profile.d/conda.sh"
      break
    fi
  done
fi
exec conda run --no-capture-output -n igaming python -u call/gemini_call.py --persona apigw ${ARGS[@]+"${ARGS[@]}"}
