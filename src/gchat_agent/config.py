"""Env-driven settings (§5.1 + §10).

A tiny stdlib `.env` loader (no `python-dotenv`): parse a `.env` file if present,
overlay `os.environ`, then fall back to the §10 defaults. `load_config()` returns
a frozen `Config` with every key from §10, coerced to the right type.

Sensible defaults mean the mock-LLM / CI test path needs no configuration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Final

# --- precedence: .env file < process env < defaults-as-fallback -------------
# We read .env first, overlay os.environ on top, then each Config field falls
# back to its hard-coded default when the key is absent from both layers.

_DEFAULT_ENV_FILE: Final[str] = ".env"

_TRUE_TOKENS: Final[frozenset[str]] = frozenset({"true", "1", "yes", "on", "y"})


def _clean_value(val: str) -> str:
    """Strip an inline `# comment` and surrounding quotes from a raw .env value.

    Quoting wins: a value wrapped in matching quotes is taken verbatim (any `#`
    inside is literal; trailing content after the closing quote is dropped). For
    an unquoted value a `#` that follows whitespace begins an inline comment, so
    `http://x#frag` or a leading `#fff` survive intact."""
    val = val.strip()
    if not val:
        return val
    if val[0] in ("'", '"'):
        quote = val[0]
        end = val.find(quote, 1)
        return val[1:end] if end != -1 else val[1:]
    for i, ch in enumerate(val):
        if ch == "#" and i > 0 and val[i - 1] in (" ", "\t"):
            return val[:i].rstrip()
    return val


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a `.env` file into a dict. KEY=VALUE per line; blanks and
    `#`-comment lines are ignored; inline `# comments` and surrounding quotes
    on values are stripped (see `_clean_value`). A missing file yields an empty
    dict (the test path needs no .env)."""
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):  # tolerate `export KEY=VALUE`
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if not key:
                continue
            values[key] = _clean_value(val)
    return values


def _to_bool(value: str) -> bool:
    """Parse a bool from `true`/`1`/`yes` (case-insensitive)."""
    return value.strip().lower() in _TRUE_TOKENS


def _to_int(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return default


def _to_float(value: str, default: float) -> float:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return default


@dataclass(frozen=True)
class Config:
    """Immutable, fully-resolved settings (every §10 key)."""

    # --- LLM provider / transport ---
    LLM_PROVIDER: str = "openrouter"  # or: mock
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "deepseek/deepseek-v4-flash"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

    # --- Observability (optional [observability] extra) ---
    OBSERVABILITY: str = "none"  # or: langfuse
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""

    # --- RAG ---
    RAG_DENSE: bool = False
    RAG_TOP_K: int = 5
    KB_DIR: str = "data/knowledge_base"

    # --- Agent loop ---
    MAX_CLARIFY_ROUNDS: int = 3
    STALE_AFTER_IDLE_CYCLES: int = 3
    RESOLVE_CONFIDENCE_THRESHOLD: float = 0.8
    DETECT_WINDOW_MESSAGES: int = 50
    STATE_FILE: str = ".state/issues.json"
    REPORTS_DIR: str = "reports"

    # --- Google Chat (real demo only — user OAuth, §7) ---
    GOOGLE_SPACE: str = ""
    GOOGLE_OAUTH_CLIENT: str = "secrets/oauth_client.json"
    GOOGLE_TOKEN_FILE: str = "secrets/token_bot.json"
    GOOGLE_QUOTA_PROJECT: str = ""
    POLL_INTERVAL_SECONDS: int = 15
    POLL_BACKFILL_SINCE: str = ""

    # --- Webhook (Phase 2 only) ---
    WEBHOOK_PORT: int = 8080
    WEBHOOK_AUTH_AUDIENCE: str = ""


# Which fields need non-string coercion (everything else stays a str).
_BOOL_KEYS: Final[frozenset[str]] = frozenset({"RAG_DENSE"})
_INT_KEYS: Final[frozenset[str]] = frozenset({
    "RAG_TOP_K",
    "MAX_CLARIFY_ROUNDS",
    "STALE_AFTER_IDLE_CYCLES",
    "DETECT_WINDOW_MESSAGES",
    "POLL_INTERVAL_SECONDS",
    "WEBHOOK_PORT",
})
_FLOAT_KEYS: Final[frozenset[str]] = frozenset({"RESOLVE_CONFIDENCE_THRESHOLD"})


def load_config(env_file: str = _DEFAULT_ENV_FILE) -> Config:
    """Resolve a `Config` from (`.env` file < `os.environ`), defaulting any key
    absent from both to the field default. `int`/`float`/`bool` keys are coerced;
    everything else stays a string."""
    layered: dict[str, str] = {}
    layered.update(_parse_env_file(env_file))
    layered.update(os.environ)  # process env overrides .env

    kwargs: dict[str, object] = {}
    for field in fields(Config):
        name = field.name
        if name not in layered:
            continue  # leave the dataclass default in place
        raw = layered[name]
        if name in _BOOL_KEYS:
            kwargs[name] = _to_bool(raw)
        elif name in _INT_KEYS:
            kwargs[name] = _to_int(raw, field.default)  # type: ignore[arg-type]
        elif name in _FLOAT_KEYS:
            kwargs[name] = _to_float(raw, field.default)  # type: ignore[arg-type]
        else:
            kwargs[name] = raw
    return Config(**kwargs)  # type: ignore[arg-type]
