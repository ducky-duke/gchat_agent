# agent/ — the bot's brain

Detection → clarification → resolution, plus the demo staff personas and issue
persistence. The LLM contracts and report shape live here; the *orchestration* that drives
them is `runner.py` (parent dir).

- **`analyzer.py`** — the single retrieval-augmented `Analyzer`. `detect_issues()`,
  `assess_clarity()`, `generate_questions()`. Owns RAG retrieval (`_retrieve_context`),
  cited-id resolution (`_resolve_cited_id` — model-portable id formats), and thread
  anchoring (`_anchor_thread`). Detection emits opening `clarifying_questions` and clarity
  emits the next `questions` inline (Lever 1 merged calls).
- **`prompts.py`** — single source of truth for every LLM contract: `detect_prompt`,
  `clarity_prompt`, `questions_prompt`, `resolution_prompt`, `narration_prompt`.
  `_asked_block()` injects already-asked questions so the model never repeats one
  (loop-breaker layer 1; layer 2 is the deterministic guard in `runner._step_issue`).
- **`report.py`** — resolution report: `build_resolution_report`, `render_markdown`,
  `write_report`, `confirmation_line`. Voice path: `build_narration`, `voice_caption`,
  `voice_message_text` (transcript carried in-thread). `ResolutionReport.open_questions`
  → the "Open questions" section used when an issue closes *with gaps*.
- **`staff.py`** — the two LLM staff personas. `StaffAgent.seed()` / `.answer_question()`;
  `load_personas()` reads `data/scenarios.json`. Personas post as HUMAN senders.
- **`state.py`** — `IssueStore`: persistent issue store under `.state/`. Upsert with
  jaccard-similarity dedup/merge, tombstones, poll cursor, and persisted `bot_user_id`
  (self-filter precedence layer).
