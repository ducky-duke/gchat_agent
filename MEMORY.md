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

## Harness lessons (build)
- **Cursor parallel relay deadlock.** Launching both `cursor-agent` models simultaneously inside a
  workflow agent hit a `.cursor/cli-config.json.tmp` rename ENOENT (killing one model) **and** a
  `pgrep -f "model composer-2.5"` wait-loop that **self-matched its own command line** and hung. Fix
  (baked into later workflow prompts): stagger the launches (`sleep 5` between the two) and rely only
  on the shell's `&` + `wait` builtins — never a pgrep/ps wait-loop.
- **Workflow scripts are plain JS**: a literal triple-backtick inside a template-literal agent prompt
  closed the string and broke parsing — write "three-backtick" in prose instead.
