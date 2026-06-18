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
- **`run_webhook.py`** — webhook ingress entrypoint, **Phase-2 DEFERRED** stub.

The repo-root **`demo_live.sh`** wraps `run_poller.py` + `run_staff.py` into a
one-command LIVE end-to-end demo (seed an "API gateway timeout" incident → resolve
→ filed GitHub issue + voice DM); details in the root [`CLAUDE.md`](../CLAUDE.md).
