"""Gemini-backed `LLMClient` via the official **`google-genai` SDK** (§5.3).

`GeminiClient` talks to the Gemini API directly (`client.models.generate_content`)
authenticating with **`GEMINI_API_KEY`** — the SAME key the Gemini Live voice call
uses (`call/gemini_voice.py`), so the project has one Google key, not two. This is
the active live transport; the OpenRouter/`openai` path (`openrouter.py`,
`tts.py`) is kept for reference but no longer selected by the default config.

`complete_json` runs a single-turn completion with `response_mime_type=
"application/json"` and still pulls the object out via the foundation's
`extract_json`, so a chatty/fenced reply degrades gracefully (the API's JSON mode
is honored, but we never trust it blindly).

Per the Gemini 3.x guidance (`docs/gemini_live/whats-new-gemini-3.5.md.txt`) the
sampling knobs `temperature`/`top_p`/`top_k` are intentionally NOT set — the
reasoning models are tuned for their defaults — and thinking depth is controlled
by the string `thinking_level` enum (`GEMINI_THINKING_LEVEL`), not a raw budget.

Stdlib only at module top level; `google.genai` is imported lazily inside methods,
so nothing third-party is required on the mock/CI path.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from gchat_agent.llm import _retry
from gchat_agent.llm.base import extract_json

if TYPE_CHECKING:  # type-only; no runtime import
    from gchat_agent.config import Config


# Extra application-level backoff on transient failures (the SDK retries too).
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.5  # seconds; base of the exponential backoff (with jitter)
_BACKOFF_CAP = 30.0  # ceiling so a server Retry-After can't park us forever

# Per-request timeout. Without this the SDK can wait a long time, so one hung/slow
# call would freeze the whole poll cycle. A reasoning model legitimately takes
# tens of seconds, so keep this generous but bounded. (google-genai HttpOptions
# expects milliseconds.)
_REQUEST_TIMEOUT = 90.0  # seconds


class GeminiClient:
    """An `LLMClient` backed by the Gemini API via the `google-genai` SDK (§5.3)."""

    def __init__(self, config: "Config") -> None:
        self._config = config
        self._model = config.GEMINI_MODEL
        self._api_key = config.GEMINI_API_KEY
        # Optional thinking depth ("minimal"|"low"|"medium"|"high"); empty ⇒ leave
        # the model default (medium for gemini-3.5-flash). The SDK coerces the
        # lower-cased string to its ThinkingLevel enum.
        self._thinking_level = (config.GEMINI_THINKING_LEVEL or "").strip().lower()
        self._client: Any | None = None  # lazily constructed
        # Cumulative token usage across this client's lifetime (best-effort: only
        # populated when the API returns `usage_metadata`). Read via
        # `usage_snapshot()` — the runner diffs it per cycle to log spend.
        self._usage: dict[str, int] = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        # One client instance is shared by the analyzer (foreground detect/assess)
        # AND the runner's background report builders, so the usage dict can be
        # mutated from two threads at once. A lock keeps the read-modify-write of
        # each counter (and the snapshot copy) consistent.
        self._usage_lock = threading.Lock()

    # --- client construction (lazy import) -----------------------------------
    def _get_client(self) -> Any:
        """Construct (once) and return the `google.genai` client. Imports the SDK
        lazily so nothing third-party is needed on the mock/CI path."""
        if self._client is not None:
            return self._client
        from google import genai  # lazy, optional dep
        from google.genai import types

        self._client = genai.Client(
            api_key=self._api_key,
            http_options=types.HttpOptions(timeout=int(_REQUEST_TIMEOUT * 1000)),
        )
        return self._client

    def _build_config(self, *, system: str, json_mode: bool) -> Any:
        """The `GenerateContentConfig` for one call. System prompt → the dedicated
        `system_instruction` slot; JSON tasks request the application/json mime
        type; thinking depth is set only when configured. No `temperature` etc. —
        Gemini 3.x is tuned for its defaults (see module docstring)."""
        from google.genai import types

        kwargs: dict[str, Any] = {}
        if system:
            kwargs["system_instruction"] = system
        if json_mode:
            kwargs["response_mime_type"] = "application/json"
        if self._thinking_level:
            # The SDK coerces the lower-cased string to its ThinkingLevel enum at
            # runtime; ty only sees the str, hence the inline ignore.
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=self._thinking_level  # ty: ignore[invalid-argument-type]
            )
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _to_contents(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Map the `{"role","content"}` message list to Gemini `contents` dicts
        (the SDK coerces them). Roles: `assistant` → `model`, everything else →
        `user`. In practice `chat` is only ever called with a single user turn."""
        contents: list[dict[str, Any]] = []
        for msg in messages or []:
            role = "model" if msg.get("role") == "assistant" else "user"
            text = str(msg.get("content", "") or "")
            contents.append({"role": role, "parts": [{"text": text}]})
        return contents

    # --- token-usage accounting ----------------------------------------------
    def _record_usage(self, response: Any) -> None:
        """Accumulate `response.usage_metadata` token counts (best-effort, never
        raises). A response that omits it is simply not counted."""
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        with self._usage_lock:
            self._usage["calls"] += 1
            for key, attr in (
                ("prompt_tokens", "prompt_token_count"),
                ("completion_tokens", "candidates_token_count"),
                ("total_tokens", "total_token_count"),
            ):
                try:
                    self._usage[key] += int(getattr(usage, attr, 0) or 0)
                except (TypeError, ValueError):
                    pass

    def usage_snapshot(self) -> dict[str, int]:
        """A copy of cumulative token usage since construction. The runner diffs
        `total_tokens` across a cycle to report per-cycle spend."""
        with self._usage_lock:
            return dict(self._usage)

    def _generate(
        self, *, contents: Any, system: str, json_mode: bool
    ) -> Any:
        """Call `models.generate_content` with extra backoff on 429/5xx.

        The SDK retries some failures itself; this adds an application-level
        bounded backoff (honoring a server `Retry-After` when present, else
        exponential with full jitter) for the transient classes `_retry` knows."""
        client = self._get_client()
        cfg = self._build_config(system=system, json_mode=json_mode)
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = client.models.generate_content(
                    model=self._model, contents=contents, config=cfg
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
    def _text(response: Any) -> str:
        """Extract the assistant text from a generate_content response. The `.text`
        property can raise (a blocked / candidate-less response), so guard it."""
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - blocked/empty response → no text
            return ""
        return text or ""

    # --- LLMClient protocol --------------------------------------------------
    def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        """Return the assistant text for a system prompt + message list."""
        contents = self._to_contents(messages)
        response = self._generate(contents=contents, system=system, json_mode=False)
        return self._text(response)

    def complete_json(
        self,
        system: str,
        user: str,
        schema_hint: str | None = None,
    ) -> dict[str, Any]:
        """Single-turn completion -> parsed JSON object (via `extract_json`)."""
        sys_text = system if not schema_hint else f"{system}\n\n{schema_hint}"
        contents = self._to_contents([{"role": "user", "content": user}])
        # A reasoning model occasionally returns empty text (the turn's budget went
        # to thinking tokens); retry once before giving up. JSON mode is honored by
        # gemini-3.5-flash, but `extract_json` still defends against a fenced/chatty
        # reply.
        text = ""
        for _attempt in range(2):
            response = self._generate(contents=contents, system=sys_text, json_mode=True)
            text = self._text(response).strip()
            if text:
                break
        # Never raise on a bad payload: an empty/garbage reply degrades THIS call to
        # {} (callers handle that — no issues / default clarity / no questions),
        # not a crash of the whole poller.
        try:
            return extract_json(text)
        except ValueError:
            return {}
