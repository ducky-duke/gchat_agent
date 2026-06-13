"""LLM client protocol + robust JSON extraction (§5.3).

Not all OpenRouter models honor `response_format`, so `complete_json` callers
lean on `extract_json` to pull a JSON object out of a possibly-fenced, possibly-
chatty completion.
"""
from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    """A minimal chat-completion interface; OpenRouter and Mock both implement it."""

    def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        """Return the assistant's text reply for a system prompt + message list
        (each message a `{"role": ..., "content": ...}` dict)."""
        ...

    def complete_json(
        self,
        system: str,
        user: str,
        schema_hint: str | None = None,
    ) -> dict[str, Any]:
        """Run a single-turn completion and return a parsed JSON object. The
        implementation should call `extract_json` on the raw text so fenced or
        prose-wrapped JSON still parses; `schema_hint` may be appended to the
        prompt to steer the output shape. For list-shaped outputs (e.g.
        detection, §6) instruct the model to wrap them as `{"issues": [...]}`,
        or use `extract_json_value` which also parses a top-level array."""
        ...


def _strip_code_fences(text: str) -> str:
    """Remove a single surrounding Markdown code fence if present (```json … ```
    or a bare ``` … ```). Leaves un-fenced text untouched."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    # Drop the opening fence line (which may carry a language tag, e.g. ```json).
    newline = stripped.find("\n")
    if newline == -1:
        return stripped  # malformed single-line fence; let the brace scan handle it
    body = stripped[newline + 1:]
    closing = body.rfind("```")
    if closing != -1:
        body = body[:closing]
    return body.strip()


def _first_balanced_json(text: str) -> str | None:
    """Return the first balanced top-level JSON value — an object `{...}` or an
    array `[...]` — respecting strings and escapes so brackets inside string
    literals don't throw off the depth count. Returns `None` if none is found."""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return None
    start = min(starts)
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json_value(text: str) -> dict[str, Any] | list[Any]:
    """Robustly extract a JSON value (object or array) from LLM output (§5.3).

    Strategy: strip Markdown code fences, then try `json.loads` on the whole
    thing; on failure, scan for the first balanced `{...}`/`[...]` and parse it.
    Detection (§6) returns a top-level array; object responses (assess / question
    generation) return a dict. Raises `ValueError` on total failure.
    """
    if text is None:
        raise ValueError("cannot extract JSON from None")
    candidate = _strip_code_fences(text)

    # Fast path: the (de-fenced) text is already valid JSON.
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, (dict, list)):
            return parsed
    except json.JSONDecodeError:
        pass

    # Fallback: locate the first balanced value embedded in surrounding prose.
    blob = _first_balanced_json(candidate)
    if blob is not None:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"found a JSON-like value but could not parse it: {exc}; "
                f"text was: {text!r}"
            ) from exc
        if isinstance(parsed, (dict, list)):
            return parsed

    raise ValueError(f"no JSON value found in LLM output: {text!r}")


def extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON *object* from LLM output — a convenience over
    `extract_json_value` for the object-shaped responses `complete_json` returns.
    Raises `ValueError` if the payload is an array or absent. For array-shaped
    detection output use `extract_json_value`, or instruct the model to wrap it
    as `{"issues": [...]}`."""
    value = extract_json_value(text)
    if not isinstance(value, dict):
        raise ValueError(
            f"expected a JSON object but got a {type(value).__name__}: {text!r}"
        )
    return value
