# call/ — the outbound voice-call + Meet-link subsystem

The voice/telephony subsystem, lifted out of `scripts/` because it has outgrown a "script": ~22
files, a real internal module split, heavy non-core deps, and its own war-story record. The
agent-ops entry points (poller, staff, verifiers, OAuth, offline demo) stay in
[`../scripts/`](../scripts/CLAUDE.md). Deep war-story / dead-end / proven-recipe detail for
everything here lives in [`../docs/CALL_AUTOMATION.md`](../docs/CALL_AUTOMATION.md) — entries
below stay lean and point there; read it before debugging a call.

## Architecture / conventions (read before moving or importing anything here)
- **Subprocess boundary, not an import.** The runner NEVER imports this code — it spawns
  `call/gemini_call.py` as a detached subprocess (`config.CALL_SCRIPT`, `runner._maybe_place_call`).
  That decoupling is why the heavy deps below can live here without touching the core.
- **Heavy deps are quarantined here.** `playwright`, `google-genai`, `pyaudio` are imported ONLY
  in this subtree — the `src/gchat_agent/` core stays pure-stdlib (+ lazy `openai`). Don't add an
  import of anything under `call/` into the package.
- **Flat-dir scripts, NOT a Python package.** Modules import each other as flat siblings
  (`import meet_call_browser`), relying on each entry script self-adding its own dir to `sys.path`
  (`sys.path.insert(0, _THIS_DIR)`) — there is **no `__init__.py`** and the imports are NOT
  package-relative. Keep it that way: all mutually-importing modules must stay co-located in
  `call/` (one `sys.path` entry resolves them). Run a tool directly: `python call/gemini_call.py …`.
- **`_REPO_ROOT = dirname(_THIS_DIR)`** in every module → repo root (used for `data/scenarios.json`,
  `.env`, `logs/`). Valid because `call/` is one level under the repo root. Files in `call/diag/`
  are two levels down, so they compute `_CALL_DIR`/`_REPO_ROOT` explicitly (see
  `diag/call_network_capture.py`).
- **Import graph (the coupled cluster — must stay co-located):** `gemini_call` →
  `ai_call`, `gemini_voice`, `meet_call_browser`; `meet_call_browser` (facade) →
  `meet_call_{js,run,setup,signals}`; `meet_call_run` → `meet_audio_{capture,inject}`,
  `meet_rest_watch`, `meet_call_{js,setup,signals}`; `diag/call_network_capture` → `meet_call_browser`.

## Meet/Call LINKS — REST + local Gemini Live (no UI automation)
None of these makes the AI *speak* on a Google call — a hard ceiling (Meet Media API is
receive-only; see [`../docs/CLAUDE.md`](../docs/CLAUDE.md) `google_meet/`). They mint/share a join
link or run a local voice loop.
- **`demo_meet_call.py`** — the **issue bot** (`--token`, default `token_bot`) mints a REAL Meet
  link (Meet REST `spaces.create`) for the `apigw` incident and **DMs** the briefing + join link to
  the human stakeholder. Target precedence: `--space` > bot↔recipient DM (`GOOGLE_VOICE_SPACE`) >
  `GOOGLE_SPACE`. Flags `--persona`, `--token`, `--callee`, `--space`, `--dry-run`, `--message`.
  Token MUST carry the `…/auth/meetings.space.created` scope (re-run `../scripts/authorize.py` for
  an older token) AND the Meet REST API must be **enabled**
  (`SERVICE_DISABLED` 403 → `gcloud services enable meet.googleapis.com`).
- **`make_call.py`** — the **minimal "make a phone call"** utility (stripped-down sibling of
  `demo_meet_call.py`, no incident text). Run AS THE BOT (`--token`, default `token_bot`) it mints a
  Meet link and DMs the callee "calling you" + the link. Default route bot → Duc in their DM
  (`GOOGLE_VOICE_SPACE`). Flags `--to`, `--token`, `--space`, `--message`, `--dry-run`. Each run
  mints a fresh meeting (never deduped). Same prereqs as above.
