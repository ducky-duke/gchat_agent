# scripts/ — entry points

Each script self-adds `src/` to `sys.path`, so run them directly (no install). Run flow
details + `./start_bot.sh` wrapper live in the root [`CLAUDE.md`](../CLAUDE.md).

- **`run_poller.py`** — the issue-spotter bot's polling loop. `--once` for a single cycle.
  Wrapped by `./start_bot.sh` (fresh-start default; `--continue` to resume).
- **`run_staff.py`** — run one LLM staff persona against the live space.
  `--persona ops|promo|apigw|noise|dupe|injection --token <tok.json>` (`apigw` = the
  API-gateway-timeout incident scenario in `data/scenarios.json`, used by the live demo;
  `noise` = the CONTROL persona: benign small talk, no `facts`, never answers — proves the
  bot files NO issue for non-issue chatter; `dupe` = a SECOND reporter of the apigw incident
  in its OWN thread — proves the bot folds two reports into ONE issue; `injection` = a
  support agent forwarding a hostile pasted block that attempts a prompt injection — proves
  the bot treats the transcript as UNTRUSTED data and never complies; like `noise` it holds
  no `facts` and never answers, and carries a `canary` field the verifier greps for). On
  seed it prints machine-readable `SEEDED_THREAD`/`SEEDED_MSG <id>` lines so an orchestrator
  can verify the bot's handling.
- **`verify_precision.py`** — live verifier for the demo's control case: given the noise
  thread + message ids, confirms the bot SAW the noise (ids in its `seen_message_ids`),
  posted NOTHING into the noise thread, and that the noise came from a NON-bot account.
  Prints `KEY value` lines + `VERDICT PASS|REGRESSION|INCONCLUSIVE` (exit 0/2/3).
