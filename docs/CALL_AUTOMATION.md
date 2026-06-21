# Call automation — the deep saga

The full experiment-log / war-story record behind the `scripts/` call-automation tools
(native ringing Chat calls + the "AI voice on a call" direction). `scripts/CLAUDE.md`
keeps the lean per-script index + run commands; this file holds the blow-by-blow detail,
dead-ends, and proven recipes that don't belong inline in an index.

Why any of this exists: an AI **cannot be a speaking participant on a Google Meet via the
APIs** — the Meet Media API is receive-only and Developer-Preview-gated (see
[`CLAUDE.md`](CLAUDE.md) `google_meet/`), and no API can make a native Chat call *ring*.
The only path that rings a human is the Chat UI's call button, so the call tools drive a
real browser. ⚠️ All of this automates Google's UI (ToS-violating, brittle selectors,
account-flag risk) — **demo accounts only**.

## Account / authuser facts (durable, safety-critical)
- **`authuser 0` = `dttran@glo.com` — REVOKED, NEVER use.** A `/u/0/` run stalls at
  "connecting (CDP)". **`authuser 1` = `mikmikb26@gmail.com`** = the bot/caller → pass
  `--authuser 1` or an explicit `--url 'https://chat.google.com/u/1/app/chat/<spaceId>'`.
- The Chat DM deep link the live app routes to is
  **`https://chat.google.com/u/<n>/app/chat/<spaceId>`** (the older `#chat/space/...`
  hash form silently bounces to `/app/home`).
- The DM call control is labelled **`Start a video call`**; clicking it shows a Meet
  pre-join whose **`Join now`** must be clicked to actually ring the callee (the script
  does this). Across CDP runs we've also seen `(no 'Join now' button)` — the call still
  rings directly, which is fine.
- Bot↔Duc DM = `spaces/qtotjoAAAAE` (`GOOGLE_VOICE_SPACE`).

## Browser session: what works, what's a dead end
- **⚠️ DEAD END — copying/importing the session does NOT work (tested 2026-06-17).**
  `--import-cookies <cdp-url>` (pull live cookies via CDP → inject into an isolated
  profile) and file-copying the profile both *work for a few minutes then Google signs
  ALL copies out* (redirect to `accounts.google.com/signin`). Cause: Google binds the
  session to the original browser via rotating `SIDCC` cookies + **Device-Bound Session
  Credentials** (a private key the copy lacks). The genuine daily Brave keeps working;
  only copies die. The `--import-cookies`/`--login` flags are kept only as documented
  experiments.
- **⚠️ WORSE — once the account is FLAGGED, even a fresh isolated login dies (tested
  2026-06-18).** After enough automation/copy churn, Google's risk engine flags the
  account and then *also* invalidates a brand-new isolated profile login within minutes:
  a clean plain-Brave login (37 google cookies, 15 SID/SAPISID rows persisted) was
  force-signed-out on the next automated open (Playwright-launch →
  `signin/challenge/pwd` "Hi Tran"; plain-launch + CDP → `signin/identifier`). The ONLY
  session that kept working through all of this was the **long-established DAILY Brave via
  CDP** (`--cdp-url http://127.0.0.1:9222 --authuser 1` → call button found).
  **Conclusion: an "isolated browser that doesn't touch the daily session" is NOT
  achievable for a flagged account.** Realistic paths: (a) **CDP into the genuine daily
  Brave** (works; the script opens a NEW tab via `new_page`, so existing tabs are
  untouched — only a call window appears), (b) wait for the flag to cool down (hours/days)
  before a fresh isolated login will stick (unproven), or (c) the human clicks the call
  button manually.
