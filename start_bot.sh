#!/usr/bin/env bash
#
# start_bot.sh — start the Google Chat issue-spotter bot (the poller).
#
# Reads the configured space (GOOGLE_SPACE in .env), detects issues, asks
# clarifying questions until each is clear, and writes resolution reports to
# reports/. Loops forever until you press Ctrl-C.
#
# Usage:
#   ./start_bot.sh            # fresh start: reset previous-session state, then poll
#   ./start_bot.sh --once     # one fresh poll cycle, then exit
#   ./start_bot.sh --continue # keep previous-session state (resume, no reset)
#
# A fresh start is the DEFAULT — every launch resets ONLY genuine previous-
# session state before starting:
#   * .state/  — the IssueStore (poll cursor + tracked issues) is deleted, so
#                the bot starts a clean session. NOTE: this also resets the poll
#                cursor, so on a live Space the bot re-scans history from the top.
#   * reports/ — past reports are ARCHIVED (moved) to reports/_archive/<ts>/,
#                never deleted. voice_report_sample.mp3 and .gitkeep stay put.
# It deliberately does NOT touch data/ — knowledge_base/ + scenarios.json are
# the RAG corpus / input, not session state. There are no on-disk logs or RAG
# index to clear either: logs go to stdout, and the RAG index is rebuilt in
# memory from data/knowledge_base/ on every start.
#
# Pass --continue to SKIP the reset and resume the previous session (keeps
# .state/ and reports/). Combinable with other flags, e.g.
# ./start_bot.sh --continue --once. (--fresh is also accepted as an explicit
# no-op, since fresh is already the default.)
#
# Override the interpreter if your conda env lives elsewhere:
#   IGAMING_PYTHON=/path/to/python ./start_bot.sh
#
set -euo pipefail

# Always run from the repo root so .env, secrets/ and scripts/ resolve, no
# matter where the script is invoked from.
cd "$(dirname "${BASH_SOURCE[0]}")"

# Parse our own flags (consumed here); everything else passes to run_poller.py.
# Fresh start is the default; --continue opts out of the reset. --fresh is an
# accepted no-op (fresh is already default) so it never leaks to the poller.
CONTINUE=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --continue) CONTINUE=1 ;;
    --fresh) : ;;
    *) ARGS+=("$arg") ;;
  esac
done

# Fresh start (the default): reset previous-session state (see header comment).
if [ "$CONTINUE" -eq 0 ]; then
  echo "start_bot: fresh start → resetting previous-session state (use --continue to resume)" >&2

  # 1) IssueStore (poll cursor + tracked issues). Regenerated on first poll.
  rm -rf .state

  # 2) Archive (don't delete) past reports, keeping the sample + .gitkeep.
  #    Dotfiles like .gitkeep are not matched by reports/* so they survive.
  if [ -d reports ]; then
    ts="$(date +%Y%m%d-%H%M%S)"
    archived=0
    for entry in reports/*; do
      [ -e "$entry" ] || continue   # empty dir → glob stays literal, skip
      case "$(basename "$entry")" in
        voice_report_sample.mp3|_archive) continue ;;
      esac
      if [ "$archived" -eq 0 ]; then
        mkdir -p "reports/_archive/$ts"
        archived=1
      fi
      mv "$entry" "reports/_archive/$ts/"
    done
    if [ "$archived" -eq 1 ]; then
      echo "start_bot: archived old reports → reports/_archive/$ts/" >&2
    fi
  fi
fi

# Prefer the env's Python directly: no `conda run` (which buffers stdout and
# needs conda on PATH) and works even when launched as ./start_bot.sh.
PY="${IGAMING_PYTHON:-$HOME/miniconda3/envs/igaming/bin/python}"
if [ -x "$PY" ]; then
  exec "$PY" -u scripts/run_poller.py ${ARGS[@]+"${ARGS[@]}"}
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
exec conda run --no-capture-output -n igaming python -u scripts/run_poller.py ${ARGS[@]+"${ARGS[@]}"}