- **`verify_dedup.py`** — state-only verifier for the demo's dedup/merge case: given the
  incident + dupe thread ids and the dupe's message ids, attributes `.state/` issues to the
  threads and proves the 2nd report folded into the ONE incident issue (its evidence in that
  issue's `source_message_ids`, no separate dupe issue). `VERDICT MERGED|SEPARATE|INCONCLUSIVE`
  (exit 0/2/3). Best-effort live (LLM phrasing controls the cross-thread jaccard); the merge
  itself is proven deterministically by `tests/test_issue_store.py`.
- **`verify_injection.py`** — live+state verifier for the demo's prompt-injection case: given
  the injection thread + message ids + the `--canary`, reads the LIVE space as the bot and
  proves the guard HELD — the bot SAW the hijack attempt (ids in `seen_message_ids`) and its
  OWN posts in that thread carry neither the compliance canary nor a verbatim phrase from its
  hidden system role (`prompts._ROLE`). Scan is scoped to the injection thread (fresh per run →
  no stale-canary contamination). Also prints `INJECTION_ISSUES` (issues the bot anchored to
  that thread — flagging suspicious DATA is fine, NOT compliance; the demo discounts these from
  the noise-precision count). `VERDICT HELD|BREACHED|INCONCLUSIVE` (exit 0/2/3). The guard
  itself is proven deterministically by `tests/test_goclaw_hardening.py`.

## Demo accounts (token → Gmail → users/<id>) — NON-secret mapping
The refresh tokens under `secrets/` carry no email; resolve via OAuth `tokeninfo`. The
**bot self-filters ONLY its own `users/<id>`** (no sender-type rule), so a staff/noise
persona MUST post from a non-bot account or its messages are dropped by default:
- `token_bot.json`   → **mikmikb26@gmail.com**     `users/116566195804326411461` = THE BOT
- `token_ops.json`   → trantrongducqt@gmail.com     `users/107160784481317583826`
- `token_promo.json` → **mety25757@gmail.com**       `users/115562244684898458288`

`demo_live.sh` posts the incident as `token_ops` and the noise/dupe/injection personas as
`token_promo` (mety25757) — the opt-in `--dupe`/`--injection` personas (or `--all` for both
at once) reuse the non-incident account but each posts in its OWN thread, so the reports read
as distinct humans. The decoy cases share the incident's live timeline as ONE combined showcase
(detect · ignore noise · merge duplicate · refuse injection), not separate runs. Don't point a
persona at `token_bot` (self-filtered → hollow test); the `injection` persona especially MUST
post from a non-bot account or the hijack attempt would be dropped before the bot ever judges it.
- **`authorize.py`** — one-time-per-account OAuth loopback mint.
  `--client <client.json> --out <tok.json> --account <email>` → a refresh token.
- **`demo_local.py`** — full agent loop end-to-end over the in-memory `FakeChatClient` with
  the live/.env LLM — **no Google needed**. `--persona ops|promo|both`, `--max-rounds N`,
  `--voice`. Reports → `reports/demo/`.
- **`demo_incident_call.py`** — a **Gemini Live API "phone call"**: the AI plays the
  on-call engineer from a `data/scenarios.json` persona (default `apigw` = the API-gateway
  504 incident, INFRA-2207) "calling" the callee (default Duc / trantrongducqt@gmail.com),
  opens with a spoken briefing, then answers questions live. Real-time bidirectional VOICE
  over the `google-genai` SDK (Live API) — built from the bundled reference in
  [`docs/gemini_live/`](../docs/gemini_live) (the proven `command-line/python/main.py`).
  **Not** a Chat voice message — the Chat API can't carry a live call (see `MEMORY.md`);
  `--announce` posts a one-line heads-up to `GOOGLE_SPACE` to bridge that framing.
  Modes: default **voice** (mic+speaker, barge-in; needs `pyaudio`); **`--text`** (type
  questions, hear answers; transcript-only if `pyaudio` absent → only needs `google-genai`).
  Auth: **`GEMINI_API_KEY`** (Google AI Studio key — a Google service, distinct from
  `OPENROUTER_API_KEY`) from env/`.env`. Key Live-API facts learned building it: on the
  default model `gemini-3.1-flash-live-preview`, live text MUST use
  `session.send_realtime_input(text=...)` — `send_client_content` only seeds initial history
  (capabilities.md.txt / the model migration doc); audio is 16 kHz in / 24 kHz out PCM; the
  stdin reader is a **daemon** thread (a blocked `readline` is abandoned at exit, not joined,
  so Ctrl+C/drop tears down instantly instead of the 300 s `THREAD_JOIN_TIMEOUT` stall a
  ThreadPoolExecutor would cause). One-time deps (NOT in `igaming`):
  `conda run -n igaming pip install google-genai pyaudio` (+ system PortAudio for pyaudio).
- **`demo_meet_call.py`** — the **issue bot** (`--token`, default `token_bot` =
  mikmikb26) mints a **REAL Google Meet link** via the Meet REST API
  (`spaces.create`) for the `apigw` incident and **DMs** the briefing + join link to
  the human stakeholder, so a HUMAN joins a live incident call. The bot is the
  convener (not the reporter's account). Target precedence: `--space` > the
  bot↔recipient **DM** (`GOOGLE_VOICE_SPACE`, the same DM voice reports use) >
  `GOOGLE_SPACE`. Flags `--persona ops|promo|apigw`, `--token`, `--callee`,
  `--space`, `--dry-run` (mint + print, no post), `--message`. The token MUST carry
  the `…/auth/meetings.space.created` scope (**re-run `authorize.py`** for an older
  token) AND the Meet REST API must be **enabled** in the GCP project (a
  `SERVICE_DISABLED` 403 → `gcloud services enable meet.googleapis.com`).
  Complements `demo_incident_call.py` (local Gemini Live voice): neither makes the
  AI *speak* on a Google call — a hard ceiling (Meet Media API is receive-only; see
  [`docs/CLAUDE.md`](../docs/CLAUDE.md) `google_meet/`).
- **`make_call.py`** — the **minimal "make a phone call"** utility: run AS THE BOT
  (`--token`, default `token_bot` = mikmikb26) it mints a **REAL Google Meet link**
  (Meet REST `spaces.create`) and **DMs** the callee a short "calling you" + join
  link, so a HUMAN taps to join a live call. Default route **bot → Duc** in their DM
  (`GOOGLE_VOICE_SPACE` = spaces/qtotjoAAAAE). Generic (no incident/persona text) —
  the stripped-down sibling of `demo_meet_call.py`. Flags `--to`, `--token`,
  `--space`, `--message`, `--dry-run` (mint + print, no post). Same hard limit: the
  AI cannot *speak* on the Meet (Media API receive-only) — it sets up the call.
  Same prereqs: token needs the `meetings.space.created` scope + the Meet REST API
  enabled in the GCP project. Each run mints a fresh meeting (request id keyed to
  the new meeting code → never deduped against a prior call).
- **`meet_call_browser.py`** — the **native ringing call** via browser automation
  (the "trick" — no API can ring; only the Chat UI's call button does). Drives the
  **system Brave** (`/usr/bin/brave-browser`) with **Playwright** (Python pkg only,
  `pip install playwright` — system browser is reused, NO `playwright install`), in
  a **persistent profile** (`--profile-dir`, default `.browser-profile/`,
  gitignored — holds live Google cookies) so login survives runs and never locks
  the daily browser. Opens a Chat DM (`--space`/`--url`/`--authuser`), finds the
  call button (aria-label patterns; `--dry-run` DUMPS all visible button labels →
  pass the right one via `--button-name`), clicks it → **rings** the callee, then
  holds the call `--duration` s (or `--keep-open`). `--cdp-url` reuses a daily Brave
  launched with `--remote-debugging-port`. First run is manual: sign into Google in
  the headed window, open the DM, re-run. ⚠️ Automates Google UI (ToS-violating,
  brittle selectors, account-flag risk) — demo accounts only. This is the RING +
  WebRTC-presence half; the AI **voice** half (virtual PulseAudio devices + the
  `demo_incident_call.py` Gemini Live loop pointed at them) is documented in a
  footer block in the script, NOT yet wired.
  - **RECOMMENDED — isolated profile + ONE-TIME MANUAL login (non-disruptive).** Runs
    its OWN browser instance on `--profile-dir` (per-browser default, gitignored)
    ALONGSIDE the daily Brave (different user-data-dir → no lock clash, daily session
    untouched; verified headed alongside a running daily Brave). Do the login in a
    **plain Brave** (not Playwright, so Google's "browser may not be secure" block
    doesn't bite): `rm -rf .browser-profile && brave-browser
    --user-data-dir="$PWD/.browser-profile"` → sign in as **mikmikb26 only** (→ `u/0`,
    default `--authuser 0`, NO glo.com), open the Duc DM, close. Then place calls:
    `meet_call_browser.py --browser brave --browser-path /usr/bin/brave-browser
    --profile-dir .browser-profile --authuser 0 --duration 60`.
  - **⚠️ DEAD END — copying/importing the session does NOT work (tested 2026-06-17).**
    `--import-cookies <cdp-url>` (pull live cookies via CDP → inject into an isolated
    profile) and file-copying the profile both *work for a few minutes then Google
    signs ALL copies out* (redirect to `accounts.google.com/signin`). Cause: Google
    binds the session to the original browser via rotating `SIDCC` cookies +
    **Device-Bound Session Credentials** (a private key the copy lacks). The genuine
    daily Brave keeps working; only copies die.
  - **⚠️ WORSE — once the account is FLAGGED, even a fresh isolated login dies
    (tested 2026-06-18).** After enough automation/copy churn, Google's risk engine
    flags the account and then *also* invalidates a brand-new isolated profile login
    within minutes: a clean plain-Brave login (verified: 37 google cookies, 15
    SID/SAPISID rows persisted) was force-signed-out on the next automated open
    (Playwright-launch → `signin/challenge/pwd` "Hi Tran"; plain-launch + CDP →
    `signin/identifier`). The ONLY session that kept working through all of this was
    the **long-established DAILY Brave via CDP** (`--cdp-url http://127.0.0.1:9222
    --authuser 1` → call button found). **Conclusion: an "isolated browser that
    doesn't touch the daily session" is NOT achievable for a flagged account** — its
    fresh sessions get killed. Realistic paths: (a) **CDP into the genuine daily
    Brave** (works; the script opens a NEW tab via `new_page`, so existing tabs are
    untouched — only a call window appears), or (b) wait for the flag to cool down
    (hours/days) before a fresh isolated login will stick (unproven), or (c) the human
    clicks the call button manually. The `--import-cookies`/`--login` flags are kept
    only as documented experiments; don't rely on them for a flagged account.
  - **PROVEN live recipe (2026-06-17, this machine) — CDP into the daily Brave.**
    Verified end-to-end but DISRUPTIVE (hijacks your session): 1) quit Brave,
    `brave-browser --remote-debugging-port=9222 --profile-directory="Default"`;
    2) `python scripts/meet_call_browser.py --cdp-url http://127.0.0.1:9222 --authuser 1`.
    Env facts (Work/Default profile): **`u/0` = `dttran@glo.com` (REVOKED — never
    use)**, **`u/1` = `mikmikb26@gmail.com` (bot/caller)** → `--authuser 1`. Shared
    facts (both paths): the Chat DM deep link the live app routes to is
    **`https://chat.google.com/u/<n>/app/chat/<spaceId>`** (the older
    `#chat/space/...` hash form silently bounces to `/app/home`); the DM call control
    is labelled **`Start a video call`**, and clicking it shows a Meet pre-join whose
    **`Join now`** must be clicked to actually ring the callee (the script does this).
- **`meet_audio_inject.py`** — the **"AI mouth"** engine (counterpart to
  `meet_audio_capture.py`'s "AI ear"): `AudioInjector` builds a virtual mic
  (`module-null-sink ai_mic_sink` + `module-remap-source ai_mic`), makes ai_mic the
  default capture source, and plays a file (or a generated 4-note test tone) into it
  with ffmpeg so the CALLEE hears it as the caller. Fully reversible (stop() restores
  the prev default + unloads modules, atexit-guarded). Used by `meet_call_browser
  --inject-audio` and `ai_call.py`. Standalone proof: `python
  scripts/meet_audio_inject.py --verify` (records the ai_mic source → volumedetect).
  Bidirectional follow-on now built: `gemini_voice.py` / `gemini_call.py` (below).
- **`ai_call.py`** — the **minimal AI-mouth call** (the focused entry point for the
  AI-voice-on-a-call direction). Launches a DEDICATED caller Brave
  (`.browser-profile-caller`, port 9333) with `--use-fake-ui-for-media-stream` so the
  mic is **auto-granted (no manual allow click)** and getUserMedia binds to the
  default device (= ai_mic, set before the call), then delegates ring+WebRTC-join+
  inject to `meet_call_browser.main` over CDP. **PROVEN + user-confirmed 2026-06-20**:
  callee heard the tone with no allow click and **no move-dance needed** (the
  default-source preset means the browser grabs ai_mic from the start — `move_browser_mic`'s
  "no browser mic → silence" log is a FALSE alarm on this path; see MEMORY.md "ai_call.py").
  Plain-launch (not Playwright) for login survival; leaves the browser running for reuse
  (`--quit-browser` to stop, via /proc scan — never `pkill -f <profile>`, self-matches).
  Flags: `--audio FILE` (default test tone), `--duration`, `--at-join` (default from-ring),
  `--once`, `--url`, `--port`, `--profile`, `--login-wait`. First run needs the dedicated
  profile signed in as mikmikb26 (the script prints the one-time command). Keep the window
  VISIBLE (native Wayland suspends an occluded renderer → call drops). ⚠️ demo accounts only.
- **`gemini_voice.py`** — the **bidirectional Gemini Live ⇄ call audio bridge** (the AI
  gets a MOUTH and an EAR). `GeminiVoiceBridge` sets up TWO virtual PulseAudio devices:
  MOUTH = `ai_mic_sink` null sink + `ai_mic` remap-source (default **source**) — Gemini's
  24 kHz audio → ffmpeg → ai_mic_sink → browser mic → callee; EAR = `gemini_call_spk` null
  sink (default **sink**) — browser plays the callee's voice there → ffmpeg records its
  `.monitor` at 16 kHz → Gemini realtime input. The Gemini Live session
  (`client.aio.live.connect`, model `gemini-3.1-flash-live-preview`, AUDIO out + both
  transcriptions) runs in a worker thread; setup/teardown are sync (pactl) and restore the
  prev default source+sink. **Greeting hardening** (session 2): the ear is GATED
  (`_ear_to_gemini` drains+discards) until the callee truly answers AND the opening is fully
  delivered — `on_pickup` greets, moves the playback, waits for the greeting's `turn_complete`
  (≤`GREET_MAX_WAIT`=20s), THEN opens the ear — so the AI always speaks FIRST, uncut by callee
  noise. `trigger_greet()` uses **`send_realtime_input(text=…)` NOT `send_client_content`**
  (the latter only seeds history on this model → slow greeting; the swap = first audio ~0.6s
  after pickup). Mouth ffmpeg has `-buffer_duration 80` (low-latency playout). `greet_text`
  overrides what's said first (incident mode). **Debug log + audio recording (default ON):**
  every `[voice]` event + transcript → `logs/gemini_call_<ts>.log` (elapsed `+N.NNs` stamps);
  both directions → `_mouth.wav` (24 kHz) + `_ear.wav` (16 kHz). Helpers: `load_gemini_key`
  (env→.env, MEMORY's quoted/comment-safe parse), `build_live_config`. Standalone:
  `--devices-test` (sinks+probe, no Gemini) / `--selftest` (text→audio→WAV). Needs
  `GEMINI_API_KEY` + `google-genai`.
- **`gemini_call.py`** — the **orchestrator**: Gemini Live is the CALLER, you're the callee,
  you talk to each other on a real Chat call. Composes `ai_call` (pre-granted caller Brave +
  login gate), `gemini_voice.GeminiVoiceBridge` (audio + Gemini session in a worker thread),
  and `meet_call_browser.main(..., on_join=bridge.on_join, on_pickup=bridge.on_pickup)` over
  CDP with `--watch-join --ensure-mic-on` (NO `--inject-audio` — the bridge owns the audio).
  Main thread drives the browser (sync Playwright) + blocks until hang-up, then stops the bridge
  and restores devices. **`--persona apigw` = incident-report mode**: on pickup the AI reports a
  `data/scenarios.json` incident (e.g. the API-gateway 504, INFRA-2207) in Vietnamese, then
  answers from its held facts. The AI is a NEUTRAL INTERMEDIARY ("trợ lý trực sự cố") that
  *relays* the incident on behalf of the on-call owner (**Dave** for apigw) — it is NOT that
  engineer and does NOT own the incident, so "who's responsible?" → Dave, never itself; and it
  answers strictly from the report, saying it doesn't know / will check back for anything not in
  it (no guessing). `build_incident_persona` builds that VN intermediary prompt + opening;
  `_reporter_name(role)` derives the owner name from the scenario `role` (so the apigw scenario's
  reporter name = the owner the AI cites). Reuses the apigw scenario shared with
  `run_staff`/`demo_incident_call`. Flags:
  `--duration`(180), `--persona`, `--callee`(Duc), `--url`, `--port`, `--profile`, `--model`,
  `--voice`(Aoede), `--system`/`--system-file` (default = a Vietnamese AI-caller persona),
  `--language`, `--no-greet`, `--no-record`, `--quit-browser`, `--diag-pickup`. Run (generic
  two-way): `conda run --no-capture-output -n igaming python -u scripts/gemini_call.py`; incident
  report: add `--persona apigw --callee Duc`. Keep the window VISIBLE; ⚠️ demo accounts only. Echo:
  callee should use a phone/headset (its AEC).
  - **`--diag-pickup`** (passed through to `meet_call_browser`): logs elapsed-stamped `[join]`
    (bot's own join flow) + `[ring]` (pickup-detection poll) lines — the tool that found the
    greeting-latency bugs. **Greeting latency was fixed (session 3, 2026-06-20): ~15-22s → ~3s.**
    Two root causes in `meet_call_browser`, both BEFORE the callee answers, neither model/transport:
    a 5s meeting-code retry loop (now gated behind `--watch-rest`), and a ~38s call-button
    double-click stall (the in-place DM call has no popup → the redundant 2nd click stalled 30s on
    a detached element; now clicks ONCE). Details in [`../MEMORY.md`](../MEMORY.md) "Greeting-latency
    root-cause fixes (session 3)".
- **`meet_rest_watch.py`** — the **REST room-data** half: given a meeting code
  (`--meeting-code abc-mnop-xyz`) or `--auto` (the caller's currently-active
  conference), polls the **Meet REST v2** API (`conferenceRecords` +
  `participants`, filter `latest_end_time IS NULL`) and prints the live roster each
  cycle, reporting a remote LEAVE (hang-up). Queryable only by the **call
  organizer's** token (the caller = bot; default `secrets/token_bot.json`); needs
  the `meetings.space.created` (or `…readonly`) scope. ⚠️ REST conference/participant
  data has propagation latency (seconds–minutes) → reliable but not instant. Core
  loop is the reusable `watch(meet, *, meeting_code, self_id, poll, duration,
  find_timeout, stop_event=None)` (urllib + OAuth only, no Playwright) — `main()`
  calls it, and `meet_call_browser --watch-rest` runs it in a background thread.
  ⚠️ **GOTCHA / how a native Chat 1:1 call maps to REST (verified live 2026-06-18)**:
  a Chat DM call creates its own AUTO-generated Meet space (e.g.
  `spaces/YIwUXyrGa9IB`), distinct from the Chat space. A conferenceRecord IS
  created once the conference STARTS (someone truly joins — a 3s join produced a
  3.5s record), BUT you can only find it via the UNFILTERED `conferenceRecords.list`
  (newest-first) — NOT by `space.meeting_code="…"` (the code scraped from the call
  page does NOT match; returns `{}`) and NOT by the Chat space name (also `{}`). It
  also only appears POST-HOC with propagation lag → **conferenceRecords polling is
  NOT real-time** and is the wrong tool for "detect the join the instant it happens"
  (see the real-time channels in the root CLAUDE.md / `huddle_watch.py`). An
  *unanswered* ring (no one joins) creates no record at all.
- **`meet_call_browser.py`'s `--watch-join` (REAL-TIME join detection)**: the only
  truly *instant* "bot catches the join the moment they enter" channel, because the
  bot is physically IN the call via the browser. **PROVEN END-TO-END LIVE
  (2026-06-18)**: ring placed mikmikb26→Duc, join fired `+26.9s` (= the answer
  delay, not detection latency), clean self-terminate on hang-up. Three independent
  join signals, ANY fires it (`runner` hold loop):
  - **(a) DOM roster** `[data-participant-id]` tile count `≥2`, and **(b)** the
    `'X joined'` toast (also yields the name). Instant when the tab is FOREGROUND.
  - **(c) WebRTC remote-track growth** — `_WEBRTC_HOOK` (a context init-script added
    when `--watch-join`) wraps `RTCPeerConnection` to count inbound `track` events;
    `_webrtc_track_count` reads it. Fires when the count rises ABOVE a baseline that's
    re-captured during a 6s settle window after the call is placed (so OUR own SFU
    receive-tracks ramp never false-fires). **This is the signal that survives the call
    tab being BACKGROUNDED** — see below.
  Fires once, printing `🔔 REMOTE JOINED: <name> (…tiles=N, tracks=N/base=N, via=…)`
  + a machine-readable `PARTICIPANT_JOINED <name>`. State machine: **1 = caller alone
  (ringing) · ≥2 = remote joined · 0 = call ended (UI torn down)**. After a join, a
  roster collapse to 0 for 3 polls self-terminates the loop EVEN when the flaky
  `in_call` control check never confirmed (so it doesn't hold to the `--duration` cap).
  - 🔑 **Backgrounding the tab throttles the DOM, NOT the WebRTC layer (proven live
    2026-06-18).** When the user answered then **switched to another tab to work**, the
    join fired with `tiles=0, tracks=5, via=webrtc` — the Meet tab was hidden so its
    DOM roster never rendered the participant tile (read 0), but the inbound media
    `track` events still fired (it's why Meet audio keeps playing when you tab away).
    **Without signal (c) this join would have been MISSED.** So: DOM signals are for the
    foreground; the WebRTC counter is mandatory for "detect the join while I work in
    another tab." `connectionState` is still NOT a join signal (SFU → `connected` once
    the CALLER joins, before any remote).
  - ⚠️ **Account gotcha**: `--space` default builds a `/u/0/` URL, and **authuser 0 =
    glo.com (REVOKED — never use)**. Pass `--url 'https://chat.google.com/u/1/app/chat/
    qtotjoAAAAE'` (mikmikb26 = authuser 1) explicitly, or `--authuser 1`. A `/u/0/` run
    stalls at "connecting (CDP)" (glo.com not usable) — kill it.
  - ⚠️ Other UI-automation brittleness: across CDP runs we've hit `(no 'Join now'
    button)` (the call still rings directly — fine), `TargetClosedError` (caused by the
    user CLOSING the script's tab mid-run — switching tabs is safe, closing is not),
    and slow connect (attaching CDP to a full Brave enumerates all tabs; the call-button
    wait is silent for ~30-45s). For a clean run: don't close the script's tab; run
    ONCE; let it self-terminate. Composes with `--watch-rest` (independent; REST still
    lags — it found no conference before the call ended, as expected). Bugs fixed while
    building: join detection was wrongly gated behind `in_call` (now ungated), and
    `--watch-rest` must use AUTO not the scraped meeting_code.
- **`diag_call_join.py`** — the diagnostic that discovered the join signal (sibling of
  `diag_call_dom.py` for hang-up): places a CDP call, hooks `RTCPeerConnection`
  (`ontrack` remote-track count + state history) AND probes the roster DOM each second
  through the callee answering, logging to `/tmp/call_join.log` which signal flips on
  join. Re-run to re-verify if Meet's DOM drifts (`[data-participant-id]` selector).
- **`meet_call_browser.py`'s `--watch-rest`**: after the native call connects, it
  extracts the Meet meeting code from the call-page URL (`_extract_meeting_code`,
  3-4-3 form; falls back to scanning frame links/HTML, then to REST `--auto` if
  unreadable), prints a machine-readable `MEETING_CODE <code>` line, and runs
  `meet_rest_watch.watch(...)` in a daemon thread (urllib-only → safe alongside the
  Playwright main thread) until the call ends. This is the one-command **call → get
  meeting ID → REST room data** chain. Extra flags: `--rest-token` (default the bot
  = organizer), `--rest-poll`, `--rest-self-id`, `--rest-find-timeout`.
- **`meet_audio_capture.py` + `meet_call_browser.py --capture-audio`** — CAPTURE the
  REMOTE voice the bot hears, to a WAV, as the INPUT path for a future Gemini Live loop
  ("put an AI ear on the call"). Output is **16 kHz mono s16le PCM** — exactly Gemini
  Live's realtime-input format, so the next step is a straight swap of "write WAV" for
  "stream frames to Gemini". Driven by `--capture-audio` (start after the call is
  placed, stop on hang-up; `--audio-out` overrides `reports/meet_audio_<ts>.wav`;
  `--audio-mode` picks the path). Prints `CALL_AUDIO <path>` on success. Standalone OS
  tooling check: `python scripts/meet_audio_capture.py --selftest`. **Modes
  (`--audio-mode`, default `allsinks` — the user-confirmed working path for Meet):**
  - **`webrtc` (DEFAULT flag value, but ⚠️ DISQUALIFIED on Google Meet — use `monitor`)** —
    `BrowserAudioTap` taps the **inbound WebRTC audio
    track INSIDE the browser**: the combined `_WEBRTC_HOOK` (also the join-detection
    hook) sees `window.__MCB_CAPTURE`, attaches a `MediaRecorder` to an immortal
    AudioContext→MediaStreamDestination graph fed by the inbound audio track, and pushes
    base64 webm/opus chunks onto `window.__audioChunks`; the Python side drains them each
    loop tick. This is the REMOTE voice the bot hears = **the CALL's voice, not other
    tabs/apps** (it's the actual media stream, OS-independent — no desktop mix, blind to
    other apps by construction), and it survives a backgrounded tab (with the anti-
    occlusion launch flags — see below). **Truncation-immune (fixed 2026-06-19):** each
    chunk is tagged `"<frameId>:<gen>|<b64>"`, grouped into one standalone webm SEGMENT
    per (frame, recorder-generation), drained ONLY from the owner frame; at stop each
    segment is transcoded and the WAVs concatenated (`-f concat -c copy`). A restarted
    recorder (renderer suspend/error) lands in a NEW segment instead of corrupting the
    file — the old single-`.webm` append truncated the WAV to ~3.15s. Flush-before-hangup:
    `stop()` calls `__mcbRecorder.stop()` on the still-open page, waits, drains, finalizes.
    Needs only `ffmpeg`. Proven offline by `test_segment_concat`. 🔑 **The decode-activation
    fix**: a remote WebRTC track feeding a WebAudio source outputs SILENCE unless also sunk
    into a PLAYING muted `new Audio()` (Chromium lazy-decodes remote audio); `__mcbStartRec`
    does this per track + retains the ref. Proven necessary AND sufficient by a Google-free
    WebRTC loopback A/B (with-sink −9.1 dB vs stripped −91 dB) driving the real hook/tap. ⚠️
    **Capture + hang-up only work while the renderer is AWAKE** — GNOME-Wayland fully suspends
    an OCCLUDED Brave renderer (the anti-occlusion flags are Wayland no-ops; you can't even
    programmatically occlude/minimize to test). **The fix: run the caller under Xvfb** (virtual
    display, no compositor → never occluded → renderer always awake → unattended). Full
    findings: [`MEMORY.md`](../MEMORY.md) "Unattended call self-test".
  - **`allsinks` (✅ RECOMMENDED for Meet, 2026-06-19) — record EVERY sink monitor, mix at stop.**
    One ffmpeg recorder per output sink (independent, NOT a single amix process), merged with
    `amix normalize=0` on stop. This is the robust capture: the call's audio (ring/voice) can land
    on a DIFFERENT HDA sink between calls, so locking one sink (monitor mode) can miss it; recording
    ALL can't. The per-sink layout is the exact config that captured the live remote voice (the call-4
    diagnostic: −21 dB of speech for ~30s on sink 134 while 3 other sinks were silent). No routing
    change, nothing to restore, operator still hears the call. Pair with **`--capture-from-ring`** to
    include the ringback. ⚠️ Captures the OS output (= the remote voice the bot hears), blind to the
    local mic (fine — the bot has none). NOT yet live-verified end-to-end through the script (testing
    halted after call 5); the gating live blocker is media-flow reliability on the occluded daily-Brave
    renderer, not the capture. Full story: [`MEMORY.md`](../MEMORY.md) "the full 5-call investigation".
  - **`monitor`** — record the sink Brave plays to (`_browser_output_sink_name`, app-aware), its
    `.monitor`. Simpler but locks ONE sink at start → MISSES the voice if it lands on a different
    sink than the ring (observed: call 1 captured the ring, lost the voice). Use `allsinks` instead.
    No move/match step (can't fail the way `isolate` did). Operator still hears the call.
  - **`isolate`** — null sink + MOVE the browser stream in + record its monitor. Clean
    in theory but fragile (depends on matching Brave's PulseAudio stream by app name)
    and mutes the call for the operator. A live run captured **37s of silence** — the
    matcher never matched Brave's sink-input, so Brave kept playing to BT and the null
    sink stayed empty (the null-sink MECHANISM is sound: a realtime paplay tone into it
    captured -25 dBFS; only the match step broke). Kept for a future per-tab isolation.
  ⚠️ **SUPERSEDED (2026-06-19): on Meet, use `monitor`, NOT `webrtc`.** The earlier
  "tap the WebRTC media at the source" guidance is correct in general but FAILS on Google
  Meet specifically — Meet renders remote audio through its own Web Audio path, exposing no
  tappable MediaStreamTrack/media-element, so the in-browser tap records a 0-byte webm
  (−91 dB). Meet's DECODED audio IS on the OS output sink (you HEAR the call), so `monitor`
  (record that sink's `.monitor`) is the path that actually captures the remote voice —
  live-verified −28.2 dB. monitor captures the OS output (= the remote voice the bot hears),
  which is exactly what we want; it's blind to the LOCAL mic, but the bot has no mic anyway.
  ⚠️ **PipeWire fact** (monitor/isolate): a sink monitor only carries audio while the
  sink is **RUNNING**, which needs a **realtime** producer — a non-`-re` ffmpeg tone
  dumps its buffer and exits → monitor records silence (a test artifact, not a real
  failure; Brave's live stream is realtime). ⚠️ **Capture is only as good as the
  talking**: all modes record what the bot RECEIVES = the REMOTE voice, so the human
  must actually SPEAK on the OTHER (Duc) device, else the WAV is (near-)silent.
  Realises the "AUDIO (next phase)" comment block in meet_call_browser.
- **`auto_answer.py`** — the unattended CALLEE: drives a 2nd Brave (separate
  `--remote-debugging-port`, fresh `.browser-profile-callee`, signed in as Duc) over CDP,
  navigates to the DM, and on an incoming ring clicks **answer** then turns mic + camera
  ON so media flows for the caller's capture; auto-**leaves** after a hold so the caller's
  hang-up detection fires. Matches the exact VI labels (answer `Trả lời cuộc gọi`, leave
  `Rời khỏi cuộc gọi`, unmute `Bật micrô`, camera-on `Bật máy ảnh`) and skips disabled
  decoy buttons. Prints `ANSWERED <t>` / `LEFT` markers. Needs the callee Brave launched
  with `--use-fake-{ui,device}-for-media-stream` (auto-grants mic/cam + a fake A/V source)
  AND the anti-occlusion flags.
- **`selftest_call.sh`** — the one-command HANDS-OFF self-test of the whole call loop.
  **By DEFAULT it runs the caller under Xvfb** (starts `Xvfb :99` + a caller Brave on port
  9322 from `.browser-profile-caller`, then tears them down) so the caller renderer is never
  occluded → **fully unattended, no window to keep in front**. One-time prereq: sign
  `.browser-profile-caller` in as **mikmikb26 only** (the script prints the exact command if
  the dir is missing; fresh single-account profile ⇒ DM is `u/0`). It starts the bot/caller
  (`meet_call_browser.py --watch-join --capture-audio`) ringing mikmikb26→Duc and
  `auto_answer.py` on the callee, then reports two independent verdicts — **HANG-UP
  DETECTION** (answered + a `📴 Call ended — <reason>` + the duration cap was NOT hit) and
  **AUDIO CAPTURE** (ffmpeg `volumedetect` mean_volume > −80 dB). `DURATION` is a cap only.
  Flags: `--diag` (live DOM/capture dumps), `--caller-real` (revert to the babysat `:9222`
  daily-Brave path), `--caller-headed` (dedicated clean profile HEADED on the real GPU — the
  proven media-connecting path, but its window must stay VISIBLE or Wayland suspends the
  renderer and drops the call), `--caller-xwayland` (headed real-GPU on XWayland with occlusion
  disabled — HYPOTHESIS to survive a covered window; see MEMORY "evening" update),
  `--no-callee` (a HUMAN answers on another device — no auto-callee; the caller's `REMOTE
  JOINED` is the pickup proof, the audio verdict still runs), `--caller-profile <dir>`,
  `--answer-seconds N`. The callee still runs on the real-display `:9223` (it answers fine
  occluded; if its fake-mic proves silent occluded, run it under Xvfb too).
  ⚠️ **Caller-mode reality (2026-06-19)**: media stability is the live blocker — headed-Wayland
  connects-then-DROPS on occlusion, Xvfb/swiftshader NEVER connects. Voice capture (req #3) is
  NOT yet live-verified non-silent. ⚠️ Kill stuck caller/callee braves by PID (`/proc` scan),
  never `pkill -f <profile-path>` from an interactive shell (self-matches → SIGKILLs the shell).
- **`run_webhook.py`** — webhook ingress entrypoint, **Phase-2 DEFERRED** stub.

The repo-root **`demo_live.sh`** wraps `run_poller.py` + `run_staff.py` into a
one-command LIVE end-to-end demo (seed an "API gateway timeout" incident → resolve
→ filed GitHub issue + voice DM); details in the root [`CLAUDE.md`](../CLAUDE.md).
