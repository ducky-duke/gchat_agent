# gchat_agent — project memory

Accumulated knowledge for this project: findings, setup gotchas, and environment
facts. `CLAUDE.md` is the lean index; this file holds the detail. Keep new
findings/lessons here and leave a one-line pointer in `CLAUDE.md` if discoverability needs it.

## VALIDATED auth design — personal Google accounts via user OAuth (smoke-tested 2026-06-13)
**Consumer @gmail.com accounts CAN drive the Google Chat REST API via user OAuth.** Proven
end-to-end with `mikmikb26@gmail.com` (all HTTP 200): list spaces, create a `SPACE`
(`spaceThreadingState=THREADED_MESSAGES`), post a message, list messages. Tooling in `smoke/`.

- **Design = 3 personal Gmail accounts in one Space, each driven by its own user-OAuth refresh
  token** (1 bot + 2 staff). NO service accounts, NO app auth, NO admin approval, NO Workspace,
  NO incoming webhooks. This replaced the earlier app-auth / 3-Chat-apps / webhook ideas.
- Posts are attributed to the **user** (`sender.type = HUMAN`, `sender.name = users/<id>`),
  **text-only**. So the bot's "everyone but me" filter = drop only the bot's own `users/<id>`.
- Distinct staff need distinct accounts (user-auth posts carry the account identity, no per-message
  display name). 3 accounts for a clean demo; could collapse to 2 (1 bot + 1 staff) if needed.

## Setup gotchas (hard-won — put these in SETUP_GOOGLE_CHAT.md)
1. **gcloud's built-in OAuth client is BLOCKED from Chat scopes** ("This app is blocked"). Must
   create your **own OAuth client (Desktop type)** in a personal GCP project. `smoke/get_token.py`
   runs the loopback flow against it and mints the token.
2. **The GCP project must have a Chat app configured** in Cloud console → *Chat API → Configuration*
   tab (app name + avatar + description). Without it, every Chat API call — even user-auth read —
   returns `404 "Google Chat app not found"`. One-time; the app config is **dormant** (we never use
   app auth or interactivity).
3. OAuth consent screen in **Testing** mode → **refresh tokens expire after 7 days**; each of the 3
   accounts must be added as a **test user**. Re-consent weekly for a demo, or publish the app.
4. **Browser account ≠ gcloud account.** Console + the OAuth consent run as the *browser's* Google
   account. Use an Incognito window signed into the target Gmail, or append
   `authuser=<email>` to console URLs.
5. User-OAuth token refresh is hand-rolled over `urllib` (see `smoke/get_token.py`) — **no
   `google-auth` needed** for the user-auth path, so the core stays zero-dep.

## Current smoke-test environment (personal, NOT glo.com)
- gcloud account: `mikmikb26@gmail.com`. **glo.com (`dttran@glo.com`) was revoked** from gcloud +
  ADC on 2026-06-13 at the user's request — do not reintroduce it.
- GCP project: `chat-smoke-1781346315` (owner mikmikb26); Chat API enabled + Chat app configured.
- OAuth client JSON: `client_secret_*.apps.googleusercontent.com.json` in repo root (**gitignored**).
- Throwaway test space created during smoke: `spaces/AAQAvDklGmc` (delete in Chat UI, or reuse).
- `smoke/get_token.py` → mints a user token to `smoke/.token` (refuses any glo.com token);
  `smoke/smoke_test_chat.py` → exercises read/write; see `smoke/README.md`.

## Build (implemented 2026-06-13, via ultracode workflows)
Full agent built under `src/gchat_agent/` (layout + commands in `CLAUDE.md`). Green:
`PYTHONPATH=src python -m unittest discover -s tests -t . -p "test_*.py"` → **162 tests pass**
offline (MockLLM + `tests/fakes.FakeChatClient`, no network/key). Built in 4 sequential workflow
phases, each with an independent Cursor (gpt-5.5-extra-high + composer-2.5) cross-review whose
findings were verified against source before applying.

Changes made during the build that **refine/diverge from `PLAN.md`** (PLAN may lag — trust the code):
- **models**: typed `QAPair`; top-level `AgentState` (poll cursor + `seen_message_ids` + `issues`
  + resolved/stale `tombstones` + `bot_user_id`); `Conversation` helpers (`tail`/`for_thread`/
  `without_sender`/`after`; `render(with_ids=True)` prefixes `#<id>` so the LLM can cite source ids);
  normalized category in `issue_fingerprint`; safe bool/float coercion in `from_dict`.
- **LLM JSON**: `extract_json` rejects bare arrays; `extract_json_value` parses object OR array;
  detection uses the `{"issues":[...]}` wrapper; prompt task markers (`TASK:detect_issues`, …) let
  MockLLM branch deterministically.
- **config.py**: `.env` parsing strips inline `# comments` + quotes (else `.env.example` copies corrupt values).
- **chat**: `google_rest`/`oauth` are stdlib `urllib` (ported from `smoke/`); guarded success-path
  JSON + `expires_in`; **401 → drop cached token + retry once** (`oauth.invalidate`); `me()` seeded
  from persisted `bot_user_id` so self-filtering survives a restart.
- **runner**: stale transitions **tombstone** (not just resolve, else stale issues re-detect);
  anti-spam `_new_replies` returns `[]` unless the bot's question is anchored in the working view;
  resolve is **crash-idempotent** (gated on `report_written_at`; file-write skipped if present;
  confirmation posted via stable `request_id`); `_since` prefers the persisted cursor over
  `POLL_BACKFILL_SINCE` and is widened by a 2s skew so equal-`createTime` messages aren't dropped
  (seen-set dedups the replay); single-runner lock; atomic state save. `state.load()` falls back to
  fresh on a malformed-but-valid-JSON file.

## Live LLM run — validated 2026-06-14 (OpenRouter)
First real-model run, end-to-end, no Google Chat (in-memory `FakeChatClient`):
- **`openai` is NOT in the `igaming` env by default** — it's the lazy core dep, so the
  offline 145-test gate never imports it. Installed `openai 2.41.1` once
  (`conda run -n igaming pip install openai`). Without it the live path raises `ModuleNotFoundError`.
- Model `deepseek/deepseek-v4-flash` + the user's key work: `complete_json` round-trips clean
  JSON, and the real `Analyzer` detect→assess_clarity→generate_questions contracts all parse live.
- **New script `scripts/demo_local.py`** drives the *real* Runner + Analyzer (live RAG over the KB)
  + StaffAgent personas over one shared in-memory space (each participant a distinct `users/<id>`,
  via a local `StaffChatView` copied from `tests/test_loop.py`). The live LLM drives both bot and
  staff. Reports land in `reports/demo/` (wiped per run); state in a throwaway temp dir (so detection
  always re-fires). `--persona ops|promo|both`, `--max-rounds`, `--max-cycles`.
- **Resolution needs enough clarify rounds.** deepseek asks ~3 questions/round but personas reveal
  **one fact per reply**, so an issue needs ~6-8 rounds; at `MAX_CLARIFY_ROUNDS=3` it caps out and
  goes **stale**. `.env` is `MAX_CLARIFY_ROUNDS=8` (config default stays 3).

## Clarity bar was too strict for a live model — fixed 2026-06-14 (`--persona both`)
The single biggest gap surfaced by the *two-staff* live run: **issues never RESOLVED, they all went
STALE**, even after every persona fact was revealed. Root cause was the `clarity_prompt`, not the loop.
The resolve gate (`runner.py`) is `is_clear AND confidence>=RESOLVE_CONFIDENCE_THRESHOLD AND not
missing_info` — all three. The old prompt said clear "only when **every** fact needed is present /
nothing material is still missing," which a chatty model (deepseek) reads as licence to chase endless
detail (status-page checks, severity labels, conn-pool %, exact timestamps): `is_clear` never flips,
`missing_info` is never empty, so nothing resolves and it idle-stales. **This would hit the real
Google Chat run identically.** Fixes that made *both* issues resolve with rich reports:
- **`prompts.py` `clarity_prompt` → a bounded, per-issue-type CORE-facts checklist** (owner; for an
  incident/bug: scope+impact/numbers+root-cause-or-fix-plan; for a request: start+end dates+audience+
  key terms; ticket if mentioned) and an explicit "do NOT hold open for peripheral nice-to-haves
  (extra diagnostics, status-page, exhaustive metrics, exact times, severity label) — those are
  optional follow-ups." Bounded ⇒ resolves reliably *and* the report stays substantive.
- **`.env` `RESOLVE_CONFIDENCE_THRESHOLD` 0.8 → 0.75** for headroom on the `confidence>=thr` leg.
- **`data/scenarios.json` scenario-data bugs** (these are demo-data, not loop, bugs): ops `deadline`
  date `2026-06-13`→`2026-06-14` (a *past* date made deepseek spawn a phantom "timeline inconsistency"
  3rd issue that siphoned answers and staled); promo `owner` was a *deflection* ("engineering needs to
  assign someone") → concrete ("Raj on the bonus-platform team") so the bot's repeated "who's the eng
  owner?" is satisfiable; promo `deadline` now carries **both** go-live AND end dates (2026-06-19 →
  06-28) — a promo with no end date is genuinely under-specified, so the bot rightly refused to close.
- Result: `--persona both --max-rounds 10` → **ops [high] resolved ~6 rounds, promo [med] resolved
  ~8 rounds**, each with a full Summary/Resolution/Q&A report in `reports/demo/`. 145 offline tests
  still green after the prompt edit (mock branches on the `MARK_*` token, not the prose).
- **Detection fire-time is nondeterministic** with the live model: identical seed transcript, yet the
  first non-empty detect landed at cycle 1, 3, 6, or 13 across runs (deepseek returns `{"issues":[]}`
  for the same input some cycles). Harmless locally (use a generous `--max-cycles`, e.g. 30), but the
  **real poller would idle a random ~0-3 min before first detection** at `POLL_INTERVAL_SECONDS=15` —
  expected, not a bug. The detect prompt says "Be conservative"; don't make it aggressive (false +ves).

## Model-portability hardening — validated deepseek / glm / minimax / grok (2026-06-14)
Swapping `OPENROUTER_MODEL` across `deepseek-v4-flash`, `z-ai/glm-5.1`, `minimax/minimax-m3`, and
`x-ai/grok-4.3` surfaced a chain of latent bugs — **each model breaks a different fragile assumption.**
All fixed model-agnostically (162 offline tests green; `demo_local.py --persona both` now resolves
BOTH issues on deepseek, minimax, and grok). Lessons by symptom:
- **Cited-id format varies by model → issues silently dropped ("0 detected" forever).** `Analyzer.
  _build_issue` matched the model's `source_message_ids` against real ids by exact string. deepseek
  cites the full id (ok); **glm copies the transcript's `#<id>` marker verbatim**; **minimax cites only
  the trailing segment** ("m1" for "spaces/FAKE/messages/m1"). Any mismatch → no source ids → issue
  dropped. Fix: `Analyzer._resolve_cited_id` resolves by exact / `/<cited>` suffix / last-segment
  match; the prompt also now says cite WITHOUT the leading `#`. (Tests in `test_analyzer.py`.)
- **Reasoning models intermittently return empty `content` → crash.** minimax occasionally returns ""
  (turn budget spent on reasoning); `extract_json("")` raised and killed the runner mid-cycle. Fix:
  `OpenRouterClient.complete_json` retries once on empty and **never raises** (degrades to `{}`; callers
  already handle that). (Tests in `test_llm_openrouter.py`.)
- **One empty questions reply staled a healthy issue.** `_step_issue` staled immediately when
  `generate_questions` returned nothing; with a flaky model a single transient empty killed a
  progressing issue. Fix: a no-questions cycle counts as **idle and retries**, only staling after
  `STALE_AFTER_IDLE_CYCLES`. (Test in `test_runner_hardening.py`.)
- **Some models double their output.** minimax echoes the staff reply twice ("X.X."); `StaffAgent.
  _dedupe_repeat` collapses a verbatim double so transcripts/reports stay clean.
