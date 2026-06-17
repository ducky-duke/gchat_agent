# gchat_agent — project memory

## What this is
A Google Chat "issue-spotter" AI agent **demo**, now **implemented** (`PLAN.md` is the design
doc it was built from). Three agents in one real Chat space: 2 LLM *staff* personas seed problems
+ answer questions, and 1 *bot* detects issues, asks clarifying questions until clear, then writes
a Markdown report. Code lives under `src/gchat_agent/`; `docs/google_chat/` holds the bundled REST
reference. Built in 4 ultracode workflow phases (foundation → offline impl → runner+tests → docs),
each gated by a full `py_compile` + `unittest` run and an independent Cursor cross-review.

## Code layout & commands
- **Package** (src layout): `src/gchat_agent/` — `config.py`, `models.py`, `runner.py`,
  `observability.py`; subpackages `llm/` (base, mock, openrouter+`build_llm`, tts+`build_tts`),
  `chat/` (base, google_rest, oauth, Phase-2 webhook stub), `github/` (base, rest+`build_github`),
  `rag/` (bm25, boost, chunk, fuse,
  store, optional dense), `agent/` (prompts, `state`·IssueStore, analyzer, report, staff). Entry
  scripts in `scripts/`; tests in `tests/` (+ `fakes.FakeChatClient`).
- **Nested indexes** (per-subtree maps, read the local one before touching files there):
  [`src/gchat_agent/CLAUDE.md`](src/gchat_agent/CLAUDE.md) (+ `agent/`, `llm/`, `chat/`,
  `rag/` subpackage indexes), [`tests/CLAUDE.md`](tests/CLAUDE.md),
  [`scripts/CLAUDE.md`](scripts/CLAUDE.md), [`docs/CLAUDE.md`](docs/CLAUDE.md). This root
  file keeps the cross-cutting behavioral specs; the nested files hold per-directory layout.
- **Run the tests** (offline, no key — the functional gate, currently **302 green**):
  `PYTHONPATH=src python -m unittest discover -s tests -t . -p "test_*.py"`.
- **Merged LLM calls (Lever 1, latency)**: detection emits opening
  `clarifying_questions` per issue and clarity emits the next `questions` batch
  inline, so the first-contact *detect→ask* and the *assess→ask* clarify step are
  each ONE round-trip, not two (the dominant per-cycle cost). The runner's `_ask`
  prefers these inline questions (`Issue.pending_questions` / `ClarityAssessment.
  questions`) and falls back to a dedicated `generate_questions` call only when the
  model returned none — so question quality never regresses. MockLLM emits both
  inline too (`_questions_from_missing`).
- **Skip in-thread re-detection (Lever B, latency)**: `detect_issues` (a full
  `DETECT_WINDOW_MESSAGES` tail through the frontier model) fires only when new
  foreign content landed OUTSIDE every open issue's threads — see
  `runner._should_detect` / `_open_issue_threads`. A pure clarification cycle (the
  reporter only answered inside an open issue's thread, its nudge thread, or its
  `active_thread_id`) skips detect entirely: that reply is `assess_clarity`'s job,
  not a re-detect that re-derives the same candidates. The detect window is a flat
  `tail(N)` across ALL threads (top-level + replies), so a new issue raised inside a
  clarification thread is only deferred, not lost — the next out-of-thread traffic
  re-detects over it while it's still in-window.
- **Background voice delivery (Lever C, latency)**: the resolve cycle was the
  slowest (~33s: assess + report + narration + TTS synth + MP3 upload, all
  serial). Now only `build_resolution_report` + the in-thread confirmation /
  RESOLVED / tombstone stay on the cycle's critical path; for
  `REPORT_DELIVERY=voice|both` the narration + TTS + upload (~17s) run on a
  background single-worker pool (`runner._deliver_voice_bg`, scheduled by
  `_submit_voice`, drained in `run_once`/`run_forever` finally). The reporter sees
  the ✅ at once; audio follows seconds later. The shared OpenRouter client's
  token counter is now lock-guarded (`openrouter._usage_lock`) since foreground
  detect/assess and background narration mutate it concurrently. The disk safety
  net moved into the worker: an *attempted* voice that fails writes the report to
  disk itself (so a resolution is never lost). Trade-off: the confirmation is
  posted before the voice outcome, so attempted-voice always uses the "recorded"
  wording even in the rare fail-to-disk case. Tests inject a synchronous
  `tests.fakes.InlineExecutor`; `tests/test_voice_report.BackgroundVoiceTest` pins
  the "issue closed before audio" contract.
