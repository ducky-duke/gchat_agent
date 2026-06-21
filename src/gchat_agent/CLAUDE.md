# src/gchat_agent — package index

The `gchat_agent` package (src layout). Behavioral specs (loop-breaker, self-filter,
voice, merged-LLM-calls) live in the **root [`CLAUDE.md`](../../CLAUDE.md)** — this file
maps *where the code is*. Docstrings cite `§N` = sections of [`PLAN.md`](../../PLAN.md).

## Top-level modules
- **`config.py`** — `Config` dataclass + `load_config()` (which now `validate_config`s the
  result — fail-fast enum/range checks, keyless `mock` path stays valid); env-driven settings
  with a stdlib `.env` parser (`_parse_env_file`/`_clean_value` handle empty-value-comment
  lines). Tunables: `MAX_CLARIFY_ROUNDS`, `MAX_NO_PROGRESS_ROUNDS`, `STALE_AFTER_IDLE_CYCLES`,
  `RESOLVE_CONFIDENCE_THRESHOLD`, `EPISODIC_RECALL` (default on), `SEMANTIC_DEDUP` (default on —
  LLM cross-thread duplicate decider), `REDACT_REPORTS` (default off), the safety-mode + voice +
  provider flags, and the `CALL_*` cluster (`CALL_ON_RESOLVE` + script/callee/language/url/owner/
  log-dir — the outbound voice call on resolve).
- **`models.py`** — domain dataclasses, lossless JSON round-trip. Enums `SenderType`/
  `Severity`/`Status`; `QAPair`, `Message`, `Issue`, `ClarityAssessment`,
  `ResolutionReport`, `Conversation`, `AgentState`; `issue_fingerprint()`. `Issue` carries
  the loop-breaker bookkeeping (`missing_info`, `last_missing_info`, `no_progress_rounds`,
  `pending_questions`, `questions_asked`).
- **`runner.py`** — the orchestration loop. `Runner` (cycle → detect → clarify → resolve/
  escalate), `build_runner()` factory, single-instance file-lock, and the module-level
  `_looks_like_decline()` heuristic. Key methods: `run_cycle`, `_detect`,
  `_process_open_issues`, **`_step_issue`** (loop-breaker), `_ask`, `_resolve`,
  `_escalate_due`, `_redirect_out_of_thread`, `_deliver_voice_bg` (background voice,
  off the resolve critical path — see root CLAUDE.md "Lever C"), `_publish_issue_bg`
  (background GitHub export — see root CLAUDE.md "GitHub issue export"),
  `_maybe_place_call` + module-level `build_call_incident` (the outbound voice call
  on resolve — a detached `gemini_call.py` subprocess; see root CLAUDE.md "Outbound
  voice call on resolve"), `run_forever`.
- **`observability.py`** — Langfuse shim (`observe`/`trace`/`flush`); no-op by default,
  lazy when `LANGFUSE_*` is set. `@observe` is wired onto the 5 LLM boundaries (3 analyzer
  methods + 2 report builders).

## Subpackages (each has its own CLAUDE.md)
- **`agent/`** — the brain: detection · clarity · resolution · personas · persistence.
- **`llm/`** — LLM/TTS transport (protocol, MockLLM, OpenRouter, provider factories).
- **`chat/`** — Google Chat ingress/egress adapters + user-OAuth (stdlib urllib).
- **`github/`** — optional GitHub issue export (Protocol + stdlib-urllib REST client +
  `build_github`). Files each resolved issue off the resolve critical path — see root
  CLAUDE.md "GitHub issue export".
- **`meet/`** — optional Google Meet link minting (Protocol + stdlib-urllib REST client +
  `build_meet`, gated by `MEET_LINKS`). Mints a meeting + shares the join link in Chat so a
  HUMAN joins the incident call — an AI can't speak on the Meet (Media API is receive-only) —
  see root CLAUDE.md / [`docs/google_meet/`](../../docs/google_meet/).
- **`rag/`** — retrieval stack (BM25 + boosts + optional dense + RRF fusion).