- **RECOMMENDED for a clean account — isolated profile + ONE-TIME MANUAL login
  (non-disruptive).** `meet_call_browser.py` runs its OWN browser instance on
  `--profile-dir` (per-browser default, gitignored) ALONGSIDE the daily Brave (different
  user-data-dir → no lock clash, daily session untouched; verified headed alongside a
  running daily Brave). Do the login in a **plain Brave** (not Playwright, so Google's
  "browser may not be secure" block doesn't bite):
  `rm -rf .browser-profile && brave-browser --user-data-dir="$PWD/.browser-profile"` →
  sign in as **mikmikb26 only** (→ `u/0`, default `--authuser 0`, NO glo.com), open the
  Duc DM, close. Then place calls:
  `meet_call_browser.py --browser brave --browser-path /usr/bin/brave-browser
  --profile-dir .browser-profile --authuser 0 --duration 60`.
- **PROVEN live recipe (2026-06-17, this machine) — CDP into the daily Brave.** Verified
  end-to-end but DISRUPTIVE (hijacks your session): 1) quit Brave,
  `brave-browser --remote-debugging-port=9222 --profile-directory="Default"`;
  2) `python scripts/meet_call_browser.py --cdp-url http://127.0.0.1:9222 --authuser 1`.
  (Work/Default profile: `u/0` = glo.com REVOKED, `u/1` = mikmikb26 = bot/caller.)

## `meet_call_browser.py` module layout (split for size, behavior-preserving)
`meet_call_browser.py` is now a thin **facade**: the usage docstring + `__main__` +
re-exports of the public surface other scripts import (`main`, `_WEBRTC_HOOK`, and the
call-button / hang-up probe helpers `_IN_CALL_PATTERNS`/`_find_call_button`/
`_dump_buttons`/`_find_button_in_frames`/`_in_call`/`_alone_signal` used by
`call_network_capture`). The implementation lives in 4 cohesive sibling modules:
- **`meet_call_js.py`** — embedded JS: the `_WEBRTC_HOOK` RTCPeerConnection hook +
  getStats/DOM probe strings.
- **`meet_call_signals.py`** — path setup + aria/regex pattern constants + every
  page-probe wrapper that reads the live call page.
- **`meet_call_setup.py`** — browser launch / CDP-attach / cookie-import / exec-resolve /
  proc-match / URL + renderer keepalive.
- **`meet_call_run.py`** — `_start_rest_watch` + `main()` (the call lifecycle, moved
  verbatim).

Import DAG is acyclic: js ← signals ← setup; all ← run; run+js+signals ← facade.
**Edit the relevant module, not the facade.**

Optional follow-ups (deferred — need a live call to validate): (1) **Perf:** `main()`'s
poll fires ~8 separate CDP `evaluate` round-trips per tick (join + roster + several
`_webrtc_*` globals + survey/calling/frame probes), which can stretch the nominal
`--join-poll`. Batch the per-frame reads into ONE `evaluate` returning all signals to cut
latency — behavior-affecting, so verify on a real call first. (2) **Tidy:**
`meet_call_setup` imports `_REPO_ROOT`/`_resolve_path` from `meet_call_signals`; a tiny
shared `paths` module would be cleaner (pure taste, no behavior change).

## `--watch-join` — REAL-TIME join detection
The only truly *instant* "bot catches the join the moment they enter" channel, because the
bot is physically IN the call via the browser. **PROVEN END-TO-END LIVE (2026-06-18)**:
ring placed mikmikb26→Duc, join fired `+26.9s` (= the answer delay, not detection
latency), clean self-terminate on hang-up. Three independent join signals, ANY fires it:
- **(a) DOM roster** `[data-participant-id]` tile count `≥2`, and **(b)** the `'X joined'`
  toast (also yields the name). Instant when the tab is FOREGROUND.
- **(c) WebRTC remote-track growth** — `_WEBRTC_HOOK` (a context init-script added when
  `--watch-join`) wraps `RTCPeerConnection` to count inbound `track` events;
  `_webrtc_track_count` reads it. Fires when the count rises ABOVE a baseline that's
  re-captured during a 6s settle window after the call is placed (so OUR own SFU
  receive-tracks ramp never false-fires). **This is the signal that survives the call tab
  being BACKGROUNDED.**

Fires once, printing `🔔 REMOTE JOINED: <name> (…tiles=N, tracks=N/base=N, via=…)` + a
machine-readable `PARTICIPANT_JOINED <name>`. State machine: **1 = caller alone (ringing)
· ≥2 = remote joined · 0 = call ended (UI torn down)**. After a join, a roster collapse to
0 for 3 polls self-terminates the loop EVEN when the flaky `in_call` control check never
confirmed (so it doesn't hold to the `--duration` cap).

- 🔑 **Backgrounding the tab throttles the DOM, NOT the WebRTC layer (proven live
  2026-06-18).** When the user answered then **switched to another tab to work**, the join
  fired with `tiles=0, tracks=5, via=webrtc` — the Meet tab was hidden so its DOM roster
  never rendered the participant tile (read 0), but the inbound media `track` events still
  fired. **Without signal (c) this join would have been MISSED.** So: DOM signals are for
  the foreground; the WebRTC counter is mandatory for "detect the join while I work in
  another tab." `connectionState` is still NOT a join signal (SFU → `connected` once the
  CALLER joins, before any remote).