- **GitHub issue export** (`GITHUB_ISSUES=true`): each resolved issue is also
  filed as a GitHub issue (report Markdown + the collected thread transcript) in
  `GITHUB_REPO`, building a durable searchable backlog. Like voice it runs OFF the
  resolve critical path — but on its OWN single-worker pool (separate service, no
  shared Chat rate limit) so a file never queues behind the ~17s voice pipeline:
  `runner._submit_publish` renders the `(title, body, labels)` in the foreground
  (only an immutable tuple crosses into the worker) and `_publish_issue_bg` POSTs
  it; both pools are drained by `_drain_background` in `run_once`/`run_forever`.
  Best-effort: a transport/credential failure is logged and swallowed (GitHub is an
  extra sink, not the system of record — the confirmation and disk report already
  hold the resolution). The `github/` subpackage mirrors `chat/`: a `GitHubClient`
  Protocol + stdlib-`urllib` `GitHubRestClient` (`create_issue`, 422-unknown-label
  tolerant: retries once without labels). `build_github` resolves the token from
  `GITHUB_TOKEN`, else `gh auth token --user GITHUB_ACCOUNT` (so the demo files
  under a pinned account — e.g. `ducky-duke` — without a secret in `.env` or
  switching the machine's active gh login); returns `None` (export skipped) when
  off or tokenless. Labels are a fixed pre-created set (`auto-filed`,
  `severity:<low|med|high>`) so a create never 422s. Payload rendering lives in
  `report.render_chat_transcript` / `render_github_issue`. Tests:
  `tests/test_github_export.py` (`FakeGitHubClient`; off-critical-path + never-crash
  contracts).
- **No duplicate questions (loop-breaker)**: the bot never re-asks a question the
  reporter can't answer. Two layers: (1) `clarity_prompt`/`questions_prompt` show
  the model every already-asked question (`prompts._asked_block`) and instruct it
  to never repeat one and to treat a declined fact ("I don't know") as
  unobtainable — drop it, resolve with the gap. (2) A deterministic runner guard
  in `_step_issue`: when the reporter replied but the gap stayed *identical* (same
  `missing_info` two rounds running), it closes *with gaps* instead of re-asking —
  on a decline that made **no progress** (`runner._looks_like_decline`), else after
  `MAX_NO_PROGRESS_ROUNDS` (default 2, via `Issue.no_progress_rounds`/`last_missing_info`).
  **A decline never closes while the reply made progress** — the gap shrank, or this
  round surfaced NEW core facts (owner, root-cause) the reporter was never asked.
  "I don't know" then means "I can't answer THESE asked questions", not "I know
  nothing about the issue", so the bot pursues the still-unasked facts first (the
  screenshot bug: "API timeout" → asked env/endpoints/logs → reporter "it's in
  production, else I don't know" → bot wrongly closed with never-asked owner/root-cause
  as open questions). `_looks_like_decline` only fires on a reply that is *essentially
  the refusal* (phrase + padding); a partial answer ("it's in production. Else I don't
  know") is NOT a decline. A gap-close routes through `_resolve(..., gaps=...)` →
  `ResolutionReport.open_questions` → an "Open questions" report section + an honest
  "recorded with open questions" confirmation. Regression: `tests/test_duplicate_questions.py`
  (`DeclineDoesNotAbandonUnaskedFactsTest`).
- **Self-filtering (no self-loop)**: detection drops the bot's OWN messages via
  `_detect`'s `without_sender(own_id)` (never a `sender_type` rule — staff post as
  HUMAN). `own_id` is `chat.me()`, resolved in precedence: configured
  **`GOOGLE_BOT_USER_ID`** (optional override; `users/<id>` or bare id, normalized
  by `runner._normalize_user_id`, seeded via `build_runner`) → persisted `.state/`
  → **OAuth tokeninfo** (`GoogleChatClient._resolve_self_id`: `sub` == the Chat
  user id, one cached lookup on first `me()`, needs only the already-granted
  `userinfo.email` scope) → learn-from-first-post. The tokeninfo step is what makes
  self-filtering work from **cycle 1 on a fresh start** with no pin and no posting
  (the original bug: `me()` was `None` on cycle 1 → bot clarified its own messages).
  Runner logs `bot self-id resolved: …` (stderr, once) when not pinned; poller
  banner's `self:` line shows pinned vs auto-detect. Tests:
  `tests/test_self_filter.py`. The bot account here is the user's own
  `mikmikb26@gmail.com` (`users/116566195804326411461`), so it shows as the same
  person ("Tran Duc") for both posts and bot replies.
