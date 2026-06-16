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
  `chat/` (base, google_rest, oauth, Phase-2 webhook stub), `rag/` (bm25, boost, chunk, fuse,
  store, optional dense), `agent/` (prompts, `state`·IssueStore, analyzer, report, staff). Entry
  scripts in `scripts/`; tests in `tests/` (+ `fakes.FakeChatClient`).
- **Run the tests** (offline, no key — the functional gate, currently **238 green**):
  `PYTHONPATH=src python -m unittest discover -s tests -t . -p "test_*.py"`.
- **Merged LLM calls (Lever 1, latency)**: detection emits opening
  `clarifying_questions` per issue and clarity emits the next `questions` batch
  inline, so the first-contact *detect→ask* and the *assess→ask* clarify step are
  each ONE round-trip, not two (the dominant per-cycle cost). The runner's `_ask`
  prefers these inline questions (`Issue.pending_questions` / `ClarityAssessment.
  questions`) and falls back to a dedicated `generate_questions` call only when the
  model returned none — so question quality never regresses. MockLLM emits both
  inline too (`_questions_from_missing`).
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
  `python scripts/run_staff.py --persona ops|promo --token <tok.json>` (staff);
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
