"""gchat_agent — Google Chat issue-spotter AI agent.

See PLAN.md for the full design. The foundation layer (config, models, and the
LLM / chat / RAG base protocols) is pure stdlib; the core LLM transport —
`google-genai` (the active Gemini path) and the legacy `openai`/OpenRouter path —
is lazy-imported (so the mock/CI path needs no install or key), and observability
(`langfuse`) and other backends are optional extras.
"""

__version__ = "0.1.0"