- ⚠️ **Account gotcha**: `--space` default builds a `/u/0/` URL, and authuser 0 = glo.com
  (REVOKED). Pass `--url '…/u/1/app/chat/qtotjoAAAAE'` or `--authuser 1`.
- ⚠️ Other UI-automation brittleness: `TargetClosedError` (caused by the user CLOSING the
  script's tab mid-run — switching tabs is safe, closing is not), and slow connect
  (attaching CDP to a full Brave enumerates all tabs; the call-button wait is silent for
  ~30-45s). For a clean run: don't close the script's tab; run ONCE; let it self-terminate.
  Composes with `--watch-rest` (independent). Bugs fixed while building: join detection was
  wrongly gated behind `in_call` (now ungated), and `--watch-rest` must use AUTO not the
  scraped meeting_code.

## `--watch-rest` / `meet_rest_watch.py` — REST room-data half
After the native call connects, `--watch-rest` extracts the Meet meeting code from the
call-page URL (`_extract_meeting_code`, 3-4-3 form; falls back to scanning frame
links/HTML, then to REST `--auto`), prints `MEETING_CODE <code>`, and runs
`meet_rest_watch.watch(...)` in a daemon thread (urllib-only → safe alongside the
Playwright main thread) until the call ends. Extra flags: `--rest-token` (default the bot =
organizer), `--rest-poll`, `--rest-self-id`, `--rest-find-timeout`.

⚠️ **How a native Chat 1:1 call maps to REST (verified live 2026-06-18)**: a Chat DM call
creates its own AUTO-generated Meet space (e.g. `spaces/YIwUXyrGa9IB`), distinct from the
Chat space. A conferenceRecord IS created once the conference STARTS (someone truly joins —
a 3s join produced a 3.5s record), BUT you can only find it via the UNFILTERED
`conferenceRecords.list` (newest-first) — NOT by `space.meeting_code="…"` (the code scraped
from the call page does NOT match; returns `{}`) and NOT by the Chat space name (also
`{}`). It also only appears POST-HOC with propagation lag → **conferenceRecords polling is
NOT real-time** and is the wrong tool for "detect the join the instant it happens" (use
`--watch-join` or `huddle_watch.py`). An *unanswered* ring (no one joins) creates no record
at all.

## Hang-up detection — `huddle_watch.py` is the clean answer
A DM call posts a *message* into the Chat space whose annotation is a `MEET_SPACE`
rich-link carrying a `meetSpaceLinkData` block with `huddleStatus` — the call's lifecycle.
Terminal values: `MISSED` (callee never answered) / `ENDED` (connected then hung up). This
is the clean native-call-ended signal, needs only the bot token's `chat.messages.readonly`
scope, and is a supported pollable API. ⚠️ Latency: `huddleStatus` is a server-updated
annotation, so the ENDED transition lags the real hang-up by seconds. Both other channels
are dead ends for a native Chat call: the **Meet REST API** is BLIND to Chat-UI 1:1 calls
(`conferenceRecords` empty, `spaces.get` 400s — only bot-MINTED spaces are visible), and
the **browser network** only shows the hang-up indirectly (the clean roster-leave frame is
inside the `SyncMeetingSpaceCollections` server-stream Playwright can't read mid-stream).
`call_network_capture.py` is the diagnostic that established that last point.

### Inbound-flatline must be MUTUAL silence (the AI-call mid-answer drop, 2026-06-21)
The browser-side robust hang-up signal is inbound-RTP `bytesReceived` flatlining (the SFU
keeps the PC/tracks/roster alive after a leave, so only the media stops). That was tuned for
ONE-WAY use (inject a tone, callee listens): ~3s of inbound silence ⇒ hang-up. On a TWO-WAY
`gemini_call` conversation it false-fired **mid-answer** — while the callee LISTENS to the AI
their mic sends nothing, so "listening" looked identical to "left", and the call dropped ~3s
into the AI's reply. Fix: also probe OUTBOUND RTP (`_OUTBOUND_BYTES_FN` / `_webrtc_outbound_bytes`,
`outbound-rtp` `bytesSent` = the media WE send) and only count a poll as flat when NEITHER
direction grew (true mutual dead-air). New `--media-flatline-secs` (seconds of mutual silence;
default 3 for back-compat, `gemini_call` passes **30**); a clean hang-up is still caught fast by
the **roster-collapse** signal (`controls/tiles → 0`, window must be visible). Log line flipped
from "inbound media stopped — confirming hang-up" to "no media either way (in=… out=…) — watching
for hang-up (Ns)". Validated live: a ~90s conversation held through goodbyes and only stopped on
the real hang-up ("call controls disappeared"). The fully silence-immune signal remains
`huddle_watch.py` (REST `huddleStatus`, lags seconds).

## Audio: "AI ear" (capture) — the full investigation
`meet_audio_capture.py` (+ `meet_call_browser --capture-audio`) records the REMOTE voice
the bot hears to a WAV (**16 kHz mono s16le PCM** = exactly Gemini Live's realtime-input
format). `--audio-mode` picks the path:

- **`allsinks` (✅ RECOMMENDED for Meet, 2026-06-19) — record EVERY sink monitor, mix at
  stop.** One ffmpeg recorder per output sink (independent, NOT a single amix), merged with
  `amix normalize=0` on stop. The call's audio (ring/voice) can land on a DIFFERENT HDA
  sink between calls, so locking one sink can miss it; recording ALL can't. This is the
  exact config that captured the live remote voice (the call-4 diagnostic: −21 dB of speech
  for ~30s on sink 134 while 3 other sinks were silent). No routing change, operator still
  hears the call. Pair with `--capture-from-ring` to include the ringback. NOT yet
  live-verified end-to-end through the script (testing halted after call 5; the gating
  blocker is media-flow reliability on the occluded daily-Brave renderer, not the capture).
- **`monitor`** — record the sink Brave plays to (`_browser_output_sink_name`, app-aware),
  its `.monitor`. Simpler but locks ONE sink at start → MISSES the voice if it lands on a
  different sink than the ring (observed: call 1 captured the ring, lost the voice). Use
  `allsinks` instead.
- **`webrtc` (⚠️ DISQUALIFIED on Google Meet — use `allsinks`/`monitor`)** —
  `BrowserAudioTap` taps the inbound WebRTC audio track INSIDE the browser via the combined
  `_WEBRTC_HOOK` (MediaRecorder on an immortal AudioContext→MediaStreamDestination graph,
  base64 webm/opus chunks → `window.__audioChunks`, drained Python-side). Truncation-immune
  (each chunk tagged `"<frameId>:<gen>|<b64>"`, one standalone webm SEGMENT per
  (frame, recorder-generation), transcoded + concatenated at stop). 🔑 The decode-activation
  fix: a remote WebRTC track feeding a WebAudio source outputs SILENCE unless also sunk into
  a PLAYING muted `new Audio()` (Chromium lazy-decodes remote audio) — proven necessary AND
  sufficient by a Google-free loopback A/B (with-sink −9.1 dB vs stripped −91 dB). **But on
  Google Meet specifically it FAILS**: Meet renders remote audio through its own Web Audio
  path, exposing no tappable MediaStreamTrack/media-element, so the in-browser tap records a
  0-byte webm (−91 dB). Meet's DECODED audio IS on the OS output sink (you HEAR the call),
  so `monitor`/`allsinks` (record that sink's `.monitor`) is what actually captures the
  remote voice — live-verified −28.2 dB.
- **`isolate`** — null sink + MOVE the browser stream in + record its monitor. Clean in
  theory but fragile (depends on matching Brave's PulseAudio stream by app name) and mutes
  the call for the operator. A live run captured 37s of silence (the matcher never matched
  Brave's sink-input). Kept for a future per-tab isolation. (The null-sink MECHANISM is
  sound: a realtime paplay tone into it captured −25 dBFS; only the match step broke.)

⚠️ **PipeWire facts**: a sink monitor only carries audio while the sink is **RUNNING**,
which needs a **realtime** producer — a non-`-re` ffmpeg tone dumps its buffer and exits →
monitor records silence (a test artifact; Brave's live stream is realtime). All modes
record what the bot RECEIVES = the REMOTE voice, so the human must actually SPEAK on the
OTHER (Duc) device, else the WAV is (near-)silent. Capture is blind to the local mic (fine
— the bot has none). Standalone OS-tooling check: `python scripts/meet_audio_capture.py
--selftest`. `diag_call_sink3.sh` / `diag_call_sinks.sh` are the shell diagnostics that
pinpointed the call's sink.

## Audio: "AI mouth" (inject) + bidirectional bridge
- `meet_audio_inject.py` builds a virtual mic (`module-null-sink ai_mic_sink` +
  `module-remap-source ai_mic`), makes ai_mic the default capture source, and plays a file
  (or a generated 4-note test tone) into it with ffmpeg so the CALLEE hears it as the
  caller. Fully reversible (stop() restores the prev default + unloads modules,
  atexit-guarded). Standalone proof: `--verify` (records ai_mic → volumedetect).
- `ai_call.py` (**PROVEN + user-confirmed 2026-06-20**): launches a dedicated caller Brave
  with `--use-fake-ui-for-media-stream` so the mic is auto-granted (no allow click) and
  getUserMedia binds to ai_mic (set before the call). The callee heard the tone with no
  allow click and **no move-dance needed** — the default-source preset means the browser
  grabs ai_mic from the start (`move_browser_mic`'s "no browser mic → silence" log is a
  FALSE alarm on this path). Plain-launch (not Playwright) for login survival; leaves the
  browser running for reuse. Keep the window VISIBLE (native Wayland suspends an occluded
  renderer → call drops).
- `gemini_voice.GeminiVoiceBridge` wires TWO virtual devices: MOUTH (`ai_mic_sink` +
  `ai_mic` default source — Gemini's 24 kHz → ffmpeg → browser mic → callee) and EAR
  (`gemini_call_spk` null sink as default sink — browser plays the callee there → ffmpeg
  records its `.monitor` at 16 kHz → Gemini). **Greeting hardening (session 2):** the ear
  is GATED until the callee truly answers AND the opening is fully delivered, so the AI
  always speaks FIRST, uncut. `trigger_greet()` uses `send_realtime_input(text=…)` NOT
  `send_client_content` (the latter only seeds history on this model → slow greeting; the
  swap = first audio ~0.6s after pickup). Mouth ffmpeg has `-buffer_duration 80`.

## Greeting-latency root-cause fixes (session 3, 2026-06-20): ~15-22s → ~3s
Two root causes in `meet_call_browser`, both BEFORE the callee answers, neither
model/transport: a 5s meeting-code retry loop (now gated behind `--watch-rest`), and a ~38s
call-button double-click stall (the in-place DM call has no popup → the redundant 2nd click
stalled 30s on a detached element; now clicks ONCE). `--diag-pickup` logs elapsed-stamped
`[join]` / `[ring]` lines — the tool that found these. Fuller detail in
[`../MEMORY.md`](../MEMORY.md) "Greeting-latency root-cause fixes (session 3)".

## Caller-mode reality & the self-test blocker (2026-06-19)
Media stability is the live blocker for the unattended self-test:
- **GNOME-Wayland fully suspends an OCCLUDED Brave renderer** (the anti-occlusion launch
  flags are Wayland no-ops; you can't even programmatically occlude/minimize to test).
  Capture + hang-up only work while the renderer is AWAKE. **The fix: run the caller under
  Xvfb** (virtual display, no compositor → never occluded → renderer always awake →
  unattended) — this is `selftest_call.sh`'s default.
- Headed-Wayland connects-then-DROPS on occlusion; Xvfb/swiftshader NEVER connects. Voice
  capture is NOT yet live-verified non-silent end-to-end through the script.
- ⚠️ Kill stuck caller/callee Braves by PID (`/proc` scan), **never `pkill -f <profile>`**
  from an interactive shell (self-matches → SIGKILLs the shell). Full findings:
  [`../MEMORY.md`](../MEMORY.md) "Unattended call self-test" / "the full 5-call
  investigation".