- **`OPENROUTER_QUANTIZATIONS` default "fp8" 404s most models.** The config default hard-pinned fp8
  provider routing; grok-4.3 has no fp8 endpoint → `404 No endpoints found ... quantization: fp8`.
  Default is now `""` (auto-route); pin via `.env` only when needed.
- **No request timeout → a hung call blocks ~10 min.** Added a 90s per-request timeout +
  `max_retries=2` on the OpenAI client (`_REQUEST_TIMEOUT` in `openrouter.py`).
- **Speed: reasoning OFF for these tasks.** Reasoning models burn tens of secs/call; the detect /
  clarity / questions JSON tasks don't need it. `.env` now `OPENROUTER_REASONING=false` (config default
  stays True). deepseek-v4-flash is fast either way; minimax/grok need it off to be usable.
- **`conda run` buffers stdout — a fast crash looks like a 14-min hang.** It captures and flushes only
  on exit, so the fp8 404 read as a long hang. Use `conda run --no-capture-output -n igaming python -u
  scripts/demo_local.py ...` to stream progress live.

## LIVE 3-account Google Chat run — validated 2026-06-14 (the real demo)
First true end-to-end on **real Google Chat**: 3 personal Gmail accounts (bot=`mikmikb26`, ops, promo)
in one Space `spaces/AAQApcq1--E`, each via its own user-OAuth refresh token, LLM = `x-ai/grok-4.3`.
Bot poller + both staff (`run_poller.py` / `run_staff.py`) — **both issues resolved in ~192s**, real
reports written (`reports/issue-*.md`) citing real `spaces/.../messages/<thread>.<id>` source ids.
The live path exposed **three bugs that offline/local testing could never catch** — all now fixed:
- **`.env` parser leaked an inline comment as the value (HTTP 400).** `KEY=<spaces># comment` (empty
  value + comment, e.g. `POLL_BACKFILL_SINCE`) collapsed to a leading-`#` literal because `_clean_value`
  stripped *before* checking for the comment, so the `i > 0` guard kept the whole comment. The bot then
  sent that comment text as the Chat API `createTime >` filter → 400. Fix: detect leading-whitespace-then-`#`
  BEFORE stripping → empty. Also silently fixed `WEBHOOK_AUTH_AUDIENCE`. (Tests: `tests/test_config.py`.)
- **`orderBy=ASC` is invalid (HTTP 400).** `spaces.messages.list` wants a field+direction —
  `createTime asc`, not a bare `ASC`. Fixed in `google_rest.fetch_messages`.
- **Quota-project header 403'd the staff (PERMISSION_DENIED / USER_PROJECT_DENIED).** `x-goog-user-project`
  makes Google check `serviceusage.services.use` on that project for **every calling account**; the staff
  Gmails are only OAuth *test users* with no IAM role → 403 on their first POST. The bot only worked because
  it **owns** the project. **The header is NOT required** — a read test confirmed bot+ops+promo all succeed
  without it (quota falls back to the OAuth client's own project). Fix: **`GOOGLE_QUOTA_PROJECT` is now
  BLANK** in `.env`. ⚠️ This REVERSES old Gotcha 4 ("set the quota project") — for the multi-account user-OAuth
  demo, leave it empty (or else grant each staff account `roles/serviceusage.serviceUsageConsumer`).

## Out-of-thread answer capture + escalation — added 2026-06-15
The clarify/resolve loop was strictly keyed on `issue.thread_id`: a reporter who kept chatting at the
space top level (each top-level message opens a *new* thread) was invisible — the bot's question idled
straight to STALE even when answered. Two features fixed this (design cross-reviewed by Cursor
gpt-5.5 + composer-2.5; full behavior in `docs/ARCHITECTURE.md` §3). Durable decisions worth not
re-deriving:
- **`Issue.reporter_id`** (sender of `root_message_id`, set in `analyzer._build_issue`, persisted +
  backfilled in `IssueStore._merge`) drives BOTH features: the @mention target and the out-of-thread
  author filter. Needed because after a restart the working view (unseen-only) no longer holds the root.
- **Re-tag, don't change the analyzer.** `analyzer.assess_clarity` re-scopes to `for_thread(issue.thread_id)`
  and `test_analyzer.py::test_clarity_scoped_to_issue_thread` PINS that. So `Runner._effective_conversation`
  collects the reporter's out-of-thread messages as **copies re-tagged to the issue thread** (via
  `dataclasses.replace`); originals in `self._conversation` are untouched. The analyzer stays unchanged.
- **Follow-the-reporter posting (refined 2026-06-15).** `Issue.active_thread_id` = the REAL thread of the
  reporter's latest reply (set in `_step_issue` by mapping the latest reply's id back through
  `self._conversation`, since the effective view's copies are re-tagged). `_post_to_thread`/`_thread_anchor`
  target `active_thread_id or thread_id` — so the next question AND the confirmation land wherever the
  reporter answered (issue thread, nudge thread, or anywhere). NOTE this intentionally REVERSES the earlier
  "always post to the canonical issue thread" decision; the Cursor safety invariant still holds — the anchor
  is resolved from `self._conversation` (a REAL message), never a re-tagged effective-view copy, so a copy
  can never redirect a post.
- **Two out-of-thread sources, by confidence (refined 2026-06-15 — "collect issue thread OR nudge thread").**
  `_effective_conversation` collects in two passes: **(A) home threads** = `{escalation_thread_id,
  active_thread_id}` (the nudge thread + the thread the convo moved to) — each is 1:1 with the issue, so
  *any* non-bot reply there is pulled in **unconditionally — bypassing the ambiguity guard**; **(B)** a
  reporter reply in some *other* fresh thread is guarded. This fixes a real gap: before, a reply in issue
  A's nudge thread hit the ambiguity guard and was dropped whenever the reporter had ≥2 open issues, even
  though the nudge thread is unambiguous. The bot's own posts (sender == own_id) are filtered out.