- **Voice reports** (`REPORT_DELIVERY=voice|both`): a resolved issue is narrated to a concise
  spoken script (`report.build_narration`) → TTS (`llm/tts.py`, OpenRouter `audio.speech`, default
  `x-ai/grok-voice-tts-1.0`) → posted as an in-memory MP3 attachment to `GOOGLE_VOICE_SPACE` (a
  separate space/DM; empty ⇒ in the issue thread) via `chat.post_voice` (`media.upload` multipart +
  `attachment` create, both on the bot's `chat.messages` user-OAuth scope — no service account).
  Best-effort: any failure falls back to the on-disk report. Demo offline: `demo_local.py --voice`
  (saves MP3s to `reports/demo/`). `TTS_VOICE` is model-specific — override in `.env` if rejected.
  ⚠️ It's an audio **file attachment**, NOT a native voice message (waveform bubble) — that's a hard
  Chat-API ceiling for bots, settled 2026-06-15; format is irrelevant — mp3/m4a/ogg AND an exact
  webm/opus + `UserRecording_*.webm` match all render as file cards. See [`MEMORY.md`](MEMORY.md)
  "Voice reports = audio FILE attachment only". Because the attachment is a download-only card, the
  voice message body now **also carries the spoken transcript** (`report.voice_message_text` =
  caption + `build_narration` text), so the report stays readable in-thread without playing the file.
- **Offline path**: `LLM_PROVIDER=mock` → MockLLM, no network/key. Live path needs `OPENROUTER_API_KEY`
  **and** `pip install openai` (lazy core dep — NOT auto-installed in `igaming`; do it once: `conda run -n igaming pip install openai`).
- **Scripts** self-add `src/` to path: `python scripts/run_poller.py [--once]` (bot);
  `python scripts/run_staff.py --persona ops|promo|apigw --token <tok.json>` (staff;
  `apigw` = the "API gateway timeout" incident scenario, added for the live demo);
  `python scripts/authorize.py --client <client.json> --out <tok.json> --account <email>` (mint a
  per-account refresh token); `python scripts/demo_local.py [--persona ops|promo|both] [--max-rounds N]`
  (full loop end-to-end over the in-memory FakeChatClient with the live/.env LLM — **no Google needed**;
  reports → `reports/demo/`). Docs: `README.md`, `docs/{ARCHITECTURE,RAG_ANALYSIS,SETUP_GOOGLE_CHAT}.md`.
- **`./start_bot.sh`** is the convenience launcher for the poller (`--once` passthrough). A **fresh
  start is the DEFAULT**: every launch resets previous-session state first — deletes `.state/`
  (IssueStore + poll cursor → bot re-scans the Space from the top on a live run) and *archives*
  (moves, never deletes) old reports to `reports/_archive/<ts>/` (keeping `voice_report_sample.mp3` +
  `.gitkeep`). Pass **`--continue`** to skip the reset and resume the previous session (keeps `.state/`
  + reports). It never touches `data/` (RAG corpus / input, not session state); there are no on-disk
  logs or RAG index to clear (logs → stdout, index rebuilt in memory each start). `--fresh` is accepted
  as an explicit no-op. Combinable: `./start_bot.sh --continue --once`.
- **`./demo_live.sh`** is the one-command **LIVE end-to-end demo** (the GitHub +
  voice showcase): it preflights `.env`/tokens/`gh`, resets `.state/`, snapshots
  the GitHub issue baseline, starts the poller (the bot) **first** so its cursor
  pins to *now*, then starts a staff persona (default `apigw` — the "API gateway
  timing out, 504s in prod" incident, posting as the ops account's token) that
  seeds the incident and answers the bot's questions. It WATCHES until a brand
  new issue appears in `GITHUB_REPO` (server-side proof) and the bot log confirms
  the voice DM, prints the issue URL + voice destination, then tears both
  processes down with SIGINT (clean lock release + background drain). Flags:
  `--persona ops|promo|apigw`, `--timeout <s>` (default 600), `--token <tok.json>`,
  `--keep-running`. Relies on the bot's success logs `filed GitHub issue for …`
  and `posted voice report …` for confirmation. **Re-runnable** against the same
  space: it passes a fresh per-run `--seed-suffix` to `run_staff.py` so the staff's
  seed/answer `request_id`s are unique each run (otherwise Chat would dedup them to
  the prior run's old messages, which the no-backfill bot never re-detects).

## Memory
Accumulated findings, setup gotchas, and the smoke-test environment live in
[`MEMORY.md`](MEMORY.md) — read it before touching auth, OAuth, or the smoke tooling. Key facts:
- **The real demo ran end-to-end (2026-06-14)**: 3 personal Gmail accounts in one live Space
  (`spaces/AAQApcq1--E`, bot + 2 staff, model grok-4.3), **both issues resolved in ~192s**. The live
  path exposed 3 bugs offline tests couldn't (`.env` empty-value-comment parse, `orderBy=ASC`,
  quota-header 403) — all fixed. ⚠️ **`GOOGLE_QUOTA_PROJECT` must be BLANK** for multi-account user
  OAuth (staff are test users with no `serviceusage` IAM role; `x-goog-user-project` isn't required).
  See [`MEMORY.md`](MEMORY.md) "LIVE 3-account Google Chat run".
- **Auth is validated**: personal @gmail.com accounts drive the Chat REST API via user OAuth
  (3 accounts in 1 Space, 1 bot + 2 staff; no service accounts / app auth / Workspace).
- **Setup has hard-won gotchas**: own OAuth client (Desktop), a dormant Chat app config is
  mandatory, Testing-mode refresh tokens expire after 7 days, browser account ≠ gcloud account.
- **Environment**: gcloud `mikmikb26@gmail.com`, GCP project `chat-smoke-1781346315`; glo.com is
  revoked — do not reintroduce it.
- **Live demo resolves both issues** (`scripts/demo_local.py --persona both`): required softening the
  `clarity_prompt` to a bounded per-issue CORE-facts checklist (the old "every fact / nothing missing"
  bar never let a chatty model reach `is_clear`, so everything staled — would hit the real run too) +
  scenario-data fixes. Details in [`MEMORY.md`](MEMORY.md) "Clarity bar was too strict".
- **Model-portability is hardened across deepseek/glm/minimax/grok** — each model broke a different
  assumption (cited-id format, empty replies, output doubling, fp8 quantization 404, no timeout). Run
  with `conda run --no-capture-output -n igaming python -u ...` (else `conda run` buffers and a crash
  looks like a hang). Full list in [`MEMORY.md`](MEMORY.md) "Model-portability hardening".
- **Out-of-thread safety modes** (for a shared space with non-staff/leadership): `REQUIRE_IN_THREAD_REPLY`
  (strict demo floor — ignore replies outside the issue thread) and `REDIRECT_OUT_OF_THREAD_REPLY`
  (production "redirect-on-capture" — collect the outside reply as evidence-only and post one templated,
  LLM-free in-thread nudge; never resolves/leaks on it). Both gate *source B* only; design + leak-safety
  invariants in [`MEMORY.md`](MEMORY.md) "Out-of-thread safety modes".

## goclaw-inspired hardening
A batch of low-risk hardening ported from a review of the `goclaw/` Go platform
(all pure-stdlib, gated by the offline suite — now **302 green**; tests in
[`tests/test_goclaw_hardening.py`](tests/test_goclaw_hardening.py)):
- **Observability is actually wired**: `@observe` (was applied to nothing) now
  decorates the 5 LLM boundaries — `analyzer.{detect_issues,assess_clarity,
  generate_questions}` + `report.{build_resolution_report,build_narration}` — so a
  live `OBSERVABILITY=langfuse` run gets a span per call. No-op/zero-import on the
  default path. **Self-hosted Langfuse v2** is wired + verified (client must be
  `langfuse<3`; `.env→os.environ` bridge in `_seed_langfuse_env`; host =
  `localhost:3000`) — version-matching + setup gotchas in [`MEMORY.md`](MEMORY.md)
  "Self-hosted Langfuse observability".
- **Token usage**: `OpenRouterClient`/`MockLLM` accumulate `usage_snapshot()`
  (calls + prompt/completion/total tokens; Mock estimates ~chars/4). The runner
  diffs it per cycle and adds `tokens` to the cycle summary + log.
- **`config.validate_config()`** fails fast at load on a bad enum
  (`LLM_PROVIDER`/`OBSERVABILITY`/`REPORT_DELIVERY`) or out-of-range number
  (threshold, intervals, ports). Called by `load_config` AND `build_runner`. Does
  NOT check the API key — that stays in `build_llm`/`build_tts` so the keyless
  `mock` path is always valid.
- **Retry hardening** ([`llm/_retry.py`](src/gchat_agent/llm/_retry.py), shared by
  openrouter+tts; google_rest mirrors it): honor a server `Retry-After` header
  (both `exc.response.headers` and bare `exc.headers` shapes; integer-seconds only,
  HTTP-date → backoff), add full **jitter**, and a **cross-cycle** exponential
  backoff in `run_forever` (consecutive-failure count → `min(interval*2ⁿ, 300s)`),
  replacing the flat sleep that hammered a dead endpoint.
- **Episodic recall** (`EPISODIC_RECALL`, default on, self-gating): detection is
  shown the few most recently closed issues (`IssueStore.recent_closed`) as a
  `prompts._prior_issues_block` — `#`-stripped so it stays inert for MockLLM
  detection (which only flags `#<id>` lines). Detection-only; clarity is left
  untouched so the MockLLM owner/date/number heuristic stays deterministic.
- **Prompt-injection guard**: `_ROLE` + `_render_user` mark the transcript and
  retrieved context as UNTRUSTED data, never instructions (defense for a bot
  ingesting arbitrary staff messages).
- **Report-only secret redaction** (`REDACT_REPORTS`, default **OFF**):
  `report.redact_secrets` masks high-confidence secrets (bearer/sk-/AIza/JWT) in
  the on-disk report only — never the LLM input path; conservative so ticket/
  message ids survive. Belt-and-suspenders (the bot never logs auth headers).
- **State `.bak`**: `IssueStore.save` copies the last-known-good state to
  `<state_file>.bak` before `os.replace` (one-deep rollback). `_issue_query` also
  blends the reporter's latest reply into the RAG query.

## Tooling / conventions
- Python: `igaming` conda env (3.14). **`ty` is NOT installed here** → syntax-check with
  `python -m py_compile <file>`; tests via stdlib `unittest`.
- Pure stdlib everywhere except the **lazy-imported** `openai` (core LLM transport) and optional
  `langfuse`/embeddings extras. The Google Chat + OAuth path is stdlib `urllib` (ported from `smoke/`).
- Secrets are gitignored: `client_secret*.json`, `*.apps.googleusercontent.com.json`, `smoke/.token`,
  `*.token`, `token_*.json`, `secrets/`, `.env`, plus generated `reports/` + `.state/`. Not a git repo yet.
- For interactive logins / browser consent, the user runs the command (e.g. `! gcloud auth login`)
  — Claude can't click OAuth consent screens.

## Harness lessons
- **Cursor parallel relay races on simultaneous launch.** Launching both `cursor-agent` models at
  once hit a `.cursor/cli-config.json.tmp` rename ENOENT (killed one), and a `pgrep -f "model X"`
  wait-loop **self-matched its own command line** and deadlocked. Fix: stagger the two launches
  (`sleep 5` between) and use the shell's `&` + `wait` builtins only — never a pgrep/ps wait-loop.
  (Detail + other build lessons in [`MEMORY.md`](MEMORY.md).)
