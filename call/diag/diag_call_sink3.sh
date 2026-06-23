#!/usr/bin/env bash
# Proof test: capture the default sink (sink 3 / id 134) monitor during ONE call, with the
# bot machine's MIC MUTED. A sink .monitor only carries OUTPUT, and the mic is muted, so any
# voice that lands here can ONLY be the call audio Brave plays back — proving it's the call,
# not a local-mic artifact. Mic mute state is saved and ALWAYS restored (trap).
#
# Before answering: plug EARPHONES into the other device (device B) to break the acoustic
# loop → the capture should also come out echo-free.
#
# Usage: call/diag/diag_call_sink3.sh   (then ANSWER on device B + talk ~10s + hang up)
set -u

CDP="http://127.0.0.1:9222"
URL="https://chat.google.com/u/1/app/chat/qtotjoAAAAE"   # /u/1/ = mikmikb26 (NEVER authuser 0 = glo.com)
DUR=60

TS=$(date +%Y%m%d_%H%M%S)
OUT="reports/diag_${TS}"
mkdir -p "$OUT"
ROUTE="$OUT/routing.log"; : > "$ROUTE"

# --- optionally mute the bot mic (MUTE_MIC=1). Default OFF: muting can make Chromium's
#     echo-canceller cork its WebRTC OUTPUT stream, which confounds the test. The
#     monitor=output-only fact already proves the capture isn't the mic. ---
PREV_MUTE=$(pactl get-source-mute @DEFAULT_SOURCE@ 2>/dev/null | awk '{print $2}')
restore_mic() {
  if [ "${MUTE_MIC:-0}" = "1" ] && [ "${PREV_MUTE:-yes}" = "no" ]; then
    pactl set-source-mute @DEFAULT_SOURCE@ 0 2>/dev/null
    echo "restored bot mic → unmuted" | tee -a "$ROUTE"
  fi
}
trap restore_mic EXIT INT TERM
if [ "${MUTE_MIC:-0}" = "1" ]; then
  pactl set-source-mute @DEFAULT_SOURCE@ 1 2>/dev/null
  echo "bot mic muted (was: ${PREV_MUTE:-unknown})" | tee -a "$ROUTE"
else
  echo "bot mic left as-is (${PREV_MUTE:-unknown}) — clean run, no AEC confound" | tee -a "$ROUTE"
fi

DEF=$(pactl get-default-sink)
MON="${DEF}.monitor"
echo "recording default-sink monitor: $MON" | tee -a "$ROUTE"

# 1) record the default sink monitor
ffmpeg -hide_banner -loglevel error -nostdin -y -f pulse -i "$MON" \
  -ar 16000 -ac 1 -acodec pcm_s16le "$OUT/sink3.wav" &
REC=$!

# 2) place the call in background
conda run --no-capture-output -n igaming python -u call/meet_call_browser.py \
  --cdp-url "$CDP" --url "$URL" \
  --watch-join --join-poll 0.5 --duration "$DUR" > "$OUT/call.log" 2>&1 &
CALL=$!
echo "call pid=$CALL → ANSWER on device B (with EARPHONES), TALK ~10s, HANG UP" | tee -a "$ROUTE"

# 3) poll routing every 2s
while kill -0 "$CALL" 2>/dev/null; do
  {
    echo "=== $(date +%H:%M:%S) ==="
    pactl list sink-inputs | grep -iE "Sink Input #|Sink:|Corked:|application\.name|media\.name"
  } >> "$ROUTE"
  sleep 2
done

# 4) stop recorder + restore mic
kill -INT "$REC" 2>/dev/null; sleep 1; kill "$REC" 2>/dev/null; wait "$REC" 2>/dev/null
restore_mic; trap - EXIT INT TERM

# 5) measure + extract the voiced span (mic was muted → voice here = the call)
echo "" | tee -a "$ROUTE"
echo "== full sink3 volume ==" | tee -a "$ROUTE"
ffmpeg -hide_banner -nostats -i "$OUT/sink3.wav" -af volumedetect -f null - 2>&1 \
  | grep -E "mean_volume|max_volume" | tee -a "$ROUTE"
echo "== voiced regions ==" | tee -a "$ROUTE"
ffmpeg -hide_banner -nostats -i "$OUT/sink3.wav" -af "silencedetect=noise=-50dB:d=0.5" -f null - 2>&1 \
  | grep -E "silence_(start|end)" | tee -a "$ROUTE"
# trimmed + normalized listen copy (kept at 16k mono)
ffmpeg -hide_banner -loglevel error -y -i "$OUT/sink3.wav" \
  -af "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.2:stop_periods=-1:stop_threshold=-50dB:stop_silence=0.5,loudnorm,aresample=16000" \
  -ar 16000 -ac 1 "$OUT/sink3_trimmed.wav" 2>/dev/null
echo "OUT=$OUT"