- **Ambiguity guard, not message-claiming (for source B only).** If the reporter has >1 open issue awaiting a
  reply, a *bare* top-level message can't be attributed → fall back to issue-thread + nudge-thread replies,
  which are unambiguous. Chosen over per-message claiming because claiming prevents double-capture but can
  still MIS-attribute (issue B's answer → issue A's report). Source B is **reporter-only** (staff reply
  in-thread; widening ingests their chatter); source A is any-non-bot (the nudge thread is the issue's home).
- **Out-of-thread safety modes (added 2026-06-15; Option-3-lite "redirect-on-capture"; Cursor gpt-5.5 +
  composer-2.5 reviewed).** Two config flags gate **source B only** (never source A), via the shared
  predicate `_source_b_feeds_resolution()`; the candidate selection is the shared
  `_out_of_thread_reporter_messages()` (same guards as before). `REQUIRE_IN_THREAD_REPLY` drops B
  entirely — the strict, predictable **demo floor**. `REDIRECT_OUT_OF_THREAD_REPLY` is the **production**
  design: B never resolves, never feeds the clarity/question LLM, never enters `qa`/report/voice, never
  moves `active_thread_id` — instead the runner records its message **ids only**
  (`Issue.out_of_thread_evidence_ids`) and posts ONE templated, **LLM-free** nudge **pinned to
  `issue.thread_id`** (never `active_thread_id`, so it can't be dragged into the unrelated thread)
  asking the reporter to confirm in-thread. One-shot (`Issue.redirect_nudged` + stable
  `request_id=client-issue-{id}-redirect`); it advances `last_bot_*` so the in-thread confirmation is
  then a normal reply, and resets `idle_cycles` so it precedes escalation in the idle branch (no
  double-nag). **Leak-safe by construction:** the nudge names only the reporter @mention + the issue
  **title** (derived from the ORIGINAL in-thread report, exactly like the escalation nudge) — the
  out-of-thread text is never quoted/paraphrased/fed to an LLM. The posting fallback now also checks the
  anchor's thread matches the pinned target (`_thread_anchor(issue, target)`). Why NOT an LLM "verify"
  that aggregates evidence: both Cursor models flagged that any evidence-in-context LLM verify
  paraphrases/leaks the outside text — so the verify is "reply in thread", deterministic, not a second
  LLM pass. Tests: `RedirectOnCaptureTest` (no-resolve+one-shot, leak-safe, ambiguity-suppressed,
  home-A intact, in-thread still resolves).
  - **Cross-review follow-up (2026-06-15, gpt-5.5 + composer-2.5 on the impl).** Both: I1–I6 HOLD with a
    flag on a *clean* issue; 228→**230 green** after two hardenings. **Scope of "leak-safe" — important:**
    the flags gate `_effective_conversation` (the per-issue clarify/RESOLVE view) only, **NOT detection**.
    `_detect` scans the whole recent window (space-wide, bot-filtered) — inherent, that's how issues are
    found — so off-thread text always reaches the *detection* LLM. gpt-5.5 found the one residual: a
    re-detection that cites the original in-thread root **plus** an off-thread reply keeps the SAME
    fingerprint (`issue_fingerprint(thread, root, category)`, root = earliest source), so `IssueStore._merge`
    folds the off-thread msg id + any new `missing_info` into the live issue, and `missing_info` rides into
    the clarity/question/**resolution** briefs via `_issue_brief` → report/voice. **Low severity**
    (missing_info is abstracted needed-facts not verbatim text; ids only; can't resolve/enter qa/move
    active_thread_id), **pre-existing** (not introduced by redirect/voice), and **mitigated** by in-thread
    briefing + the redirect nudge. Optional future hardening (NOT done — touches the live-demo prompt path):
    drop `missing_info` from `resolution_prompt`'s brief, or gate detection-merge under the flags. composer's
    "evidence-before-post → permanent no-nudge" was a **false positive** (`store.save()` is end-of-cycle, so a
    post exception rolls the step back) — but the extend was still reordered to strictly post-success as
    defense-in-depth (`test_redirect_records_no_evidence_when_the_post_fails`). Also added: a 3500-char cap on
    the voice transcript (`_VOICE_TRANSCRIPT_MAX_CHARS`) so a misbehaving LLM can't make the voice post fail.
    **Sticky `active_thread_id` on a mid-flight mode flip** (both, MED) is avoided by starting the demo from a
    CLEAN `.state/issues.json` (flag on from the first cycle).
- **Escalation (per-issue, consolidated + time-gated, revised 2026-06-16):** each issue gets **exactly one**
  top-level `<users/{id}>` @mention reminder (`Issue.escalated`, persisted), and a reporter's issues that go
  overdue in the **same poll cycle** are folded into a **single** nudge so they aren't pinged once-per-issue
  at the same moment (the original "1 lần thông báo" complaint). Issues that go overdue at **different** times
  each get their own one reminder — i.e. staggered issues ⇒ one message per issue, over time (the user's
  "mỗi issue nhắc 1 lần" decision; an earlier per-reporter one-shot via `escalated_reporters` was tried then
  reverted same day because it dropped later issues silently). Built **after the per-issue loop** in
  `_escalate_due`: gather every reporter with an issue past the grace window, then fold that reporter's idle
  awaiting issues (`_escalation_ready`: `_escalation_pending` + `idle_cycles>=1`) into the nudge. Trigger is
  **wall-clock**, not cycle-count: `ESCALATE_AFTER_SECONDS` (default **300** = 5 min; `0`=remind on first idle cycle,
  **negative disables**) via `_seconds_since(last_question_at)` — replaced the old `ESCALATE_AFTER_IDLE_CYCLES`.
  Single-issue nudge keeps the old phrasing + records the nudge `thread_id` as `escalation_thread_id`
  (source A home); **multi-issue** nudge lists the titles as bullets and leaves `escalation_thread_id` unset
  (a reply in the shared nudge thread can't be attributed to one of several issues — it points back to the
  original threads). Staleness is **deferred while a reminder is owed** (`_escalation_pending`) so the bot
  always nudges before giving up; every pending issue escalates within the grace window, so this never defers
  stale forever — ∴ a stale-only test must set `ESCALATE_AFTER_SECONDS=-1`. Stable
  `request_id=client-escalate-{sorted issue ids}` for idempotency. Mention format `<users/{id}>` in
  `text` is the documented Chat syntax (docs `spaces.messages` `formattedText`) and works for user-OAuth
  posts — but ⚠️ **not yet verified live** that it renders as a true mention for a user (vs Chat-app) sender;
  confirm on the next real run. The old multi-round boundary (escalation fires once; if the reporter stayed in
  the nudge thread the bot kept posting to the issue thread and could stale) is **now fixed** by
  follow-the-reporter: the bot moves the whole back-and-forth into the nudge thread once the reporter answers
  there, so multi-round no longer needs a second nudge.
- Tests: `test_runner_hardening.py` (`OutOfThreadCaptureTest` — confirmation now lands in the reporter's reply
  thread; `FollowReporterThreadTest` — follow-up lands there too; `EscalateBeforeStaleTest`;
  `EffectiveConversationGuardTest` incl. `test_nudge_thread_reply_pulled_even_when_two_awaiting`). Note
  MockLLM clarity is trivially `is_clear=True` with
  FakeChatClient's ISO timestamps (date+number patterns match the timestamp), so resolution hinges purely
  on whether a reply is *detected* — which is exactly what the out-of-thread test exercises.

## Cross-thread issue anchoring — "bot replies in the wrong thread" (fixed 2026-06-16)
Live symptom (user screenshot): the reporter posted "hi" then "our homepage is 404 now" as two
**separate top-level messages** (each opens its own thread); the bot posted the 404 clarifying
questions as a reply to the **"hi"** thread. Root cause in `analyzer._build_issue`: when the model
lumps consecutive messages from one reporter into one issue and cites source ids spanning **multiple
top-level threads**, the old rule `root_message_id = source_ids[0]` (earliest in transcript order)
anchored the whole issue — and thus every clarifying post (`_post_to_thread` → `issue.thread_id`) — to
whatever came **first**, often an unrelated greeting in a different thread.
- **Fix:** `_anchor_thread(source_ids, by_id, f"{title} {summary}")` picks the thread whose cited
  message text shares the most word tokens with the model's own title/summary (the thread the issue is
  ABOUT), then `_build_issue` drops the cross-thread stragglers from `source_message_ids`. Single-thread
  issues are unchanged (root stays the earliest). **Content-overlap, NOT recency/count** — chosen
  because a greeting cited *before* the report and a follow-up reply cited *after* it are identical by
  position/count; only the title disambiguates. Tie / no-overlap → earliest cited thread (old behavior).
- **Why recency-tiebreak was wrong (caught by `FollowReporterThreadTest`):** an out-of-thread follow-up
  reply that happens to carry an issue-signal word ("…on the **outage**") would re-anchor to its own
  thread, spawn a **phantom** issue there, and that phantom thread then suppressed source-B out-of-thread
  capture for the real issue. Content-overlap keeps the re-detection anchored to the original thread → it
  merges by fingerprint (no phantom) — and is consistent with the documented detection-merge (root stays
  the in-thread original when the title is about it).
- **Limitation:** MockLLM titles from the FIRST flagged line, so the offline mock can't demonstrate the
  e2e fix on a greeting-lump (its title points at the greeting). The regression test (`CrossThreadAnchorTest`
  in `test_analyzer.py`) uses a stub LLM with a realistic title, which is the right level — the live model
  always titles by the issue. Bare `"hi"` isn't flagged by the mock anyway, so the mock path is unaffected.
- Open/related (NOT a wrong-thread bug, left as-is): repeated identical top-level reports ("our homepage
  is 404 now" sent again) each open a new thread and dedup is thread-scoped (`IssueStore._find_similar`
  requires same thread), so each spawns its own issue. Reasonable for now; revisit if it becomes noisy.

## Voice reports = audio FILE attachment only — NOT a native voice message (settled 2026-06-15)
The voice-report feature posts the TTS audio as a Chat **file attachment**. A user asked for a
**native voice message** (the recorded-voice bubble with a waveform + inline ▶, like the mic-record
feature in the Chat app). **That is impossible for a bot — a hard platform ceiling, not our bug.**
Proven, not assumed:
- **The public REST `Attachment` resource has no voice fields.** Its keys are exactly
  `attachmentDataRef, contentName, contentType, downloadUri, name, source, thumbnailUri` — no
  `waveform`, no `duration`, no audio-message type. (Confirmed against the live message-create
  response for our own uploads, and the [docs](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages.attachments).)
- **The native voice bubble is a web-client/UI feature**, served by the *internal* API
  (`chat.google.com/u/1/api/get_attachment_url`, NOT `chat.googleapis.com/v1`). Its rendered HTML
  carries `data-waveform-samples="..."` + `data-duration-ms` + `data-media-type="audio"` — metadata
  the mic-record UI computes at record time and the public API never exposes. Tellingly its download
  `content_type` is `audio/mpeg` (same as ours), so **format is NOT the differentiator** — the
  waveform/voice metadata is.
- **Voice messages are no longer Workspace-only** — personal @gmail can record+send them by hand in
  the app (the 2024 "Enterprise-only" launch note is stale). But that's a *human* clicking the mic;
  it has nothing to do with the bot/API path.
- **Format experiment (settled it).** Re-uploaded the same audio as `audio/mp4` (m4a/AAC, matching
  the native player's `data-media-source-type`) AND `audio/ogg` (opus) to the DM. The user confirmed
  **both still render as a download file card**, identical to mp3. So: Tier A (waveform bubble) ❌
  no API field; Tier B (inline player) ❌ all of mp3/m4a/ogg are file cards; Tier C (file card) ✅ is
  all a bot gets, regardless of container.
- **The decisive test — exact-match webm/opus + `UserRecording_*.webm` filename.** The user captured
  the web UI's *internal* upload requests, which revealed the native voice file is named
  `UserRecording_<epoch_ms>.webm` (WebM/Opus), uploaded resumably with a quirky
  `x-goog-upload-header-content-type: audio/mpeg`. I transcoded our mp3 to mono webm/opus
  (`ffmpeg -ac 1 -c:a libopus -b:a 32k -f webm`), named it `UserRecording_<ms>.webm`, and posted it
  via the **public REST** path in two content-type variants (`audio/webm` and `audio/mpeg`, mirroring
  the UI header). **Both still render as a download file card.** So even byte-for-format-match +
  magic filename doesn't trigger the voice bubble: the waveform/voice metadata lives in the
  *internal message-create call* (client-computed at record time, sent over cookie-auth internal API),
  which the bot's REST/OAuth path cannot reach. Conclusion is now exhaustive across 4 formats.
- **Decision: keep mp3, no code change.** It's grok-TTS's native output (no transcode), plays
  everywhere. Docs already say "spoken voice note / audio attachment" — accurate, don't relabel it
  "voice message". `ffprobe`/`ffmpeg` are available if a transcode is ever wanted, but it buys nothing.

## Self-hosted Langfuse observability — wired + verified 2026-06-17
End-to-end traces work against a **local self-hosted Langfuse**, NOT cloud. Hard-won facts:
- **SDK major MUST match server major.** Langfuse **3.x/4.x Python SDK is OTEL-based** and POSTs traces
  to `/api/public/otel/v1/traces` — that endpoint **404s on a v2 server**. The v2 server exposes
  `/api/public/ingestion` (401 without auth). So a v2 server (`langfuse/langfuse:2`, e.g. 2.95.11)
  needs the **v2 client: `pip install "langfuse==2.60.10"`** (latest 2.x; `"langfuse<3"` resolves
  there). Diagnose a mismatch by `curl -X POST <host>/api/public/otel/v1/traces` → 404 = wrong pair.
  Server version: `GET /api/public/health` → `{"status":"OK","version":"2.95.11"}`.
- **v2 SDK import paths differ** — `observability.py` handles both: `observe` is top-level on v3 but
  `langfuse.decorators.observe` on v2; flush is `get_client().flush()` on v3 but
  `langfuse_context.flush()` on v2. `_real_observe`/`flush` try v3 then fall back to v2.
- **`observability.trace("issue")` is a no-op on v2** (no `get_client`), so per-issue span grouping is
  lost — but each LLM call still gets a generation span (latency+tokens) via `langfuse.openai`, plus
  the 5 `@observe` boundary spans. Enough for latency diagnosis.
- **`.env` → `os.environ` bridge** (`observability._seed_langfuse_env`): `load_config()` reads `.env`
  one-way into `Config`; it never exports to `os.environ`, but the langfuse SDK reads credentials FROM
  `os.environ`. So when `OBSERVABILITY=langfuse`, the shim seeds `LANGFUSE_*` into `os.environ`
  (`setdefault` — real shell env wins). Without this, keys in `.env` are invisible to the SDK.
- **Host = `http://localhost:3000`** (the docker-published port), NOT the `172.x` container IP the
  Langfuse UI prints in its sample snippet — that internal IP isn't reliably reachable from the host
  process. Get keys from the UI (sign up → Org → Project → Settings → API Keys) or seed headlessly via
  `LANGFUSE_INIT_*` in compose. Verify a trace landed: authenticated `GET /api/public/traces?limit=5`.

## Detecting a native Chat 1:1 call hang-up — the `huddleStatus` signal (2026-06-18)
Goal: detect when the OTHER user hangs up a native Google Chat/Meet **1:1 call** (the
ringing call placed from the DM's "Start a video call" button). Three channels probed
against real live calls in the bot↔Duc DM (`spaces/qtotjoAAAAE`). **Winner: the Chat
REST API.**
- **✅ Chat REST API (`spaces.messages.list`) — THE clean, supported signal.** A DM call
  posts a *message* whose annotation is a `RICH_LINK` / `richLinkType: MEET_SPACE` with
  `meetSpaceLinkData: {meetingCode, type:"HUDDLE", huddleStatus}`. **`huddleStatus`
  is the call lifecycle** — captured live by `scripts/huddle_watch.py`:
  `None → STARTED` (live/ringing) → terminal **`MISSED`** (ring never answered, ~40 s
  ring timeout) **or `ENDED`** (connected then hung up). So the hang-up = the
  `STARTED → ENDED` transition; poll the DM messages and watch it. Needs only the
  `chat.messages[.readonly]` scope the bot already has. Latency = propagation + poll
  (seconds), not instant — for instant push you'd need the Workspace Events API
  (`google.workspace.chat.message` over Pub/Sub). Proven: `huddle_watch.py` caught a
  live `STARTED → MISSED` in real time; `ENDED` confirmed across historical DM calls.
- **❌ Meet REST API — BLIND to native Chat calls.** Confirmed twice (incl. against a
  genuinely *connected* 2-party call, space `spaces/y_F__Wg3UvMB` / code `hff-vgxv-kwh`):
  `conferenceRecords?filter=space.meeting_code=…` and `…space.name=…` → **0 records**;
  `spaces.get` on the call space → **400 INVALID_ARGUMENT**. The ONLY spaces Meet REST
  can read are ones the bot **mints itself** via `spaces.create` (`make_call.py` /
  `demo_meet_call.py`). A Chat-UI huddle is a different surface REST doesn't expose. ⇒
  Don't try to monitor a real call via Meet REST; and DON'T substitute a minted Meet
  room for an actual call (the user is explicit: **always place the real ringing call**).
- **⚠️ Browser network (CDP + Playwright) — hang-up only observable INDIRECTLY.** The
  live 1:1 call state runs over `chat.google.com/u/<n>/webchannel/events` (a long-poll),
  not continuous Meet RPCs (`meet.google.com/$rpc/google.rtc.meetings.v1.*` fire only
  during ~0–42 s setup: Resolve/CreateMediaSession/CreateMeetingDevice/Sync…/CreateMeetingInvite).
  At hang-up the captured signature is: the call's webchannel **SID is rotated** (old
  session abandoned, fresh `SID` GET+POST handshake) + the DM reverts to normal chat
  (DynamiteWebUi JS + reaction-emoji reload) + the DOM leave-control vanishes. The clean
  "participant left" roster frame lives inside the `SyncMeetingSpaceCollections`
  **server-stream**, which Playwright's `response.body()` can't read mid-stream — so the
  `sig_body` capture in `call_network_capture.py` mis-times streamed bodies by *drain*
  time (e.g. a 42 s join `UpdateMeetingDevice` appears at the 104 s marker). Net: the
  browser sees the *consequence* (UI teardown), not a discrete event — use the DOM
  heuristic (`meet_call_browser._in_call` / `_alone_signal`) if you must scrape, but
  prefer the Chat REST `huddleStatus`.
- **Scripts**: `scripts/huddle_watch.py` (Chat REST poller — the deliverable),
  `scripts/meet_rest_watch.py` (Meet REST participant-leave watcher — only works on
  bot-minted spaces), `scripts/call_network_capture.py` (CDP network capture + DOM
  marker — diagnostic), `scripts/meet_call_browser.py` (places the ringing call via CDP
  into the daily Brave). Coordination gotcha: the ring times out in ~40 s, so the callee
  must answer the **incoming ring on their device** (NOT tap a Meet link in the DM)
  within that window or it logs `MISSED`.

## Unattended call self-test — browser-side join/hang-up + isolated voice capture (2026-06-19)
The hands-off loop (`scripts/selftest_call.sh`): caller (mikmikb26, daily Brave via CDP
`:9222`) rings → callee (`scripts/auto_answer.py` on a 2nd Brave `:9223`, fresh
`.browser-profile-callee`, Duc) auto-answers + turns mic/cam on → both hold → callee
auto-leaves → caller self-stops on hang-up + captures the call-only audio. Verdicts are
independent: HANG-UP DETECTION (answered + an end-reason + no duration cap) and AUDIO
CAPTURE (ffmpeg `volumedetect` mean_volume > −80 dB).

### 🔑 UPDATE 2026-06-19 (later) — the occlusion theory was largely a RED HERRING; the real fix is a CLEAN profile
Five live runs this afternoon overturned the "Wayland occlusion" diagnosis below for the
MEDIA-CONNECT failure. The decisive run (`reports/selftest_20260619_145540`, caller =
dedicated clean profile HEADED on the real GPU display via the new `--caller-headed`):
- **BREAKTHROUGH — pickup + media + hang-up ALL fire on the caller**: roster join `REMOTE
  JOINED: Duc Tran Trong +9s` (tiles=2), ICE `ics=connected igs=complete` with a succeeded/
  nominated candidate pair, inbound RTP `inB` climbing 2.8→3.7 MB, `recvLive=4 recvUnmuted=1`
  (Duc's live unmuted audio track present), and on Duc leaving, hang-up via `roster collapsed
  to 0` — a clean self-stop, NOT the cap.
- **CORRECTION**: the DAILY Brave (`:9222`, `--caller-real`) FAILS to connect media — every
  PeerConnection goes `cs:closed`, `recvUnmuted=0` across the whole call — *even though*
  `ctxState:'running'` (renderer NOT suspended), the window was a real GPU window, and the
  account was correct. So the all-PCs-closed / silent-capture failure was the **daily Brave
  itself** (heavy multi-account state / tab clutter / extensions / leftover call tabs), NOT
  Wayland occlusion. A CLEAN single-account profile connects media fine on the real GPU
  display. Occlusion may still throttle DOM signals, but it was never the media-connect blocker.
  ⇒ **Use `--caller-headed` (dedicated clean profile, headed real GPU) as the real-Brave path**;
  `--caller-real` (daily :9222) is unreliable and also grabs the wrong tab (a run attached to a
  blank `chrome://newtab/` and never found the call frame).
- **Account-index DRIFT**: glo.com was REMOVED from the daily Brave entirely → indices shifted
  DOWN: mikmikb26 `u/1`→**`u/0`**, and `u/1` is now Duc (= the callee). Using the stale `u/1`
  made the caller ring AS Duc (same identity as the callee) → no ring reached the callee. Probe
  with `scratchpad/probe_accounts.py`; selftest default `CALLER_URL` fixed to `u/0`.
- **`media_connected` gate (FIXED + verified)**: no teardown signal fires until real inbound
  media is observed (`peak_live≥1` or RTP bytes grew). Killed the false "controls disappeared"
  hang-up that quit ~2.5s after answer (it had mis-read the ring→connect UI flicker, after a
  false-early join on the stale ringback PCs). The caller now stays in the call through connect.
- **⛔ STILL OPEN — captured WAV is SILENT (−91 dB) despite media flowing.** `recvUnmuted=1`,
  RTP 3 MB+, recorder `recording` — yet the WebAudio capture graph delivers no samples. Root
  cause (strong hypothesis): `__mcbStartRec` wrapped the track in TWO separate `new
  MediaStream([t])` objects (one for the decode-activation `<audio>` sink, one for the
  `createMediaStreamSource`), and Chromium activates lazy decode of a remote track PER-stream
  → the sink decoded, the source stayed silent. **FIX WRITTEN BUT NOT YET VERIFIED LIVE** (user
  said stop): share ONE MediaStream for sink+source, add a `createMediaElementSource(sink)→dest`
  second path, and add `--autoplay-policy=no-user-gesture-required` to the caller. The next
  `--caller-headed` run is the decider — confirm `mean_volume > −80 dB`.
- **Teardown bug (FIXED)**: `brave-browser` is a launcher that forks a SEPARATE
  `/opt/brave.com/brave` process tree, so killing the launcher PID (`$!`) orphaned the real
  browser → it held the profile lock → blocked the next run. `teardown_caller` now also
  `pkill -f "$CALLER_PROFILE"` (safe: only brave carries the profile path in its cmdline).
- **New tooling**: `--caller-headed` flag; ICE candidate-pair diag in `--diag` (`ics`/`igs` +
  local/remote candidate types + selected pair — the `_webrtc_ice_stats` probe); `media_connected`.

### 🔑 UPDATE 2026-06-19 (evening) — run #5 has NOT reproduced; media connection is the real blocker
Four more live runs (one with a REAL human callee on a separate device, three auto-callee). The
breakthrough run #5 (145540, stable media) did **not** reproduce — every run since failed to HOLD
media. Corrected model of the failure modes (they are DISTINCT):
- **The human reporter DID pick up** (run `selftest_20260619_153825`, `--no-callee`). The earlier
  "ring never answered" claim was WRONG. Caller logs show a remote audio receiver briefly UNMUTED
  (`unmuteSeen:1`, `m:False`) then `ended`, PCs `closed`. So pickup worked; the media didn't STAY.
- **Headed-on-GPU = connects-then-DROPS**: media flickers on (an unmuted audio receiver appears)
  then the PC closes seconds later. Consistent with Wayland renderer-suspend when the Brave window
  gets COVERED (by the terminal/other windows) → so occlusion is NOT fully a red herring; it's the
  *connects-then-drops* mechanism, distinct from the daily-Brave *never-connects* (clutter) one.
- **Xvfb-on-swiftshader = NEVER connects** (run `155522`): ICE gathers (`igs=complete`) but NO
  candidate pairs ever, `unmuteSeen` stays 0, no media. Strongly suggests software-GL
  (`--use-angle=swiftshader`) breaks Meet's WebRTC media/ICE path. ⇒ **The "Xvfb is THE FIX" note
  above is WRONG for the MEDIA path** — Xvfb may keep the renderer awake but it kills the call.
  (Confound: placed right after a headed run, so Google THROTTLING can't be fully ruled out.)
- **Likely Google THROTTLING after rapid repeated calls**: ~4 automated calls in ~30 min, and the
  "ICE gathers but never pairs / PCs close" pattern is a classic relay-starvation/throttle symptom.
  Pause ~20-30 min between live-call sessions; don't hammer (account-flag risk).
- **`media_connected` gate was TOO STRICT (FIXED this session)**: it only latched on a *currently-
  live* track (`peak_live≥1`) or *growing* RTP bytes. A real call whose media flickered on then
  dropped never latched it → EVERY hang-up signal stayed gated off → the call held to the duration
  CAP. That is exactly the reporter's complaint: *"I picked up <30s and hung up, but the WAV is 5
  min and you said hang-up detection works."* It did, for run #5 — but the gate silently disabled it
  for the flaky-media call. FIX: also latch `media_connected` on `unmuteSeen≥1` (a remote-audio
  'unmute' event — monotonic so a poll can't miss it, and ringback-safe: a PC that never carries
  media never unmutes). New helper `_webrtc_unmute_seen` + a latch right after join detection
  (prints `[media] remote audio unmuted — media-connected latched`). py_compile OK; NOT yet live-verified.
- **Teardown DEADLOCK (FIXED this session)**: the bare `wait` after the call blocked forever on the
  `brave-browser` launcher (it PARENTS the real browser and does NOT exec/detach on this machine, so
  it's a live bg job) — which only dies later in `teardown_caller`. The script hung at end-of-run
  (had to kill it by PID). Fix: run `teardown_caller`/`teardown_callee` BEFORE the bare `wait`.
  Verified: run 155020 reached its verdict and tore down cleanly.
- **New `--no-callee` selftest flag**: a HUMAN answers on another device (no auto-callee); the
  caller's own `REMOTE JOINED` line is the pickup proof; the audio verdict still runs.
- **⚠️ kill caller/callee by PID, never `pkill -f <profile-path>` from an interactive shell** — the
  shell's OWN command line contains the profile-path string → pkill self-matches and SIGKILLs the
  shell (seen as exit 144). Read `/proc/<pid>/cmdline` to enumerate, then `kill -9 <pids>`.
- **NEW caller mode `--caller-xwayland` (HYPOTHESIS for the next call, NOT yet verified)**: headed on
  the real display but forced onto X11/XWayland (`--ozone-platform=x11` +
  `--disable-features=CalculateNativeWinOcclusion`, NO swiftshader → real GPU). Rationale: on X11
  Chromium's OWN occlusion calc decides suspension and the disable-flag suppresses it (unlike native
  Wayland, where Mutter gates frames below the flag), while the real GPU keeps media connecting
  (unlike Xvfb/swiftshader). Targets the connects-then-drops failure directly. Try this on the next
  `call lại`: `bash scripts/selftest_call.sh --caller-xwayland --diag --no-callee --duration 160`.
- **⛔ STILL OPEN — voice capture (req #3) NEVER verified non-silent.** Run #5 (the only stable-media
  run) captured silence and PREDATES the capture fix (shared MediaStream + `createMediaElementSource`
  + autoplay flag). The fix is staged but UNTESTABLE until a stable media connection is available
  again. Pickup-detect ✓ and hang-up-detect ✓ are real but only fire when media STAYS connected.

#### 🔑 UPDATE 2026-06-19 (later evening) — REAL root cause = SINK ROUTING MISMATCH (NOT a media wall). FIXED.
Run `selftest_20260619_164032`: **monitor** mode, `--caller-xwayland`, `--no-callee` (human picked up +
spoke + hung up). The WAV was −91 dB and the ICE diag showed both hooked PCs `closed`/`inB=0` — I
**WRONGLY** concluded "media never connected / Google throttling." **The user corrected this with ground
truth: the caller's SPEAKER audibly played the remote voice.** So media DID connect — the `inB=0` was a
**probe blind spot**: `window.__mcbPCs` had only the stale closed *ringback* PCs; the real media PC wasn't
in the hooked set, so the ICE/bytes read was meaningless, not evidence of no-media. Lesson: **trust an
audible speaker over the `__mcbPCs` telemetry** — that hook does NOT reliably capture the live media PC.
- **The actual bug (proven, call-free):** this machine has **4 HDA output sinks** (`sofhdadsp`,
  `_3`,`_4`,`_5__sink.2`,`__sink.2`). `AudioCapture._start_monitor` recorded `get-default-sink`.monitor
  (`…__sink.2`), but **Brave played the call to a DIFFERENT sink** → that monitor had no signal → −91 dB,
  while the operator heard the call on the sink Brave actually used. Confirmed: a 440 Hz tone played to
  the default sink and recorded off ITS `.monitor` = **−25.9 dB (loud)** — so the capture path itself is
  fine; we were just listening to the wrong sink.
- **FIX (`meet_audio_capture.py`):** new `_browser_output_sink_name()` reads the browser sink-input's
  `Sink:` field (where Brave is *actually* routed) and `_resolve_monitor_sink()` records THAT sink's
  monitor (polls ~4s for the stream to appear; falls back to the default sink; prefers a non-default sink
  when several browser streams exist). `_start_monitor` now uses it. Logs `browser audio routed to sink
  '…' — recording ITS monitor`. Offline-verified: picks the browser's sink (132), not the default (134);
  returns None→default when no stream. py_compile OK. **NOT yet live-verified** (needs the next call).
- **Run #2 (`selftest_20260619_172216`) + the call-free sink diagnostics — ISOLATE is the robust fix:**
  - Pickup fired again (+32.7s, webrtc). The monitor routing-fix RAN but logged `no active browser
    stream found yet — falling back to default sink` → recorded the default sink → **−91 dB again**.
    Root cause: at join (+33s) Brave had **no audio sink-input yet** (the remote wasn't producing audio
    that instant), so the resolver's 4 s poll found nothing and gave up. A pure TIMING miss.
  - **I then prematurely KILLED a working run** — the `caller.log` was lagging behind execution
    (conda-run output buffering), I read only "Call placed" at 71 s elapsed, wrongly concluded "stuck,"
    and SIGINT/KILLed it right as join had fired. LESSON: do NOT infer "hung" from a stale log alone —
    check the WAV file GROWING and process %CPU over time first; conda-run delays the log file.
  - **Call-free sink diagnostics (decisive):** a throwaway Brave playing a 440 Hz WebAudio tone shows a
    findable sink-input (`application.name="Brave"`, `node.name="Brave"` → app_match works) routed to the
    DEFAULT sink (134), which goes RUNNING. And **AudioCapture(mode="isolate") captured that tone at
    −16.3 dB** (loud): the continuous `_mover_loop` moved the Brave stream into the `meet_capture` null
    sink and recorded its monitor. ⇒ **Use `--audio-mode isolate` for the next call.** It (a) catches the
    stream WHENEVER it appears (mover every 1 s, no 4 s window), (b) is immune to which physical sink
    Brave picks (we move it), (c) captures ONLY the Brave call stream = the goal. Trade-off: the caller
    machine won't play the call on its speakers (stream moved to null sink) — irrelevant, the human is
    the callee on another device. The OLD "isolate breaks at the match step" note is SUPERSEDED: the
    match works; the earlier failure was the same timing miss, which the 1 s mover loop now absorbs.
- **Still also true from this run:** hang-up wasn't detected and the WAV rode to cap — because the
  capture was silent AND `media_connected` is gated on `__mcbPCs` (the same blind probe) so it never
  latched. With media genuinely connected, the survey-based hang-up (ungated) should still have fired
  when the human hung up — TBD next run whether the caller actually shows the rating survey here.
  The new `calling…`-disappears DOM pickup signal didn't fire (the false webrtc track-count signal
  latched join first); reconsider ordering if it matters.
- **⚠️ pgrep/grep self-match (re-confirmed, wider):** a `grep`/`pgrep -f` whose PATTERN string appears in
  the checker's own command line self-matches (I hit false "STILL UP" reports). Reliable checks: `pgrep
  -x ffmpeg` / `pgrep -x brave` then read `/proc/<pid>/cmdline`, OR `ps … | grep PAT | grep -v grep`.
- **⚠️ conda-run swallows SIGINT:** `kill -INT` on the `conda run …` wrapper PID did NOT reach the python
  child; even SIGINT on the child didn't unwind a Playwright sync call promptly — needed SIGTERM→SIGKILL.
  Downside: SIGKILL skips the script's `audio_cap.stop()`/atexit, ORPHANING the ffmpeg recorder (it keeps
  recording the desktop forever). Always reap leaked `ffmpeg … call.wav` after a force-killed run
  (4 such zombies were found running since ~01:39/02:15/02:27 AM and killed this session).

### (Earlier 2026-06-19 — superseded occlusion investigation, kept for context)

- **🔑 ROOT CAUSE of all flakiness — GNOME Wayland throttles Brave's renderer when its
  window is OCCLUDED / not OS-focused.** A throttled renderer stalls WebRTC media
  RX/processing, audio, AND DOM rendering. Symptom split is binary: window visible+focused
  → join `+8s`, audio captured, survey/roster/controls render & fire; window hidden behind
  the terminal → join `+33s`, silent WAV (−91 dB), no hang-up signal → cap.
  - **⚠️ The launch flags do NOT fix it (DISPROVEN live 2026-06-19).** A full self-test
    with BOTH Braves carrying `--disable-features=CalculateNativeWinOcclusion
    --disable-backgrounding-occluded-windows --disable-background-timer-throttling` STILL
    failed identically when the caller window was occluded (join +33.8s, −91 dB silent;
    AudioContext was `running` and the recorder `recording`, but its receiver tracks read
    `rs:'ended'`/`muted`, `recvLive:0` — media never reached it). Why: on Wayland the
    compositor (Mutter) stops sending `wl_surface.frame` callbacks to an occluded surface,
    a layer BELOW Chromium's occlusion flag, so no flag overrides it.
  - **CDP runtime levers tried, all insufficient occluded**: `bring_to_front`,
    `Emulation.setFocusEmulationEnabled`, `Page.setWebLifecycleState{active}`,
    `Browser.setWindowBounds{normal}`, `Page.startScreencast` frame-ack keepalive.
    `xdotool`/`wmctrl` are X11-only (useless on Wayland). **You also can't programmatically
    OCCLUDE a window to test a fix** — `Browser.setWindowBounds{minimized}` is a NO-OP on
    this GNOME-Wayland session (probe 2026-06-19: `hiddenSeen=false`, rAF stays 60fps), which
    is the same reason no CDP keepalive ever works: the compositor's frame gating is below
    anything CDP/Chromium can reach.
  - **🔑 THE FIX (2026-06-19): run the caller on a VIRTUAL DISPLAY (Xvfb).** A real *headed*
    Brave on `Xvfb :99 1920x1080x24` has no compositor and nothing can occlude it → the
    window is permanently `visibilityState:'visible'` → the renderer never suspends,
    regardless of what's on the real screen → **fully unattended** (no window babysitting).
    Mechanics verified: `raf 60fps`, `AudioContext.currentTime` 1:1 with wall-clock, software
    WebGL (`--use-gl=angle --use-angle=swiftshader`) OK. Wired as the DEFAULT in
    `selftest_call.sh` (it starts Xvfb + the caller Brave on port 9322 + tears them down;
    `--caller-real` reverts to the babysat `:9222` path). Needs a dedicated
    `.browser-profile-caller` signed in as **mikmikb26 only** (fresh single-account profile ⇒
    the DM is `u/0`, NOT `u/1`; glo.com never added). Keep foreground/keepalive ON even on
    Xvfb — `_keepalive_renderer` also `bring_to_front()`s the call TAB (a non-active tab is
    `hidden` → DOM throttled → tiles=0 → no roster hang-up signal), so never pass
    `--no-foreground` here. The old "keep the window visible+focused for ~50s" workaround is
    superseded; visible-window runs still work (+8s join) but are no longer required.
  - **⚠️ Live caveat (1st Xvfb call, 2026-06-19): Xvfb keeps the renderer awake but connects
    WebRTC SLOWER than a real GPU window** — join detected at ~+33s (vs +8s visible), software
    GL / no GPU. The first live run lost a TIMING RACE: caller connected at +33s but the callee
    only held 20s → callee already left → caller joined an empty call (`recvLive=0`, `ch=0`, PC
    closed, no hang-up → rode to cap). FIX: long callee hold (`ANSWER_SECONDS=90`, cap 200) so
    the slow caller overlaps, AND run the CALLEE under its own Xvfb (`:100`, default) so Duc isn't
    occluded on the real screen (occluded callee → slow answer / no fake-mic RTP → caller captures
    silence). Whether the Xvfb caller actually receives LIVE audio once overlapped is still
    UNCONFIRMED (the race meant zero overlap); the next run with the long hold is the decider.
- **Hang-up signals on the embedded Chat call UI** (explored exhaustively; the call iframe
  does NOT tear down and CDP can't see it die, so the obvious signal is useless):
  - ✅ **post-call "Rate the meeting N stars" survey** — fires reliably when rendered; the
    probe (`meet_call_browser._FEEDBACK_PROBE`) is **visibility-aware** (ignores a stale
    hidden survey via bounding-rect/offsetParent checks, else it reads "always present").
  - ✅ **DOM roster collapse** + ✅ **in-call controls disappeared** — both work when the
    renderer is awake; controls-disappeared is debounced.
  - ✅ **inbound-RTP `bytesReceived` flatline** — media-layer; the SFU keeps the PC
    `connected` + tracks `live` after the remote leaves, so the one thing that truly stops
    is the RTP itself. (Reads 0 when occluded — needs the flag.)
  - **🔑 8s POST-JOIN GRACE is mandatory**: both teardown blocks are gated `if join_fired
    and now - t_join >= 8`. Without it a control RE-RENDER flicker right after answer
    false-fired "controls disappeared" ~30s before the real leave.
- **Vietnamese UI labels (live-discovered)**: answer = `Trả lời cuộc gọi` (NOT bare
  `trả lời` — that's the "Reply" message button → a false +0.1s answer); decline =
  `Từ chối`; leave = `Rời khỏi cuộc gọi` (NOT `kết thúc cuộc gọi` — that substring matches
  the DISABLED decoy "Hãy kết thúc cuộc gọi trước…", so `_find_button` also checks
  `is_enabled()`); mic toggle shows `Tắt micrô` when ON / `Bật micrô` when muted; camera
  `Tắt máy ảnh` when ON / `Bật máy ảnh` when off. `auto_answer.py` matches only the
  "turn-ON" variants for unmute/camera-on (safe to re-poke — they stop matching once on).
- **🔑 Voice capture = the WebRTC INBOUND tap (`--audio-mode webrtc`, default), made
  truncation-immune.** This is the "voice from the CALL, not other tab/app" path: it taps
  the inbound WebRTC audio track INSIDE the browser (the actual call media stream, OS-
  independent — no desktop mix, blind to other apps by construction), via an immortal
  AudioContext→MediaStreamDestination graph + a `MediaRecorder`. The old single-`.webm`
  bug: if the recorder ever RESTARTED (renderer suspend / error) it wrote a fresh webm
  header mid-file → ffmpeg decoded only the first segment (the **3.15s truncation**). Fix:
  every chunk is tagged `"<frameId>:<gen>|<b64>"`; the Python `BrowserAudioTap` groups
  each (frame,generation) into its OWN standalone webm segment, drains ONLY the owner
  frame (`__mcbCaptureOwner` — no two-frame interleave), then transcodes each segment and
  concatenates the WAVs (`-f concat -c copy`) into the full-length 16k-mono-s16le output.
  Proven offline: a synthetic two-generation feed → 5.0s WAV (not 2s). `monitor`/`isolate`
  OS-level modes are the fallback (coarser; see the script's own docs).
- **🔑 Chromium "WebAudio-from-remote-track silence" bug + the DECODE-ACTIVATION fix —
  proven necessary AND sufficient (loopback A/B, 2026-06-19).** A `MediaStreamAudioSourceNode`
  built from a REMOTE WebRTC receiver track outputs SILENCE unless that same track is also
  sunk into a PLAYING media element (Chromium decodes remote audio lazily — only when an
  element consumes it). Fix in `__mcbStartRec`: for each live remote track, create a hidden
  `new Audio()`, `srcObject = new MediaStream([t])`, `muted = true`, `play()`, and RETAIN the
  ref (GC'ing it stops the decode); THEN wire the track into the capture graph. Verified
  WITHOUT Google via a local WebRTC loopback (440 Hz tone A→B) under Xvfb, driving the REAL
  `_WEBRTC_HOOK` + `BrowserAudioTap`: with the sink → **−9.1 dB** (clean tone captured);
  with the sink stripped → **−91.0 dB** (silent, 1.9 KB empty webm). So every prior live
  silence was the suspended renderer (no live tracks), not a capture bug. Same loopback also
  confirmed the PICK-UP signal (`__remoteTracks→1` when the track arrives) and the **primary
  hang-up signal** (inbound-RTP `bytesReceived` FLATLINES the instant the sender stops +
  live-audio→0). Note: `__pcDead` does NOT fire on a local `pc.close()` (spec: close()
  dispatches no `connectionstatechange`) — that's why the runner leans on the RTP-flatline,
  not `pcDead`, as the robust signal. Harness: `scratchpad/loopback_verify.py`.
- **🔑 LIVE isolate capture SUCCEEDED (2026-06-19, run 173650) — first non-silent
  capture of the call's own remote voice.** `--audio-mode isolate --caller-xwayland
  --no-callee` (human callee on another device). The full 200s WAV measured −52.9 dB
  overall, but a per-25s-block scan localized ALL real audio to a single window
  **126.4s → 140.5s (~14s, mean −43.8 / max −15 dB)** — everything else −91 dB. So
  isolate DOES capture the call's remote voice cleanly (extracted to that run's
  `voice_segment.wav`); the wins+gaps:
  - **Pickup detection fired on the WRONG (false) signal.** The logged "join +33s
    (via=webrtc, tracks=5/base=0)" is the known ringback FALSE positive (track slots
    allocated, 0 bytes). The REAL pickup — when remote audio actually started flowing —
    was **~126s** (when Brave first created a playback sink-input; the isolate mover's
    health-check confirmed a `Brave` sink-input moved into `meet_capture` only at
    ~+150s of its poll). The `calling…`-disappears DOM signal never got to fire because
    the false +33s webrtc latched `join` first.
  - **Hangup detection FAILED** (hit the 200s cap). The `media_connected` latch stayed
    off because the ICE probe is BLIND: every PC `__mcbPCs` enumerated was `ics=closed,
    inB=0` for the entire call (those are stale top-level ringback/signaling PCs; the
    live media PC lives in the cross-origin meet OOPIF and the init-script hook didn't
    catch it). With the latch off, the fragile teardown/survey signals were gated out.
  - **🔑 THE FIX DIRECTION — detect pickup/hangup from the AUDIO-STREAM LIFECYCLE, not
    DOM/ICE.** Ground truth: a `Brave` sink-input appears in `meet_capture` the instant
    remote audio flows (= real pickup) and drops when it stops (= hangup). This run
    proved it: 0→1 at ~126s = pickup, 1→0 at ~140s = hangup, both matching the only
    non-silent audio window. Far more reliable than the blind ICE probe and the
    drifting DOM survey text. The isolate mover ALREADY tracks sink-input
    appear/disappear — wire those edges into the runner's join/hangup events (with a
    short debounce so a ringback blip can't false-fire). The ~126s media-path delay
    (audio took ~2min to flow after the call was placed) is still unexplained — confirm
    whether the human answered late vs. a slow XWayland WebRTC negotiation.
- **🔑 isolate is DISQUALIFIED — it captured a FACEBOOK tab, not the call (user
  correction, run 173650).** The extracted `voice_segment.wav` was NOT the caller's
  voice — it was audio from a Facebook tab. Root cause: the isolate **mover matches
  every PulseAudio sink-input whose `application.name` contains "Brave" and moves it
  into `meet_capture`** — it cannot distinguish the call tab from any OTHER Brave
  tab/window/profile on the machine (incl. the daily Brave on :9222). So it vacuums up
  whatever Brave audio is playing (Facebook) AND, as a side effect, MUTES the user's
  daily-Brave audio for the run's duration (moved to the null sink, restored on stop).
  `monitor` is equally wrong (whole desktop mix). **Only the in-browser `webrtc` tap is
  call-only by construction** (taps the inbound track inside the call renderer, no sink).
- **🔑 webrtc tap on a REAL call: the hook is BLIND to Meet's live media PC (run 175205,
  `--caller-headed --audio-mode webrtc`).** Conclusive `[audio-dbg]` per-frame dump: the
  tap fully armed in the `meet.google.com/call` frame (`rec: recording, ctx: running,
  connected: 4, started: 1, unmuteSeen: 1`) — but **`recvLive: 0, recvUnmuted: 0` at
  EVERY tick** after the +33s false-join, and the only PeerConnections in `__mcbPCs` are
  `ics: closed, ss: closed` with receiver tracks `{k:'video', rs:'ended'}`. So the WAV
  was −91 dB silent for the full 280s. The `__mcbPCs` constructor-wrapper catches only
  the CLOSED ringback/signaling PCs; Meet's actual live media PC is NOT intercepted —
  almost certainly because Meet's bundle captures the native `RTCPeerConnection`
  reference before our `window.RTCPeerConnection` init-script runs (timing, esp. over CDP
  into a pre-loaded browser + the cross-origin meet OOPIF). `inv.els: []` too — no
  `<audio>/<video>` srcObject in that frame to fall back to. TWO compounding problems
  this run: (A) hook blind to the media PC, and (B) no evidence media even SUSTAINED a
  connection (every PC closed, `inB=0`, the false-join tracks ended at once). FIX
  DIRECTION for (A): instrument `RTCPeerConnection.prototype` methods (e.g.
  `addTransceiver`/`createAnswer`/`setRemoteDescription` → push `this` into a registry)
  instead of wrapping the constructor — the prototype is shared by every real PC, so it
  catches Meet's media PC regardless of a pre-captured constructor (testable offline via
  `scratchpad/loopback_verify.py`). But (B) is the prerequisite: confirm media actually
  connects+sustains first (needs the caller window genuinely un-occluded on
  GNOME-Wayland, or it suspends and drops).
- **⚠️ pgrep/SIGINT pitfalls re-confirmed this session**: (1) `kill -INT` on the
  `conda run` WRAPPER pid does NOT reach the python child — signal the actual
  `python -u …` pid (find via `pgrep -x python` + read `/proc/<pid>/cmdline`, NOT
  `pgrep -f meet_call_browser.py` which SELF-MATCHES the checker's own argv → exit 144,
  killed its own subshell). Reap by exact-name scan + cmdline filter only.
- **🔑 webrtc tap is DISQUALIFIED for Google Meet — the prototype hook works, but Meet
  doesn't expose the decoded audio as a tappable track (run 181136).** The prototype-level
  `RTCPeerConnection.prototype` patch DID fix the hook-blindness from run 175205: we now
  intercept Meet's media PCs *with their audio receivers* (`connected:4`, a PC with 4 audio
  receivers — vs the prior run's video-only). But `recvLive:0, recvUnmuted:0` at EVERY tick
  for the full 200s, every receiver `rs:'ended'`, every PC `cs:'closed'`, and `inv.els:[]`
  (no `<audio>/<video>` srcObject). So Meet renders the remote audio through its OWN Web
  Audio path — NOT as a live MediaStreamTrack on a receiver, NOR on a media element — and the
  in-browser tap simply cannot reach it. The recorder produced a 0-byte webm → −91 dB WAV.
  **Conclusion: stop fighting the in-browser tap on Meet.** Capture the DECODED audio at the
  OS sink instead (the one place it's guaranteed to exist — the caller "hears" it).
- **🔑 BREAKTHROUGH — capture the CALLER browser's decoded audio at the OS sink, scoped by
  its process tree ('profile' mode).** During run 181136 the caller Brave had exactly ONE
  PulseAudio Playback sink-input (`#6290`), owned by PID 1615359 = the caller's own
  `--type=utility --utility-sub-type=audio.mojom.AudioService`, a direct child of the caller
  root (`--user-data-dir=…/.browser-profile-caller --remote-debugging-port=9322`), and its
  cmdline CONTAINS the caller profile path (the audio-service utility inherits `--user-data-dir`).
  This is the fix for BOTH the old isolate disqualification AND the webrtc wall: (1) the caller
  is a DEDICATED profile that plays nothing but the call, so its audio output IS the call —
  call-only by construction; (2) scope capture to sink-inputs whose owning PID-tree carries the
  caller's `--user-data-dir` (NOT `application.name="Brave"`, which matched the daily Brave's
  Facebook tab) → the daily Brave (:9222, default profile, no `--user-data-dir`) is NEVER
  matched/muted. Implemented as `meet_call_browser --audio-mode profile` (auto-derives the
  match token from `--cdp-url` via `_derive_proc_match`; explicit `--audio-proc-match` override)
  → `AudioCapture(mode="isolate", proc_match=…)`; `_browser_sink_inputs` scopes by `_pid_in_tree`.
  `selftest_call.sh` now defaults `AUDIO_MODE=profile`. Logic validated offline (derive,
  PID-tree match, bogus→[], suite 341 green). LIVE verify done via the SIMPLER `monitor`
  mode (then `allsinks`) — see "the full 5-call investigation" below; `profile` mode remains
  offline-only (the user accepted by-app/by-sink granularity, so profile/PID-tree was not needed).
- **🔑 hangup never fired in EITHER run because `media_connected` (the gate on every teardown
  signal) is latched ONLY by WebRTC signals — all blind in OS mode.** So it stayed False → all
  teardown gated off → both runs held to the 200s cap. Fix: an OS-mode latch
  (`AudioCapture.stream_seen()` — the caller sink-input was seen present post-join — OR join+10s)
  arms the existing frame/survey/roster signals; PLUS a new WebRTC-independent hangup signal
  `AudioCapture.lost_stream()` (the caller's matched sink-input DISAPPEARED after being present
  = the call's audio element torn down on hang-up), debounced 3 polls inside the post-join settle
  grace. LIVE: hang-up auto-detect is FOREGROUND-gated — fired cleanly in call 1 (`tiles=1`,
  control-disappeared) but rode to the cap in the backgrounded calls (`tiles=0`); see "the full
  5-call investigation" below.
- **✅ CHOSEN DIRECTION — OS-sink capture works (USER-CONFIRMED 2026-06-19).** The user listened
  to `reports/captured_voice_proof.wav` (the 20s clip extracted from call-4's sink-134 monitor
  recording) and confirmed it IS correct **"ring call + callee voice"**. So the OS-sink monitor
  approach (record the sink's `.monitor`, `--audio-mode allsinks` to be multi-sink-safe) is the
  VERIFIED, AGREED path forward — "chúng ta sẽ phát triển theo hướng này". Capture content is
  no longer in question; build on it (next: solidify the script's `allsinks` end-to-end, then
  stream the 16kHz-mono-s16le frames to Gemini Live = "AI ear on the call").

### Gemini Live realtime-input design — PARKED 2026-06-20 (do NOT feed the ringback)
Researched the official Live API docs (`docs/gemini_live/`, esp. `live-api/capabilities.md.txt`
VAD section). Conclusions, parked for when we wire the "AI ear" stream:
- **Separate ring↔voice by EVENT, not by audio content.** Ringback is a loud periodic tone; a
  VAD/spectral split mis-fires. The real boundary = the moment the callee answers, which the call
  script already detects sub-second and **foreground-independently** via the WebRTC inbound-track
  count (`__remoteTracks`, `meet_call_browser.py:_webrtc_track_count`) plus 3 fallbacks
  (roster tiles, join toast, "Calling…" indicator gone) OR'd at the `join_fired` line.
- **Do NOT stream the ringback into Gemini Live.** VAD is ON by default and exists to detect
  speech onset → open a turn / interrupt. A sustained ring can (a) be read as speech → the model
  takes a meaningless "turn" and may start talking right as the callee picks up, and (b) its
  on/off cadence can arm/disarm VAD repeatedly → micro-turn spam + wasted tokens + polluted history.
  `proactive_audio` (model can choose not to respond to irrelevant input) does NOT rescue this:
  it's **not supported on Gemini 3.1 Flash Live** and needs `v1alpha`, and it only suppresses the
  response, not the turn machinery.
- **Recommended design = the event-gate we already have + keep auto-VAD for the voice.** Capture
  from ring (no recorder spin-up gap), but only START FORWARDING frames to Gemini at `join_fired`.
  Pre-answer frames (ring) are dropped → the model only ever sees real voice → server handles
  pre-speech buffer (`prefix_padding_ms`) + silence tolerance (`silence_duration_ms`, default ~800ms).
  One gate does both "ring removal" and "clean Gemini input" — no extra audio analysis.
- **Hard-guarantee fallback** (only if we ever stream-from-ring without the join gate): disable
  auto-VAD (`realtimeInputConfig.automaticActivityDetection.disabled=true`) and send `activityStart`
  ourselves at `join_fired` / `activityEnd` at hangup. Trade-off (per docs): manual VAD bypasses the
  server's pre-speech buffer + silence tolerance → must include audio context after activityStart
  and not signal activityEnd too aggressively, else speech clips.
- Refactor needed when we build it: change the capture ffmpeg from file output to a PCM stdout pipe
  (`pipe:1`), read 20ms frames (640 B @16k mono s16le), gate on a `forward_enabled` bool flipped at
  join. For the live mix, one ffmpeg `amix` of the sink monitors → `pipe:1` (no temp files).

### ✅ AI-MOUTH path WORKS — caller injects audio the callee hears (USER-CONFIRMED 2026-06-20)
The reverse of capture: make the CALLER (bot) PLAY audio that the CALLEE hears, via a virtual mic.
Built `scripts/meet_audio_inject.py` (`AudioInjector`) + wired `--inject-audio [FILE]` /
`--inject-at-join` / `--inject-once` into `meet_call_browser.py`. The user answered on their phone and
**HEARD the injected 4-note test tone** — proven end-to-end.
- **How it works**: `module-null-sink ai_mic_sink` + `module-remap-source ai_mic` (master =
  `ai_mic_sink.monitor`) → ai_mic is a real capture device the browser uses as its mic; ffmpeg plays
  the file (`-re`, looping) into ai_mic_sink. setup() runs BEFORE the call-button click and swaps the
  default source to ai_mic; stop() restores the previous default + unloads modules (atexit-guarded).
  Offline-proven by recording the ai_mic SOURCE directly: `python scripts/meet_audio_inject.py
  --verify` → mean −12.5 dB (the chain carries audio).
- **Gotcha 1 — the default-source swap is NOT enough; you MUST move the browser's mic source-output
  onto ai_mic.** Chrome/Meet pins a specific deviceId (remembers the last real mic), so getUserMedia
  ignores the pulse default. `move_browser_mic` relocates it with `pactl move-source-output`.
- **Gotcha 2 — the move MUST RETRY: the browser's real capture stream appears ~2 s AFTER answer.**
  At +7.5 s (REMOTE JOINED) the only source-output is a transient `app='?'`; the real one
  (`app='Brave input'`) shows up ~+9 s. The FIRST live call used a single-shot move at join → missed
  it → moved 0 → the bot transmitted from the real (silent) mic → **callee heard nothing**. The fix
  retries up to 8×/0.7 s and matched `'Brave input'` (→ `brave` keyword) on **attempt 3** →
  `moved 1 browser mic stream → ai_mic`. So the earlier "no sound" attempt was a genuine FAIL (the
  single-shot timing), not a near-miss — don't assume an un-instrumented run succeeded.
- **Gotcha 3 — the mic DEVICE must be allowed once.** Even after the move, the user had to click
  *allow* the mic device in Brave before audio flowed ("sau khi allow mic device thì đã nghe thấy").
  Per-origin permission persists, so subsequent calls should not re-prompt. The script's
  `grant_permissions(["microphone","camera"])` doesn't cover a CDP-attached EXISTING browser's own
  permission state — follow-up: pre-grant / persist so the demo needs no manual click.
- **Mic-on check**: `_ensure_mic_on` clicks "Turn on microphone" if the bot joined muted. This run
  logged "no mic control found yet" but the capture was `corked=n` (already transmitting), so mute
  wasn't the blocker here — the routing was. Kept as cheap insurance.
- **Diagnostics**: `move_browser_mic` logs every source-output (`#id app=… src=… corked=… browser=…`)
  so a silent run is debuggable (this is how the `'Brave input'`/late-stream cause was found).
- **Next**: swap the static tone for Gemini Live's TTS streamed into ai_mic_sink → the AI talks on the
  call. Pair with the AI-ear capture (allsinks) for full bidirectional.

### ✅ gemini_call.py — Gemini Live talks TWO-WAY on the call (USER-CONFIRMED 2026-06-20)
`scripts/gemini_call.py` + `scripts/gemini_voice.py` are the bidirectional follow-on to the
AI-mouth (`ai_call.py`): **Gemini Live is the CALLER, the human is the callee, and they hold a
real two-way voice conversation** over the live Chat call. Confirmed live — the user spoke
(EN+VI), Gemini answered in Vietnamese, incl. a weather question ("Thời tiết Hồ Chí Minh hôm
nay" → "trời nắng, có thể mưa rào… 26–33 độ C"); the call ended on the user's **hang-up**
(`remote audio track ended`, not the duration cap), devices restored clean.
- **Architecture**: `GeminiVoiceBridge` owns the audio + the Gemini session; `meet_call_browser`
  (over CDP, `--watch-join --ensure-mic-on`, **no `--inject-audio`**) places/holds the call. TWO
  virtual PulseAudio devices: MOUTH = `ai_mic_sink` null sink + `ai_mic` remap-source (default
  **source**) — Gemini's 24 kHz audio → ffmpeg → ai_mic_sink → browser mic → callee; EAR =
  `gemini_call_spk` null sink (default **sink**) — browser plays the callee there → ffmpeg records
  `.monitor` at 16 kHz → Gemini. The Gemini Live session (`client.aio.live.connect`, model
  `gemini-3.1-flash-live-preview`, voice Aoede, AUDIO + both transcriptions) runs in a **worker
  thread** (sync Playwright owns the main thread); setup/teardown are sync pactl and restore the
  prev default source+sink.
- **THE EAR FIX — default-sink preset ALONE is NOT enough (first call of this build failed exactly
  like the old capture investigation).** Call 1: MOUTH worked (Gemini greeted, user would hear it)
  but EAR got nothing → Gemini greeted once then sat silent to the cap, **zero `🧑` input
  transcripts**. Cause: Chrome/Brave pins its output device at renderer start, so the call's
  playback stream can keep going to the OLD default sink even though `gemini_call_spk` is now
  default. Fix = on answer, **move the browser's playback sink-input onto `gemini_call_spk`**
  (`GeminiVoiceBridge.move_browser_playback`, mirrors `AudioInjector.move_browser_mic`; matches
  `app='Brave'`). Call 2 logged `sink-input #8609 app='Brave' … moved 1 browser playback stream(s)
  → gemini_call_spk — Gemini's ear is live` and the 2-way convo flowed. So: **mic side = default-
  source preset is enough (proven); speaker side = you MUST move the sink-input.** (The move runs in
  a daemon thread off `on_join` so it never stalls the call poll loop.)
- **Greet must fire on ANSWER, not on connect.** An on-connect greeting plays during the ring and
  is dropped (pre-answer audio never reaches the callee). ⚠️ Join detection can FALSE-FIRE on the
  ringback: the "missed" call logged a join at +7.8s with `tracks=3/base=0` (ringback PCs) and
  greeted to nobody; the REAL answer was +10.8s `tracks=5`. **FIXED (session 2) → see "Greeting
  hardening" below**: greeting now fires on a separate ringback-safe `on_pickup` callback, not
  `on_join`.
- **Validated before the live call**: `python scripts/gemini_voice.py --devices-test` (sinks up +
  ai_mic probe + clean teardown) and `--selftest` (Gemini text→audio→WAV: 2.57s, 24 kHz mono,
  −17 dB — model+key OK). Needs `GEMINI_API_KEY` (env/.env) + `google-genai` (in `igaming`).
- **Echo**: callee should use a phone/headset — the callee-side AEC is what stops Gemini's own
  voice (heard on the callee's speaker → mic → back over WebRTC) from looping into Gemini's ear.

#### Greeting hardening + debug logging + incident-report mode (session 2, USER-CONFIRMED 2026-06-20)
Four follow-on fixes after the user hit greeting-timing + responsiveness issues, all re-confirmed live:
- **Greet on a REAL pickup, never the ringback — AND without needing the callee's audio.**
  `meet_call_browser.main` now has TWO callbacks: `on_join` (early, may fire on the ringback
  track-count bump — side-effect-free) and **`on_pickup`** (the greet trigger). on_pickup fires
  when the join came via a real-answer **DOM** signal — `join_via_dom` = roster tile (tiles≥2) /
  'joined' toast / 'Calling…' indicator gone — which is a true answer **independent of whether
  the callee's mic is on**, so the AI greets even on a SILENT/muted pickup (the user hit: "stayed
  silent, nothing happened, had to unmute to greet" — on_pickup used to need `unmute≥1`/`live_audio≥1`,
  i.e. the callee's audio). If the join was only the WebRTC track-COUNT bump (ringback-prone), it
  falls back to a ringback-safe confirmation (tiles≥2 / unmute / live-audio) before greeting.
  **No settle-window gate** on pickup (these are real-answer signals, not the caller's own track
  ramp) so a FAST answer greets with no delay. The keepalive (focus-emulated + screencast) keeps
  the DOM painting even when the caller window is occluded, so `join_via_dom` still fires.
- **The EAR is GATED until the callee answers AND the greeting is delivered.** `_ear_to_gemini`
  drains+discards ear PCM until `_answered`; `on_pickup` greets → moves the playback → waits for the
  greeting's `turn_complete` (bounded by `GREET_MAX_WAIT=20s`) → THEN opens the ear. Kills two bugs:
  (1) pre-pickup ringback/null-sink-silence that the model mis-transcribed ("Hello hello") and
  babbled a reply to during the ring; (2) the callee's noise/voice cutting the greeting. Contract:
  pick up → AI speaks first, fully, no matter what the callee's side sounds like.
- **🔑 FAST greeting: use `send_realtime_input(text=…)`, NOT `send_client_content`.** This is the
  SAME model-specific lesson `demo_incident_call.py` learned: on `gemini-3.1-flash-live-preview`,
  `send_client_content` only SEEDS history and does NOT trigger an immediate response — so the
  greeting wasn't spoken until the model got a realtime audio turn (= after the callee made a
  sound), the "I waited a long time / it waits for me to speak first" complaint. Swapping to
  `send_realtime_input(text=…)` → **first greeting audio at +0.6s after pickup** (measured in the
  debug log).
- **Mouth latency**: the mouth ffmpeg got `-buffer_duration 80` (PulseAudio output buffer; the
  muxer defaults to ~1–2s) so Gemini's voice reaches the callee ~immediately instead of seconds
  later — the residual "greeting feels late" lag.
- **Debug log + audio recording (`logs/`, gitignored).** `gemini_voice` mirrors every `[voice]`
  event + the full transcript to `logs/gemini_call_<ts>.log` with **elapsed `+N.NNs` stamps** (so
  latency is trivial to read: pickup→greet-sent→first-audio→delivered→ear-open), and records BOTH
  directions to sibling WAVs `_mouth.wav` (Gemini, 24 kHz) + `_ear.wav` (callee, 16 kHz; captures
  the gated pre-pickup audio too). On by default; `--no-record` to skip. Tapped from the exact PCM
  crossing the bridge, so the WAVs are ground truth for "did Gemini hear/say X". Diagnose levels
  with `ffmpeg -i <wav> -af volumedetect -f null /dev/null`.
- **Incident-report mode (`--persona apigw`).** Instead of the generic VN greeting, on pickup the AI
  REPORTS a `data/scenarios.json` incident: `gemini_call.build_incident_persona(persona_id, callee)`
  builds a VN system prompt + opening trigger from the persona's held facts (reuses the apigw
  scenario shared with `run_staff`/`demo_incident_call`). **The AI is a NEUTRAL INTERMEDIARY** ("trợ
  lý trực sự cố") that *relays* the incident on behalf of the on-call owner — it is NOT that engineer
  and does NOT own the incident. So "ai chịu trách nhiệm?" → it names the owner (**Dave** for apigw,
  derived via `_reporter_name(role)` from the scenario `role`), **never itself**; and it answers
  STRICTLY from the report, explicitly saying "không có trong báo cáo em đang nắm, để em hỏi lại …"
  for anything not in it (no guessing/fabrication). Renamed the apigw scenario owner Alex→Dave
  (2026-06-20, user request) so the intermediary cites a clearly-distinct human, not the AI itself.
  Earlier design had the AI BE the on-call engineer (Alex) — superseded. Run:
  `gemini_call.py --persona apigw --callee Duc`.
- **Remaining polish**: flush the mouth ffmpeg buffer on barge-in (interruption clears the queue but
  ffmpeg's now-small buffer still drains a little); the move-on-pickup adds ~0.6s before the ear
  opens (acceptable — the greeting covers it).

#### Greeting-latency root-cause fixes (session 3, USER-CONFIRMED 2026-06-20)
User reported the AI took ~15-22s to greet after they answered (silence on pickup). Added a
`--diag-pickup` flag (in BOTH `gemini_call.py` and `meet_call_browser.py`) that logs the bot's
JOIN flow and pickup loop with elapsed stamps — that instrumentation pinpointed TWO real delays,
neither of them noise/model/transport (those were red herrings I wrongly blamed first):
- **`[ring]` stamps** = the pickup-detection loop (per-poll signal snapshot + effective cadence).
- **`[join]` stamps** = the bot's own join flow (nav → call button found → clicked → call page →
  green-room "Join now"). This is the window BEFORE the callee's phone even rings, but the bot
  can't SPEAK until it's through, so a slow step here IS the perceived greeting latency.
The two bugs found & fixed in `meet_call_browser.py`:
1. **5s wasted meeting-code lookup.** After placing the call, a retry loop polled the call URL up
   to 5×1s for the Meet meeting code — which only feeds `--watch-rest` (the REST room-watch), NOT
   used by `gemini_call`. It blocked the pickup loop from starting for ~5s. FIX: gate that retry
   loop (and its "couldn't parse" warning) behind `args.watch_rest`. The instant
   `_extract_meeting_code(url)` stays (no wait).
2. **~38s double-click stall (the dominant bug).** The call-button click was
   `with context.expect_page(timeout=8_000): button.click()` then, on the `except` (no popup), a
   SECOND `button.click()`. In this embedded Chat DM the call opens IN-PLACE (a Meet iframe, no
   popup), so EVERY call: the 1st click succeeds instantly, expect_page waits the full 8s for a
   popup that never comes, then the 2nd click runs on a now-DETACHED button → Playwright stalls on
   its ~30s actionability timeout. `[join]` proved it: button FOUND +2.1s, CLICKED +40.2s. FIX:
   click EXACTLY ONCE, wait only ~3s for a popup, default to in-place; never re-click. (A wrong
   first hypothesis — Wayland renderer suspension making the button un-actionable — was tried via
   a pre-click `_keepalive_renderer(page)` and DISPROVED: the click still took 40s, and the 1st
   click returning instantly proves the button was always actionable. That change was reverted.)
RESULT (user-confirmed): bot join ~40s → ~11s; silence-after-pickup ~15-22s → **~3s** (model TTFB
0.6s + transport). The remaining ~11s join is BEFORE the callee's phone rings (green-room render
~6s + popup wait 3s + nav/find ~2s) so it does NOT count toward perceived latency. `--diag-pickup`
is gated off by default — keep it; it's the tool that found both bugs. **Lesson**: when a "latency"
symptom appears, MEASURE each stage with elapsed stamps before theorizing — I burned two rounds on
noise/transport/renderer guesses that the stamps refuted in one call each.

### ✅ ai_call.py — dedicated PRE-GRANTED browser removes the manual mic-allow + the move-dance (USER-CONFIRMED 2026-06-20)
`scripts/ai_call.py` is the minimal entry point for the AI-mouth direction: it launches a DEDICATED
caller Brave (`.browser-profile-caller`, port 9333) with `--use-fake-ui-for-media-stream`, runs a
login gate, then delegates ring+join+inject to `meet_call_browser.main` over CDP. The user heard the
tone with **NO manual allow click** — the three gotchas above are all resolved by launching OUR OWN
browser instead of CDP-ing into the daily one:
- **Mic-allow gone (was Gotcha 3)**: `--use-fake-ui-for-media-stream` auto-accepts getUserMedia.
- **Move-dance now MOOT (was Gotcha 1+2)**: the flag also binds getUserMedia to the DEFAULT capture
  device, and `AudioInjector.setup()` sets default-source=ai_mic BEFORE the call's getUserMedia fires,
  so the browser grabs ai_mic FROM THE START. This run's `move_browser_mic` found no `browser=y`
  source-output and logged **"no browser mic found … callee will hear silence" — a FALSE alarm: the
  callee heard the tone fine.** The live capture was `#8140 app='?'` (Chrome's WebRTC capture exposes
  no `application.name` → fails the `brave`/`chrom` keyword match → tagged `browser=n`), already
  reading ai_mic via the default-source preset. So on the pre-granted path the move is
  belt-and-suspenders, NOT the mechanism — don't trust that "silence" warning here. (Possible polish:
  have `move_browser_mic` treat "a source-output already on ai_mic" as success to silence the false
  alarm; left undone — harmless.)
- **Login survived**: `.browser-profile-caller` (last signed in 2026-06-19) was still live on a PLAIN
  (non-Playwright) launch — the flagged-account sign-out warnings did NOT bite a long-lived dedicated
  profile reused via plain-launch + CDP. The browser is left RUNNING on exit (default) for instant reuse;
  `--quit-browser` stops it (a /proc scan by profile path, never `pkill -f <profile>` — that self-matches
  this python). Plain-launch (not Playwright `launch_persistent_context`) is deliberate: fewer automation
  tells → better Google-login survival.
- **Cost**: the join still fired late (+33.2 s; `tiles=0, tracks=5, via=webrtc` — tab was backgrounded,
  so DOM roster read 0 and only the WebRTC counter caught it). Irrelevant to audio (from-ring playback
  reaches the callee on pickup regardless of join detection).
- **OS-sink voice capture — the full 5-call investigation (2026-06-19).** TL;DR: capturing
  the remote voice at the OS sink is **PROVEN + user-confirmed** (call 4 → captured_voice_proof.wav,
  ring + callee voice), but the integrated script capture is **gated by media-flow stability**,
  not by the capture code. The remaining live blocker is whether Brave's occluded/backgrounded
  renderer reliably DECODES the remote audio at all — not where/how we record it.
  - ⚠️ **VERIFICATION-ERROR LESSON (own mistake): check the volume TIMELINE, not the aggregate.**
    Call 1 (`--audio-mode monitor --capture-from-ring`) produced a 23.1s WAV at mean −28.2 dB and
    I declared success. WRONG: a per-3s breakdown showed the energy was ALL the ringback (3-13s
    loud), the post-answer VOICE window (13-23s) was −91 dB silent. The user caught it ("ring bắt
    ngon, voice thì k bắt được"). Aggregate mean_volume is dominated by the loudest segment — always
    `ffmpeg -ss S -t N … volumedetect` per window before claiming a non-silent capture.
  - **The call audio's sink can VARY between calls** → never lock ONE sink. Call 1: ring on the
    HDA sink `…sofhdadsp__sink.2` (idx 134, the default), voice apparently elsewhere → monitor-on-134
    missed it. Call 4 (per-second `pactl list sink-inputs` logger + a separate ffmpeg on EACH of the
    4 HDA sink monitors, no script capture): the voice was on **sink 134**, captured −21 dB for ~30s;
    the other 3 sinks recorded −91 dB. So the robust capture records ALL sinks → `--audio-mode
    allsinks`.
  - **`tiles=0` (backgrounded/occluded call tab) does NOT block OS audio.** Call 4 captured the
    voice with `REMOTE JOINED … tiles=0` — the audio still flowed to sink 134. So OS-sink capture is
    window-state-independent *when the media flows*. (Earlier "background throttles the voice"
    hypothesis was DISPROVEN by call 4.)
  - **`--audio-all-apps` (isolate + move EVERY sink-input to a null sink) FAILED live (calls 2,3).**
    The mover never relocated Brave's streams (no `routed`/`move failed` log) even though the logger
    saw Brave "Playback" sink-inputs on sink 134 — root cause never pinned (the identical move works
    in offline repros incl. late-appearing streams, and pactl works under `conda run`). Recording the
    null sink → −91 dB silence both runs. Lesson: prefer the NO-MOVE path (`allsinks`) over moving
    streams; a silent mover is a blind failure. Debug logging added to the mover anyway
    (`[mover] pactl rc=… total_sink_inputs=… matched=… ids=…`).
  - **`allsinks` = the chosen capture: ONE ffmpeg recorder PER sink monitor → mixed (amix
    normalize=0) at stop.** Deliberately per-sink, NOT a single multi-input amix process — the
    per-sink layout is the exact config that captured live (call 4); independent recorders can't let
    one sink's state stall another. (A single-amix `allsinks` was tried in call 5 → −91 dB silence,
    but that call most likely had NO media flow, so it's not conclusive proof amix is worse; per-sink
    is chosen because it's the only live-proven config.) Offline-verified both immediate and
    late-appearing (+10s) tones, temps auto-cleaned. **NOT yet live-verified end-to-end for the
    voice** (user halted testing after call 5 — "đừng gọi nữa").
  - **Hang-up detection is foreground-gated.** Call 1 (`tiles=1`): clean `📴 Call ended — call
    controls disappeared`, self-terminated. Calls 3,4,5 (`tiles=0`): `⏱ Reached the 150s cap with no
    hang-up detected` — the DOM teardown signals (controls/roster/frame) need the tab rendering, and
    the OS-mode/WebRTC end signals didn't fire while backgrounded. So hang-up auto-detect works only
    when the caller's call tab is FOREGROUND.
  - **NEW flags/modes** (`meet_call_browser.py`): `--capture-from-ring` (start recorder at
    placement, include ringback), `--audio-all-apps` (isolate match-all — deprecated by the above,
    kept for diagnosis), `--audio-mode allsinks` (the per-sink-record-all + merge capture).
  - **Net status for the user's ask** ("capture ring + callee voice; bonus: detect end-call"):
    RING capture ✅ (call 1); VOICE capture ✅ as a TECHNIQUE (call 4, −21 dB) but the script's
    `allsinks` path is not yet live-confirmed for the voice; END-CALL detect ✅ only when the call
    tab is foreground. The gating live blocker is media-flow reliability on the occluded daily-Brave
    renderer (the long-standing Wayland media-stability issue), addressable by keeping the Brave call
    window foreground/unoccluded or running the caller under a non-occluding display.

## Harness lessons (build)
- **Cursor parallel relay deadlock.** Launching both `cursor-agent` models simultaneously inside a
  workflow agent hit a `.cursor/cli-config.json.tmp` rename ENOENT (killing one model) **and** a
  `pgrep -f "model composer-2.5"` wait-loop that **self-matched its own command line** and hung. Fix
  (baked into later workflow prompts): stagger the launches (`sleep 5` between the two) and rely only
  on the shell's `&` + `wait` builtins — never a pgrep/ps wait-loop.
- **Workflow scripts are plain JS**: a literal triple-backtick inside a template-literal agent prompt
  closed the string and broke parsing — write "three-backtick" in prose instead.
