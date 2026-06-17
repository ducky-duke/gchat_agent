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
- **`prompts.py`** — single source of truth for every LLM contract: `detect_prompt`,
  `clarity_prompt`, `questions_prompt`, `resolution_prompt`, `narration_prompt`.
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
- **`staff.py`** — the two LLM staff personas. `StaffAgent.seed()` / `.answer_question()`;
  `load_personas()` reads `data/scenarios.json`. Personas post as HUMAN senders.
- **`state.py`** — `IssueStore`: persistent issue store under `.state/`. Upsert with
  jaccard-similarity dedup/merge, tombstones, poll cursor, and persisted `bot_user_id`
  (self-filter precedence layer). `recent_closed(limit)` feeds episodic recall; `save()`
  writes a `<state_file>.bak` of the last-known-good state before the atomic replace.
