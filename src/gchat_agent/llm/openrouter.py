"""OpenRouter-backed `LLMClient` + the `build_llm` provider factory (§5.3).

`OpenRouterClient` wraps the official **`openai` SDK** pointed at OpenRouter's
OpenAI-compatible endpoint. The `openai` import is **lazy** (inside methods) so
nothing third-party is required on the mock/CI path. When
``config.OBSERVABILITY == "langfuse"`` the client is sourced from
``langfuse.openai`` instead — a drop-in for `openai` with the identical API — so
every call is auto-traced with no call-site changes (§5.9).

`complete_json` runs a single-turn completion and pulls a JSON object out of the
(possibly fenced / chatty) text via the foundation's `extract_json`, since not
all OpenRouter models honor `response_format`.

Stdlib only at module top level; `openai`/`langfuse` are imported lazily.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from gchat_agent.llm.base import LLMClient, extract_json

if TYPE_CHECKING:  # type-only; no runtime import
    from gchat_agent.config import Config


# Default headers OpenRouter recommends for attribution / ranking.
_HTTP_REFERER = "https://github.com/gchat-agent"
_X_TITLE = "gchat-agent"

# Extra application-level backoff on transient failures (the SDK retries too).
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.5  # seconds; doubled each attempt


class OpenRouterClient:
    """An `LLMClient` backed by OpenRouter via the `openai` SDK (§5.3)."""

    def __init__(self, config: "Config") -> None:
        self._config = config
        self._model = config.OPENROUTER_MODEL
        self._base_url = config.OPENROUTER_BASE_URL
        self._api_key = config.OPENROUTER_API_KEY
        self._use_langfuse = config.OBSERVABILITY == "langfuse"
        self._client: Any | None = None  # lazily constructed

    # --- client construction (lazy import) -----------------------------------
    def _get_client(self) -> Any:
        """Construct (once) and return the OpenAI-compatible client.

        Imports `openai` lazily; when observability is on, sources the drop-in
        client from `langfuse.openai` so calls are auto-traced.
        """
        if self._client is not None:
            return self._client
        if self._use_langfuse:
            # langfuse.openai re-exports OpenAI with the identical constructor.
            from langfuse.openai import OpenAI  # lazy, optional dep
        else:
            from openai import OpenAI  # lazy, optional dep
        self._client = OpenAI(base_url=self._base_url, api_key=self._api_key)
        return self._client

    @property
    def _default_headers(self) -> dict[str, str]:
        return {"HTTP-Referer": _HTTP_REFERER, "X-Title": _X_TITLE}

    # --- transient-failure detection -----------------------------------------
    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """True for 429 / 5xx-style errors worth an extra backoff retry."""
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if isinstance(status, int) and (status == 429 or 500 <= status < 600):
            return True
        name = exc.__class__.__name__.lower()
        if "ratelimit" in name or "apiconnection" in name or "internalserver" in name:
            return True
        text = str(exc).lower()
        return "429" in text or "rate limit" in text or "resource_exhausted" in text

    def _create(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        """Call chat.completions.create with extra backoff on 429/5xx."""
        client = self._get_client()
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    extra_headers=self._default_headers,
                    **kwargs,
                )
            except Exception as exc:  # noqa: BLE001 - re-raised below if fatal
                last_exc = exc
                if attempt < _MAX_RETRIES - 1 and self._is_transient(exc):
                    time.sleep(_BASE_BACKOFF * (2 ** attempt))
                    continue
                raise
        # Unreachable in practice (loop either returns or raises).
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _content(response: Any) -> str:
        """Extract the assistant text from a chat-completions response."""
        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError, KeyError, TypeError):
            return ""
        return text or ""

    # --- LLMClient protocol --------------------------------------------------
    def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        """Return the assistant text for a system prompt + message list."""
        full: list[dict[str, str]] = [{"role": "system", "content": system}]
        full.extend(messages or [])
        response = self._create(full, temperature=0.3)
        return self._content(response)

    def complete_json(
        self,
        system: str,
        user: str,
        schema_hint: str | None = None,
    ) -> dict[str, Any]:
        """Single-turn completion -> parsed JSON object (via `extract_json`)."""
        sys_text = system if not schema_hint else f"{system}\n\n{schema_hint}"
        messages = [
            {"role": "system", "content": sys_text},
            {"role": "user", "content": user},
        ]
        # Request a JSON object when the model supports it; gracefully retry
        # without response_format if the model rejects it.
        try:
            response = self._create(
                messages,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        except Exception:  # noqa: BLE001 - model may not honor response_format
            response = self._create(messages, temperature=0.0)
        return extract_json(self._content(response))


def build_llm(config: "Config") -> LLMClient:
    """Provider factory (§5.3).

    Returns `MockLLM()` when `LLM_PROVIDER == "mock"`. For `"openrouter"`, raises
    a clear `RuntimeError` if no API key is set; otherwise returns an
    `OpenRouterClient`.
    """
    provider = (config.LLM_PROVIDER or "").strip().lower()
    if provider == "mock":
        from gchat_agent.llm.mock import MockLLM  # local import; no third-party

        return MockLLM()
    if provider == "openrouter":
        if not config.OPENROUTER_API_KEY:
            raise RuntimeError("set OPENROUTER_API_KEY or LLM_PROVIDER=mock")
        return OpenRouterClient(config)
    raise RuntimeError(
        f"unknown LLM_PROVIDER={config.LLM_PROVIDER!r}; use 'mock' or 'openrouter'"
    )
