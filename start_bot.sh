#!/usr/bin/env bash
#
# start_bot.sh — start the Google Chat issue-spotter bot (the poller).
#
# Reads the configured space (GOOGLE_SPACE in .env), detects issues, asks
# clarifying questions until each is clear, and writes resolution reports to
# reports/. Loops forever until you press Ctrl-C.
#
# Usage:
#   ./start_bot.sh            # poll forever (the normal demo)
#   ./start_bot.sh --once     # run a single poll cycle, then exit
#
# Override the interpreter if your conda env lives elsewhere:
#   IGAMING_PYTHON=/path/to/python ./start_bot.sh
#
set -euo pipefail

# Always run from the repo root so .env, secrets/ and scripts/ resolve, no
# matter where the script is invoked from.
cd "$(dirname "${BASH_SOURCE[0]}")"

# Prefer the env's Python directly: no `conda run` (which buffers stdout and
# needs conda on PATH) and works even when launched as ./start_bot.sh.
PY="${IGAMING_PYTHON:-$HOME/miniconda3/envs/igaming/bin/python}"
if [ -x "$PY" ]; then
  exec "$PY" -u scripts/run_poller.py "$@"
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
exec conda run --no-capture-output -n igaming python -u scripts/run_poller.py "$@"
