# llm/ — LLM + TTS transport

Pure stdlib except the **lazy-imported `openai`** (live transport — not auto-installed in
`igaming`: `conda run -n igaming pip install openai`). Offline path needs neither.

- **`base.py`** — `LLMClient` Protocol (`chat`, `complete_json`) + robust JSON extraction:
  `extract_json()` / `extract_json_value()` scan balanced braces and strip code fences, so
  a chatty model wrapping JSON in prose still parses.
- **`mock.py`** — `MockLLM`: deterministic, rule-based stand-in. The offline path
  (`LLM_PROVIDER=mock`) — no network/key. Emits inline `clarifying_questions` /
  clarity `questions` (`_questions_from_missing`) to mirror the live merged-call contract;
  used everywhere in tests.
- **`openrouter.py`** — `OpenRouterClient` + the `build_llm(config)` provider factory.
  Transient-error retry (`_is_transient`), reasoning + quantization headers, request
  timeout. Hardened across deepseek/glm/minimax/grok (see root MEMORY.md).
- **`tts.py`** — `TTSClient` Protocol, `OpenRouterTTS` (OpenRouter `audio.speech`),
  `MockTTS`, and `build_tts(config)` factory. Drives voice reports; returns `None` when
  voice delivery is off.
