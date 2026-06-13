"""Langfuse observability shim (§5.9) — no-op by default, lazy when enabled.

Active **only** when `load_config().OBSERVABILITY == "langfuse"`; otherwise every
helper here is a pure-stdlib no-op that imports nothing third-party, preserving
the offline, zero-config, no-key mock/CI path.

Public surface (stable across both paths):
- `observe` — a decorator usable as both `@observe` and `@observe(name=...)`.
  Wraps the real `langfuse.observe` when enabled, else an identity decorator.
- `trace(name, **kw)` — a context manager grouping a block under one
  trace/session (keyed e.g. by `issue.id`, §5.7); a no-op when disabled.
- `flush()` — push buffered events to Langfuse on shutdown; a no-op when disabled.

`langfuse` is imported lazily inside the enabled branches only, so nothing is
imported on the `none`/mock path.
"""
from __future__ import annotations

import functools
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional, TypeVar

from .config import load_config

F = TypeVar("F", bound=Callable[..., Any])

# Cache the enabled-flag + the resolved langfuse callables so we read `.env` /
# import the SDK at most once per process. `None` means "not yet resolved".
_ENABLED: Optional[bool] = None


def _enabled() -> bool:
    """True iff `OBSERVABILITY == "langfuse"`. Resolved once, then cached so the
    decorator/context-manager hot paths don't re-parse config on every call."""
    global _ENABLED
    if _ENABLED is None:
        try:
            _ENABLED = load_config().OBSERVABILITY.strip().lower() == "langfuse"
        except Exception:
            # Never let observability wiring break the agent: degrade to off.
            _ENABLED = False
    return _ENABLED


def _real_observe() -> Optional[Callable[..., Any]]:
    """Lazy-import and return `langfuse.observe`, or `None` if unavailable.

    Imported only when observability is enabled; an ImportError (extra not
    installed) degrades silently to the identity decorator."""
    try:
        from langfuse import observe as _observe  # type: ignore[import-not-found]
    except Exception:
        return None
    return _observe


# --- observe decorator ------------------------------------------------------

def observe(fn: Optional[F] = None, *, name: Optional[str] = None) -> Any:
    """Decorator that traces a function as a Langfuse span when enabled.

    Works both bare and parameterized::

        @observe
        def f(...): ...

        @observe(name="detect")
        def g(...): ...

    When disabled (the default) it returns the function unchanged — a true
    identity decorator with zero third-party imports.
    """
    if fn is not None and callable(fn):
        # Used bare: @observe
        return _wrap(fn, name=name)

    # Used with arguments: @observe(name=...) — return the actual decorator.
    def decorator(func: F) -> F:
        return _wrap(func, name=name)

    return decorator


def _wrap(func: F, *, name: Optional[str]) -> F:
    """Apply the real `langfuse.observe` to `func` when enabled and available;
    otherwise return `func` unchanged."""
    if not _enabled():
        return func
    real = _real_observe()
    if real is None:
        return func
    # Pass `name` through only when supplied; langfuse derives one otherwise.
    decorated = real(func, name=name) if name is not None else real(func)
    functools.update_wrapper(decorated, func)
    return decorated  # type: ignore[return-value]


# --- trace context manager --------------------------------------------------

@contextmanager
def trace(name: str, **kw: Any) -> Iterator[Any]:
    """Group a block of work under one Langfuse trace/session (§5.7).

    A no-op when disabled (yields `None`, imports nothing). When enabled it
    opens a `langfuse.start_as_current_span` (falling back to the older
    `start_span`) named `name`; extra keyword args are attached as metadata.
    Always yields the underlying span object (or `None`).
    """
    if not _enabled():
        yield None
        return

    try:
        from langfuse import get_client  # type: ignore[import-not-found]
    except Exception:
        yield None
        return

    try:
        client = get_client()
    except Exception:
        yield None
        return

    starter = getattr(client, "start_as_current_span", None)
    if starter is None:
        starter = getattr(client, "start_span", None)
    if starter is None:
        # Unknown SDK shape — stay a no-op rather than crash the agent.
        yield None
        return

    span_kwargs: dict[str, Any] = {"name": name}
    if kw:
        span_kwargs["metadata"] = kw
    try:
        cm = starter(**span_kwargs)
    except Exception:
        yield None
        return

    # `start_as_current_span` is itself a context manager; `start_span` returns
    # a span object. Handle both shapes.
    if hasattr(cm, "__enter__") and hasattr(cm, "__exit__"):
        with cm as span:
            yield span
    else:
        try:
            yield cm
        finally:
            end = getattr(cm, "end", None)
            if callable(end):
                try:
                    end()
                except Exception:
                    pass


# --- flush ------------------------------------------------------------------

def flush() -> None:
    """Push buffered Langfuse events (call on shutdown). No-op when disabled."""
    if not _enabled():
        return
    try:
        from langfuse import get_client  # type: ignore[import-not-found]
    except Exception:
        return
    try:
        get_client().flush()
    except Exception:
        # Best-effort: a failed flush must not crash shutdown.
        pass


def _reset_cache() -> None:
    """Test hook: clear the cached enabled-flag so a changed `OBSERVABILITY`
    env var is re-read on the next call. Not part of the runner's public flow."""
    global _ENABLED
    _ENABLED = None