- **`demo_incident_call.py`** — a **Gemini Live API "phone call"**: the AI plays the on-call
  engineer from a `data/scenarios.json` persona (default `apigw` / INFRA-2207), opens with a spoken
  briefing, answers live. Real-time bidirectional VOICE over `google-genai` (Live API), built from
  [`../docs/gemini_live/`](../docs/gemini_live). Modes: default **voice** (mic+speaker, barge-in;
  needs `pyaudio`); **`--text`** (transcript-only if `pyaudio` absent → only needs `google-genai`).
  `--announce` posts a one-line heads-up to `GOOGLE_SPACE`. Auth: **`GEMINI_API_KEY`** (Google AI
  Studio, distinct from `OPENROUTER_API_KEY`). Live-API facts: on `gemini-3.1-flash-live-preview`,
  live text MUST use `session.send_realtime_input(text=...)`; audio is 16 kHz in / 24 kHz out PCM;
  the stdin reader is a daemon thread (instant Ctrl+C teardown). One-time deps:
  `conda run -n igaming pip install google-genai pyaudio` (+ system PortAudio).
- **`meet_rest_watch.py`** — the **REST room-data** half: given `--meeting-code abc-mnop-xyz` or
  `--auto` (caller's active conference), polls Meet REST v2 (`conferenceRecords` + `participants`,
  filter `latest_end_time IS NULL`) and prints the live roster, reporting a remote LEAVE.
  Organizer-token only (caller = bot; default `secrets/token_bot.json`); needs
  `meetings.space.created`/`…readonly`. Core loop is the reusable `watch(...)` (urllib+OAuth, no
  Playwright) used by `meet_call_browser --watch-rest`. ⚠️ conferenceRecords are POST-HOC with
  propagation lag — NOT real-time; the Chat-1:1→REST mapping gotcha is in
  [`../docs/CALL_AUTOMATION.md`](../docs/CALL_AUTOMATION.md).

## Native RINGING call (browser automation) + AI voice
The "trick" — no API can ring; only the Chat UI's call button does. ⚠️ Automates Google's UI
(ToS-violating, brittle selectors, account-flag risk) — **demo accounts only**. Account facts,
session dead-ends, proven CDP recipe, the join/hang-up/audio investigations, and the
Wayland-occlusion blocker are all in [`../docs/CALL_AUTOMATION.md`](../docs/CALL_AUTOMATION.md).
- **`meet_call_browser.py`** — drives the system Brave with Playwright (Python pkg only, system
  browser reused — NO `playwright install`) in a persistent profile (`--profile-dir`, default
  `.browser-profile/`, gitignored) to open a Chat DM, click the call button, and **ring** the
  callee. Now a thin **facade** re-exporting the public surface; implementation is split across
  `meet_call_js.py` / `meet_call_signals.py` / `meet_call_setup.py` / `meet_call_run.py` (acyclic:
  js ← signals ← setup; all ← run). **Edit the module, not the facade.** Key flags: `--cdp-url`
  (attach to a daily Brave on `--remote-debugging-port`), `--authuser`
  (⚠️ **1 = mikmikb26 = bot; 0 = glo.com REVOKED**), `--url`/`--space`, `--button-name`, `--dry-run`
  (DUMP all visible button labels), `--duration`/`--keep-open`, `--watch-join` (real-time join
  detection — 3 signals incl. the WebRTC counter that survives a backgrounded tab), `--watch-rest`,
  `--capture-audio`, `--inject-audio`, `--ensure-mic-on`, `--diag-pickup`.
