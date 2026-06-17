# agent/ — the bot's brain

Detection → clarification → resolution, plus the demo staff personas and issue
persistence. The LLM contracts and report shape live here; the *orchestration* that drives
them is `runner.py` (parent dir).

- **`analyzer.py`** — the single retrieval-augmented `Analyzer`. `detect_issues()`,
  `assess_clarity()`, `generate_questions()` (all `@observe`-traced). Owns RAG retrieval
  (`_retrieve_context`), cited-id resolution (`_resolve_cited_id` — model-portable id
  formats), and thread anchoring (`_anchor_thread`). Detection emits opening
  `clarifying_questions` and clarity emits the next `questions` inline (Lever 1 merged
  calls). `detect_issues(conv, prior_issues=…)` takes recently-closed issues for episodic
  recall; `_issue_query` blends the reporter's latest reply into the retrieval query.
  `match_duplicate_issue(candidate, open_issues)` is the LLM cross-thread duplicate
  decider (semantic dedup the lexical bar can't do — see root CLAUDE.md "Cross-thread
  dedup/merge"); returns the matched open issue or None (best-effort: errors → None).
- **`prompts.py`** — single source of truth for every LLM contract: `detect_prompt`,
  `clarity_prompt`, `questions_prompt`, `resolution_prompt`, `narration_prompt`,
  `duplicate_match_prompt` (cross-thread same-incident decision → `{"duplicate_of": int|null}`).
  `_asked_block()` injects already-asked questions so the model never repeats one
  (loop-breaker layer 1; layer 2 is the deterministic guard in `runner._step_issue`).
  `_ROLE`/`_render_user` carry a **prompt-injection guard** (transcript + retrieved
  context marked UNTRUSTED). `_prior_issues_block` renders episodic recall for detection
  (`#`-stripped so MockLLM detection stays inert on it).
- **`report.py`** — resolution report: `build_resolution_report` + `build_narration`
  (both `@observe`-traced), `render_markdown`, `write_report` (optional `redact=` →
  `redact_secrets`, the off-by-default report-only secret masker), `confirmation_line`.
  Voice path: `voice_caption`, `voice_message_text` (transcript carried in-thread).
  `ResolutionReport.open_questions` → the "Open questions" section used when an issue
  closes *with gaps*.
- **`staff.py`** — the LLM staff personas. `StaffAgent.seed()` / `.answer_question()`;
  `load_personas()` reads `data/scenarios.json` (`ops`/`promo`/`apigw` incidents,
  `noise` control, `dupe` 2nd-reporter, `injection` prompt-injection attempt).
  `noise`/`injection` are seed-and-go (no `facts` → never answer). Personas post as
  HUMAN senders.
- **`state.py`** — `IssueStore`: persistent issue store under `.state/`. Upsert dedup/merge
  tiers: fingerprint → same-thread jaccard → cross-thread lexical (`_find_cross_thread_duplicate`,
  0.65) → optional LLM decider via the `semantic_match` callback (`_semantic_open_match`, gated
  by the `_SEMANTIC_DEDUP_HINT` lexical floor; store stays pure — callback is None offline/tests).
  Plus tombstones, poll cursor, persisted `bot_user_id` (self-filter layer). `recent_closed(limit)`
  feeds episodic recall; `save()` writes a `<state_file>.bak` before the atomic replace.
