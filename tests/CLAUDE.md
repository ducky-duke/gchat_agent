# tests/ — offline functional gate

All tests run **offline** (MockLLM + FakeChatClient, no key/network) and are *the*
correctness gate for every change. Run them:

    PYTHONPATH=src python -m unittest discover -s tests -t . -p "test_*.py"

Syntax-only check (no `ty` here): `python -m py_compile <file>`.

- **`fakes.py`** — `FakeChatClient(me=...)`: in-memory `ChatClient` double. `.inject(sender,
  text, thread_id=...)`, `.messages`, `.me()`. Stand-in for `chat/google_rest.py`. Also
  `InlineExecutor` (synchronous background-task double) and `FakeGitHubClient` (records
  filed issues; `fail=True` to exercise the never-crash export path).

## Test map
- **`test_loop.py`** — crown-jewel end-to-end clarification round-trip.
- **`test_duplicate_questions.py`** — loop-breaker / decline handling
  (`DeclineDoesNotAbandonUnaskedFactsTest`: a decline of the asked questions must not
  abandon still-unasked core facts).
- **`test_self_filter.py`** — bot never detects/clarifies its OWN account's messages.
- **`test_runner_hardening.py`** — runner/state hardening (largest; review-driven).
  `RunForeverResilienceTest.test_failed_cycle_is_swallowed_then_loop_continues` patches
  `gchat_agent.runner._sleep` (the loop's OWN pacing seam), NOT the global `time.sleep`.
  This was a former ~1/5 full-suite flake: the old `mock.patch("time.sleep")` was global,
  so a concurrent background-thread sleep from a neighboring test (jittered
  `_retry.backoff_delay`) leaked into the mock and inflated `assert_called_once()`. The
  fix routes `run_forever`'s sleeps through `runner._sleep` so only the loop's own sleeps
  are observed — keep the assertion strict; do NOT revert to patching the global.
- **`test_goclaw_hardening.py`** — the goclaw-inspired batch: `validate_config`, token
  usage, `llm/_retry` (Retry-After/jitter), `redact_secrets`, episodic recall, the
  prompt-injection guard (`PromptInjectionGuardTest` — UNTRUSTED framing on every
  transcript-bearing builder; `InjectionEndToEndTest` — a seeded hijack attempt never
  reaches a bot post), state `.bak`, and `_issue_query` reply-blending.
- **`test_analyzer.py`**, **`test_issue_store.py`**, **`test_models.py`**,
  **`test_config.py`** — core unit coverage.
- **`test_llm_base.py`** (JSON extraction), **`test_llm_mock.py`**,
  **`test_llm_openrouter.py`** — LLM layer.
- **`test_rag.py`** — retrieval stack.
- **`test_report.py`**, **`test_voice_report.py`**, **`test_tts.py`** — report + voice.
- **`test_github_export.py`** — GitHub issue export: payload/transcript renderers, the REST
  client's unknown-label fallback, `build_github` + config validation, and the runner's
  background export (off the critical path; never crashes a resolve).
- **`test_call_on_resolve.py`** — the outbound voice call on resolve (`CALL_ON_RESOLVE`):
  `build_call_incident` payload renderer + the runner spawn (`subprocess.Popen` patched —
  off-critical-path, gate-respecting, serialized one-at-a-time, never-crash contracts).
- **`test_staff.py`** — staff personas. **`test_google_rest.py`** — live-adapter timeouts.
- **`test_fakes.py`** — the test double itself.

When you add a behavior, add/extend the matching `test_*.py` in the same change.