- **`ai_call.py`** — the **minimal AI-mouth call**: launches a DEDICATED caller Brave
  (`.browser-profile-caller`, port 9333) with `--use-fake-ui-for-media-stream` (mic auto-granted,
  getUserMedia binds to ai_mic), then delegates ring+join+inject to `meet_call_browser.main` over
  CDP. **PROVEN + user-confirmed 2026-06-20** (callee heard the tone, no allow click, no move-dance).
  Flags `--audio FILE` (default test tone), `--duration`, `--at-join`, `--once`, `--url`, `--port`,
  `--profile`, `--login-wait`, `--quit-browser` (stop via /proc scan — never `pkill -f <profile>`,
  self-matches). First run needs the dedicated profile signed in as mikmikb26 (script prints the
  command). Keep the window VISIBLE (Wayland suspends an occluded renderer → drop).
- **`gemini_voice.py`** — the **bidirectional Gemini Live ⇄ call audio bridge** (the AI gets a
  MOUTH + an EAR). `GeminiVoiceBridge` sets up two virtual PulseAudio devices and runs the Gemini
  Live session in a worker thread; greeting is gated so the AI speaks FIRST. Helpers
  `load_gemini_key` / `build_live_config`. Standalone: `--devices-test` / `--selftest`. Needs
  `GEMINI_API_KEY` + `google-genai`. Audio-graph + greeting-hardening detail in
  [`../docs/CALL_AUTOMATION.md`](../docs/CALL_AUTOMATION.md).
- **`gemini_call.py`** — the **orchestrator** (this is what `CALL_ON_RESOLVE` spawns): Gemini Live
  is the CALLER, you're the callee, on a real Chat call. Composes `ai_call` +
  `gemini_voice.GeminiVoiceBridge` + `meet_call_browser.main(..., on_join=, on_pickup=)` over CDP
  with `--watch-join --ensure-mic-on`. **`--persona apigw` = incident-report mode**: on pickup the
  AI reports a scenario incident as a NEUTRAL INTERMEDIARY relaying on behalf of the owner
  (**Dave** for apigw) — it is NOT that engineer, so "who's responsible?" → Dave; answers strictly
  from the report, else says it doesn't know. **Two prompt versions** (`build_incident_persona` /
  `_INCIDENT_PROMPTS`): **English (default)** or **Vietnamese** via `--language vi` — the choice
  sets BOTH the briefing wording AND the spoken language (speech `language_code` pinned to
  `en-US`/`vi-VN`). **`--incident-file <json>`** is the bot-driven counterpart to `--persona`: same
  call behavior, but the facts come from a JSON incident the bot wrote (`runner.build_call_incident`)
  for a REAL resolved issue instead of scenarios.json (`build_incident_persona_from_file`;
  `--persona` wins if both are passed). Flags `--duration`(180; well under Gemini's 15-min
  audio-only session cap — hardcoded default, not a config knob), `--persona`, `--incident-file`,
  `--callee`(Duc), `--url`, `--port`, `--profile`, `--model`, `--voice`(Aoede),
  `--system`/`--system-file`, `--language`(en|vi), `--no-greet`, `--no-record`, `--quit-browser`,
  `--diag-pickup`. Run: `conda run --no-capture-output -n igaming python -u call/gemini_call.py
  [--persona apigw --callee Duc]`. Keep the window VISIBLE; callee should use a headset (AEC).
- **`auto_answer.py`** — the unattended CALLEE: drives a 2nd Brave (separate
  `--remote-debugging-port`, `.browser-profile-callee`, signed in as Duc) over CDP, navigates to the
  DM, and on a ring clicks **answer** then turns mic+camera ON so media flows for the caller's
  capture; auto-**leaves** after a hold so hang-up detection fires. Matches the VI labels (answer
  `Trả lời cuộc gọi`, leave `Rời khỏi cuộc gọi`, etc.), skips disabled decoys. Prints `ANSWERED <t>`
  / `LEFT`. Needs the callee Brave launched with `--use-fake-{ui,device}-for-media-stream` + the
  anti-occlusion flags.
