"""Shared transient-failure retry helpers for the OpenRouter LLM + TTS transports.

`OpenRouterClient` and `OpenRouterTTS` both retry 429/5xx with exponential
backoff. This module centralizes the three pieces they previously duplicated (and
which had drifted apart from `chat/google_rest.py`):

- `is_transient(exc)` — the 429/5xx-style classifier (status code, exception
  class name, or error text);
- `retry_after_seconds(exc)` — parse a `Retry-After` header, tolerating BOTH
  exception shapes seen in the wild: the OpenAI SDK's `exc.response.headers` and a
  bare `exc.headers`. Only the integer-seconds form is honored; the HTTP-date form
  falls back to plain backoff (returns ``None``);
- `backoff_delay(attempt, ...)` — the delay to sleep, honoring a server
  `Retry-After` when present, otherwise exponential backoff with **full jitter**
  (`random` in ``[0, min(cap, base*2**attempt)]``) so concurrent retries don't
  thunder in lock-step.

Stdlib only (`random` for jitter); no third-party imports — the mock/CI path
never touches network code, but importing this is still free of `openai`.
"""
from __future__ import annotations

import random
from typing import Any


def is_transient(exc: Exception) -> bool:
    """True for 429 / 5xx-style errors worth an extra backoff retry.

    Checks, in order: an int `status_code`/`status` attribute in the 429 or 5xx
    range; a class name hinting at a rate-limit / connection / internal-server
    error; or the stringified error mentioning 429 / rate limit /
    RESOURCE_EXHAUSTED. Deliberately broad — a false positive only costs one extra
    bounded retry, while a false negative drops a recoverable call."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    name = exc.__class__.__name__.lower()
    if "ratelimit" in name or "apiconnection" in name or "internalserver" in name:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "resource_exhausted" in text


def _headers_from(exc: Exception) -> Any:
    """The response headers carried on an exception, across SDK shapes.

    The OpenAI SDK puts them on `exc.response.headers`; a lower-level client may
    expose `exc.headers` directly. Returns whatever header mapping is found (an
    object with a `.get`), or ``None``."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) if resp is not None else None
    if headers is None:
        headers = getattr(exc, "headers", None)
    return headers


def retry_after_seconds(exc: Exception) -> float | None:
    """Parse a `Retry-After` hint (in seconds) off a transient error, or ``None``.

    Honors only the integer/float delta-seconds form (the one OpenRouter/Google
    send for rate limits). The HTTP-date form is intentionally unsupported — it is
    rare for these APIs and parsing it adds risk; the caller falls back to plain
    exponential backoff (``None``). A negative or non-numeric value also yields
    ``None``."""
    headers = _headers_from(exc)
    get = getattr(headers, "get", None)
    if not callable(get):
        return None
    raw = get("retry-after")
    if raw is None:
        raw = get("Retry-After")
    if raw is None:
        return None
    try:
        secs = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return secs if secs >= 0 else None


def backoff_delay(
    attempt: int,
    *,
    base: float,
    cap: float,
    retry_after: float | None = None,
) -> float:
    """Seconds to sleep before retry `attempt` (0-based).

    A server-provided `retry_after` wins (clamped to `cap` so a hostile/huge value
    can't park the poller forever). Otherwise: exponential backoff with FULL
    jitter — a uniform random pick in ``[0, min(cap, base * 2**attempt)]`` — which
    keeps the worst case bounded by `cap` while de-synchronizing retries that would
    otherwise fire in lock-step."""
    if retry_after is not None:
        return min(retry_after, cap)
    ceiling = min(cap, base * (2 ** attempt))
    return random.uniform(0, ceiling)
