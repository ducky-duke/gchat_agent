# llm/ — LLM + TTS transport

Pure stdlib except the **lazy-imported** SDKs: **`google-genai`** (the live default,
`gemini.py`) and **`openai`** (the legacy OpenRouter path, kept but unused by default).
Both import inside methods, so the offline `mock` path needs neither key nor SDK.

- **`base.py`** — `LLMClient` Protocol (`chat`, `complete_json`) + robust JSON extraction:
  `extract_json()` / `extract_json_value()` scan balanced braces and strip code fences, so
  a chatty model wrapping JSON in prose still parses.
- **`mock.py`** — `MockLLM`: deterministic, rule-based stand-in. The offline path
  (`LLM_PROVIDER=mock`) — no network/key. Emits inline `clarifying_questions` /
  clarity `questions` (`_questions_from_missing`) to mirror the live merged-call contract;
  also exposes `usage_snapshot()` (estimated ~chars/4) so the runner's token log works
  offline. Used everywhere in tests.
- **`_retry.py`** — shared transient-retry helpers for openrouter+tts: `is_transient`,
  `retry_after_seconds` (parses `Retry-After` across `exc.response.headers` AND bare
  `exc.headers`; integer-seconds only), `backoff_delay` (server hint, else exp backoff
  with full jitter, capped). `chat/google_rest.py` mirrors the same logic.
- **`gemini.py`** — `GeminiClient`, the **live default**: Gemini API via the
  `google-genai` SDK, authenticating with **`GEMINI_API_KEY`** (the SAME key the Gemini
  Live call uses). `chat`/`complete_json` over `models.generate_content`; JSON tasks set
  `response_mime_type=application/json` (still post-parsed by `extract_json`). No
  `temperature`/`top_p`/`top_k` (Gemini 3.x is tuned for defaults); thinking depth via
  `GEMINI_THINKING_LEVEL`. Transient retry (via `_retry`), request timeout (HttpOptions ms),
  cumulative token accounting (`usage_snapshot()` ← `response.usage_metadata`). Model =
  `GEMINI_MODEL` (default `gemini-3.5-flash`).
- **`openrouter.py`** — **legacy** `OpenRouterClient` (`openai` SDK → OpenRouter) + the
  shared `build_llm(config)` provider factory (selects gemini | mock | openrouter). Kept
  for reference; not selected by the default config. Transient-error retry (via `_retry`),
  reasoning + quantization headers, request timeout, cumulative token accounting
  (`usage_snapshot()` ← `response.usage`). Hardened across deepseek/glm/minimax/grok (root MEMORY.md).
- **`tts.py`** — `TTSClient` Protocol, `OpenRouterTTS` (OpenRouter `audio.speech`),
  `MockTTS`, and `build_tts(config)` factory. **Legacy** voice-report TTS — spoken delivery
  is now the Gemini Live call on resolve (`CALL_ON_RESOLVE`), so `build_tts` returns `None`
  on the `gemini` provider (graceful disk fallback) and on `REPORT_DELIVERY=disk`. The
  `openrouter` provider still drives it. `.env` keeps `REPORT_DELIVERY=disk`.