- **`meet_audio_inject.py`** — the **"AI mouth"** engine: `AudioInjector` builds a virtual mic
  (`module-null-sink ai_mic_sink` + `module-remap-source ai_mic`), makes it the default capture
  source, and plays a file (or a generated test tone) into it with ffmpeg so the CALLEE hears it.
  Fully reversible (atexit-guarded). Used by `meet_call_browser --inject-audio` + `ai_call.py`.
  Standalone proof: `--verify`.
- **`meet_audio_capture.py`** (+ `meet_call_browser --capture-audio`) — the **"AI ear"**: records
  the REMOTE voice the bot hears to a WAV (**16 kHz mono s16le PCM** = Gemini Live's input format).
  `--audio-mode` (default `allsinks`): **`allsinks`** ✅ records every sink monitor + mixes at stop;
  `monitor` records one sink (can miss the voice); `webrtc` ⚠️ DISQUALIFIED on Meet; `isolate`
  fragile. `--audio-out`, `--capture-from-ring`. Prints `CALL_AUDIO <path>`. Standalone:
  `--selftest`. Mode autopsy + the 5-call investigation in
  [`../docs/CALL_AUTOMATION.md`](../docs/CALL_AUTOMATION.md).
- **`huddle_watch.py`** — **clean native-call hang-up detection via Chat REST**: a DM call posts a
  message whose `meetSpaceLinkData.huddleStatus` is the call lifecycle — `MISSED`/`ENDED` are
  terminal. Needs only the bot's `chat.messages.readonly` scope; a supported pollable API (the Meet
  REST + browser-network channels are both dead ends for a Chat 1:1 call). ⚠️ Server-updated
  annotation → ENDED lags the real hang-up by seconds.

## diag/ — diagnostics + shell self-tests (dead-ends + ground-truth probes)
- **`diag/call_network_capture.py`** — diagnostic: places a ringing call (reuses
  `meet_call_browser`) and records all HTTP/WS to JSONL (live-flushed) with the DOM end-of-call as
  ground-truth marker, to discover WHICH network event fires the hang-up. Established that the clean
  roster-leave frame is inside an unreadable server-stream → use `huddle_watch.py` instead.
- **`diag/diag_call_join.py`** — the diagnostic that DISCOVERED the join signal: places a CDP call,
  hooks `RTCPeerConnection` (remote-track count) + probes the roster DOM each second through pickup,
  logging to `/tmp/call_join.log`. Re-run to re-verify if Meet's DOM drifts.
- **`diag/diag_call_dom.py`** — sibling diagnostic for hang-up: places a CDP call and logs the full
  call state every second.
- **`diag/diag_call_sink3.sh`** — proof test: capture the default sink's `.monitor` during ONE call
  with the MIC MUTED, so any voice captured can ONLY be the call audio. Mic state saved+restored
  (trap). Answer + talk ~10s + hang up on the other device.
- **`diag/diag_call_sinks.sh`** — diagnostic: during ONE call, record EVERY sink monitor in parallel
  AND poll the sink-input routing → pinpoints which sink Chromium plays the remote call audio to.
  Output `reports/diag_<ts>/sink_<i>.wav` + `routing.log`.
- **`diag/selftest_call.sh`** — one-command HANDS-OFF self-test of the whole call loop. By DEFAULT
  runs the caller under **Xvfb** (virtual display → renderer never occluded → fully unattended).
  One-time prereq: sign `.browser-profile-caller` in as mikmikb26 (script prints the command).
  Starts the caller (`call/meet_call_browser.py --watch-join --capture-audio`) ringing
  mikmikb26→Duc + `call/auto_answer.py`, then reports two verdicts — HANG-UP DETECTION and AUDIO
  CAPTURE (volumedetect > −80 dB). Flags `--diag`, `--caller-real`, `--caller-headed`,
  `--caller-xwayland`, `--no-callee`, `--caller-profile`, `--answer-seconds`. ⚠️ Kill stuck braves
  by PID (`/proc` scan), never `pkill -f <profile>` (self-matches → kills the shell). Caller-mode
  media-stability reality in [`../docs/CALL_AUTOMATION.md`](../docs/CALL_AUTOMATION.md).
