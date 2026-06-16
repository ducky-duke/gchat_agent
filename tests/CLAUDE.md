# tests/ — offline functional gate

All tests run **offline** (MockLLM + FakeChatClient, no key/network) and are *the*
correctness gate for every change. Run them:

    PYTHONPATH=src python -m unittest discover -s tests -t . -p "test_*.py"

Syntax-only check (no `ty` here): `python -m py_compile <file>`.

- **`fakes.py`** — `FakeChatClient(me=...)`: in-memory `ChatClient` double. `.inject(sender,
  text, thread_id=...)`, `.messages`, `.me()`. Stand-in for `chat/google_rest.py`.

## Test map
- **`test_loop.py`** — crown-jewel end-to-end clarification round-trip.
- **`test_duplicate_questions.py`** — loop-breaker / decline handling
  (`DeclineDoesNotAbandonUnaskedFactsTest`: a decline of the asked questions must not
  abandon still-unasked core facts).
- **`test_self_filter.py`** — bot never detects/clarifies its OWN account's messages.
- **`test_runner_hardening.py`** — runner/state hardening (largest; review-driven).
- **`test_analyzer.py`**, **`test_issue_store.py`**, **`test_models.py`**,
  **`test_config.py`** — core unit coverage.
- **`test_llm_base.py`** (JSON extraction), **`test_llm_mock.py`**,
  **`test_llm_openrouter.py`** — LLM layer.
- **`test_rag.py`** — retrieval stack.
- **`test_report.py`**, **`test_voice_report.py`**, **`test_tts.py`** — report + voice.
- **`test_staff.py`** — staff personas. **`test_google_rest.py`** — live-adapter timeouts.
- **`test_fakes.py`** — the test double itself.

When you add a behavior, add/extend the matching `test_*.py` in the same change.
