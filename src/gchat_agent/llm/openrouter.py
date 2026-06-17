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

from gchat_agent.llm import _retry
from gchat_agent.llm.base import LLMClient, extract_json

if TYPE_CHECKING:  # type-only; no runtime import
    from gchat_agent.config import Config


# Default headers OpenRouter recommends for attribution / ranking.
_HTTP_REFERER = "https://github.com/gchat-agent"
_X_TITLE = "gchat-agent"

# Extra application-level backoff on transient failures (the SDK retries too).
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.5  # seconds; base of the exponential backoff (with jitter)
_BACKOFF_CAP = 30.0  # ceiling so a server Retry-After can't park us forever

# Per-request timeout. Without this the openai SDK waits up to its 600s (10 min)
# default, so one hung/slow call can freeze the whole poll cycle. A reasoning
# model legitimately takes tens of seconds, so keep this generous but bounded.
_REQUEST_TIMEOUT = 90.0  # seconds


class OpenRouterClient:
    """An `LLMClient` backed by OpenRouter via the `openai` SDK (§5.3)."""

    def __init__(self, config: "Config") -> None:
        self._config = config
        self._model = config.OPENROUTER_MODEL
        self._base_url = config.OPENROUTER_BASE_URL
        self._api_key = config.OPENROUTER_API_KEY
        self._reasoning = config.OPENROUTER_REASONING
        self._quantizations = [
            q.strip()
            for q in (config.OPENROUTER_QUANTIZATIONS or "").split(",")
            if q.strip()
        ]
        self._use_langfuse = config.OBSERVABILITY == "langfuse"
        self._client: Any | None = None  # lazily constructed
        # Cumulative token usage across this client's lifetime (best-effort: only
        # populated when the API returns `response.usage`). Read via
        # `usage_snapshot()` — the runner diffs it per cycle to log spend. Counts
        # tokens the model actually billed; `calls` counts completions that
        # reported usage.
        self._usage: dict[str, int] = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

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
        self._client = OpenAI(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=_REQUEST_TIMEOUT,
            max_retries=2,
        )
        return self._client

    @property
    def _default_headers(self) -> dict[str, str]:
        return {"HTTP-Referer": _HTTP_REFERER, "X-Title": _X_TITLE}

    # --- transient-failure detection -----------------------------------------
    _is_transient = staticmethod(_retry.is_transient)  # back-compat alias

    # --- token-usage accounting ----------------------------------------------
    def _record_usage(self, response: Any) -> None:
        """Accumulate `response.usage` token counts (best-effort, never raises).

        The completions API returns a `usage` object with `prompt_tokens` /
        `completion_tokens` / `total_tokens`; a response that omits it (or a model
        that doesn't report usage) is simply not counted."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self._usage["calls"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                self._usage[key] += int(getattr(usage, key, 0) or 0)
            except (TypeError, ValueError):
                pass

    def usage_snapshot(self) -> dict[str, int]:
        """A copy of cumulative token usage since construction. The runner diffs
        `total_tokens` across a cycle to report per-cycle spend."""
        return dict(self._usage)

    def _create(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        """Call chat.completions.create with extra backoff on 429/5xx.

        Inject OpenRouter's unified `extra_body` knobs when configured:
        ``{"reasoning": {"enabled": True}}`` (when `OPENROUTER_REASONING` is on)
        so reasoning-capable models think before answering, and
        ``{"provider": {"quantizations": [...]}}`` (from `OPENROUTER_QUANTIZATIONS`)
        to pin routing to those quants. A caller-supplied `extra_body` is merged,
        not clobbered, and explicit `reasoning`/`provider` keys always win.
        """
        client = self._get_client()
        if self._reasoning or self._quantizations:
            extra_body = dict(kwargs.pop("extra_body", None) or {})
            if self._reasoning:
                extra_body.setdefault("reasoning", {"enabled": True})
            if self._quantizations:
                provider = dict(extra_body.get("provider") or {})
                provider.setdefault("quantizations", list(self._quantizations))
                extra_body["provider"] = provider
            kwargs["extra_body"] = extra_body
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    extra_headers=self._default_headers,
                    **kwargs,
                )
                self._record_usage(response)
                return response
            except Exception as exc:  # noqa: BLE001 - re-raised below if fatal
                last_exc = exc
                if attempt < _MAX_RETRIES - 1 and _retry.is_transient(exc):
                    time.sleep(_retry.backoff_delay(
                        attempt, base=_BASE_BACKOFF, cap=_BACKOFF_CAP,
                        retry_after=_retry.retry_after_seconds(exc),
                    ))
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
        # Reasoning models occasionally return empty `content` (the turn's budget
        # went to reasoning tokens); retry once before giving up. Request a JSON
        # object when supported, falling back if the model rejects response_format.
        text = ""
        for _attempt in range(2):
            try:
                response = self._create(
                    messages,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
            except Exception:  # noqa: BLE001 - model may not honor response_format
                response = self._create(messages, temperature=0.0)
            text = self._content(response).strip()
            if text:
                break
        # Never raise: a single empty/garbage reply must degrade THIS call (callers
        # handle {} — no issues / default clarity / no questions), not crash the
        # whole poller. Parse failures fall back to an empty object.
        try:
            return extract_json(text)
        except ValueError:
            return {}


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
