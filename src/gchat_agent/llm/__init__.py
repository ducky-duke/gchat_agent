"""LLM layer — the `LLMClient` protocol and its backends (OpenRouter, Mock)."""
from __future__ import annotations

from gchat_agent.llm.openrouter import build_llm

__all__ = ["build_llm"]
