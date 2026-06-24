"""LLM layer — the `LLMClient` protocol and its backends (Gemini, OpenRouter, Mock).

`GeminiClient` (`gemini.py`) is the live default (google-genai + GEMINI_API_KEY);
`OpenRouterClient` (`openrouter.py`, `openai` SDK) is the legacy path, kept but no
longer selected by default; `MockLLM` (`mock.py`) is the offline/test backend.
"""
from __future__ import annotations

from gchat_agent.llm.openrouter import build_llm

__all__ = ["build_llm"]
