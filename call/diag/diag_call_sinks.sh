#!/usr/bin/env bash
# Diagnostic: during ONE live call, record EVERY sink's monitor in parallel AND poll the
# sink-input routing, to pinpoint exactly which sink (or audio path) Chromium plays the
# REMOTE call audio to. Monitor-mode capture grabbed "another tab" instead of the call,
# so we stop guessing the sink and instead record them all + see the routing live.
#
# Output: reports/diag_<ts>/sink_<i>.wav (one per sink) + routing.log (per-2s snapshot of
# sink states + sink-inputs with their Sink:/media.name/role). After the call, each WAV's
# volume is measured — the one with real speech during the talk window IS the call's sink.
#
# Usage: call/diag/diag_call_sinks.sh   (then ANSWER on the other device + talk ~10s + hang up)
set -u

CDP="http://127.0.0.1:9222"
URL="https://chat.google.com/u/1/app/chat/qtotjoAAAAE"   # /u/1/ = mikmikb26 (NEVER authuser 0 = glo.com, revoked)
DUR=150

TS=$(date +%Y%m%d_%H%M%S)
OUT="reports/diag_${TS}"
mkdir -p "$OUT"
ROUTE="$OUT/routing.log"
: > "$ROUTE"

echo "=== sink-monitor diagnostic call → $OUT ===" | tee -a "$ROUTE"

# 1) one ffmpeg recorder per sink monitor (label by index; names logged for mapping)
mapfile -t SINKS < <(pactl list short sinks | awk '{print $2}')
declare -a PIDS
i=0
for s in "${SINKS[@]}"; do
  mon="${s}.monitor"
  ffmpeg -hide_banner -loglevel error -nostdin -y -f pulse -i "$mon" \
    -ar 16000 -ac 1 -acodec pcm_s16le "$OUT/sink_${i}.wav" &
  PIDS+=($!)
  echo "rec sink_${i}.wav  ←  $mon" | tee -a "$ROUTE"
  i=$((i+1))
done

# 2) place the call in the background (just connects + holds; no built-in capture)
conda run --no-capture-output -n igaming python -u call/meet_call_browser.py \
  --cdp-url "$CDP" --url "$URL" \
  --watch-join --join-poll 0.5 --duration "$DUR" > "$OUT/call.log" 2>&1 &
CALL=$!
echo "call pid=$CALL  → ANSWER on the other device, TALK continuously ~10s, then HANG UP" | tee -a "$ROUTE"

# 3) poll sink-input routing every 0.5s while the call runs — tight enough to catch
#    the brief playback stream the call creates only while the remote is speaking.
#    Log: timestamp, sink-input id, which Sink it's on, Corked state, app/media name.
SI_LOG="$OUT/sink_inputs.log"; : > "$SI_LOG"
while kill -0 "$CALL" 2>/dev/null; do
  ts=$(date +%H:%M:%S.%N | cut -c1-12)
  n=$(pactl list short sink-inputs | wc -l)
  if [ "$n" -gt 0 ]; then
    pactl list sink-inputs \
      | grep -iE "Sink Input #|Sink:|Corked:|application\.name|media\.name" \
      | sed "s/^/[$ts] /" >> "$SI_LOG"
  fi
  sleep 0.5
done

# 4) stop the recorders (SIGINT → ffmpeg finalizes the WAV header)
for p in "${PIDS[@]}"; do kill -INT "$p" 2>/dev/null; done
sleep 1
for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
wait 2>/dev/null

# 5) measure each WAV → the loud one is the call's sink
echo "" | tee -a "$ROUTE"
echo "=== RESULTS (silence ≈ -91 dB; real audio ≫ that) ===" | tee -a "$ROUTE"
i=0
for s in "${SINKS[@]}"; do
  w="$OUT/sink_${i}.wav"
  if [ -f "$w" ]; then
    vol=$(ffmpeg -hide_banner -nostats -i "$w" -af volumedetect -f null - 2>&1 \
          | grep -E "mean_volume|max_volume" | tr '\n' ' ')
    echo "sink_${i}  ${s}.monitor  →  ${vol}" | tee -a "$ROUTE"
    # voiced spans: where (if anywhere) real speech landed on this sink
    voiced=$(ffmpeg -hide_banner -nostats -i "$w" -af "silencedetect=noise=-50dB:d=0.4" -f null - 2>&1 \
             | grep -cE "silence_end")
    echo "         voiced segments (>-50dB): $voiced" | tee -a "$ROUTE"
  fi
  i=$((i+1))
done

# 6) which sinks ever carried the call's playback stream (from the 0.5s routing poll)?
echo "" | tee -a "$ROUTE"
echo "=== sink-inputs seen during call (where the playback stream lived) ===" | tee -a "$ROUTE"
if [ -s "$SI_LOG" ]; then
  grep -iE "Sink:" "$SI_LOG" | sed -E 's/\[[0-9:.]+\] //' | sort | uniq -c | tee -a "$ROUTE"
  echo "  (total poll lines mentioning a sink-input: $(grep -c "Sink Input #" "$SI_LOG"))" | tee -a "$ROUTE"
else
  echo "  NONE — the call never produced a visible playback sink-input." | tee -a "$ROUTE"
fi

# 7) hang-up fix check: did the call script self-stop on hang-up, or hit the cap?
echo "" | tee -a "$ROUTE"
echo "=== call script end reason (hang-up fix) ===" | tee -a "$ROUTE"
grep -E "\[end\]|\[end-dbg\]|frame closed|call ended|hung up|survey|Reached the .* cap|REMOTE JOINED|📴" "$OUT/call.log" \
  | tee -a "$ROUTE"

echo "" | tee -a "$ROUTE"
echo "routing log: $ROUTE"
echo "sink-input log: $SI_LOG"
echo "OUT=$OUT"
