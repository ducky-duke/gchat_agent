"""Text-to-speech client + the `build_tts` provider factory (voice reports).

A resolved issue's report can be delivered as a spoken audio attachment instead
of (or alongside) on-disk Markdown. `TTSClient` is the seam: `synthesize(text)`
returns the audio bytes **in memory** (no disk), so the runner can hand them
straight to the Chat media-upload path.

`OpenRouterTTS` wraps the official **`openai` SDK** pointed at OpenRouter's
OpenAI-compatible `audio.speech` endpoint (the same transport `OpenRouterClient`
uses — base URL, key, attribution headers, langfuse drop-in). The `openai` import
is **lazy** so nothing third-party is required on the mock/CI path.

`MockTTS` returns deterministic placeholder bytes so the whole voice-delivery flow
(narrate → synthesize → upload → post) is exercised offline with no network/key.

Stdlib only at module top level; `openai`/`langfuse` are imported lazily.
"""
from __future__ import annotations

import io
import time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from gchat_agent.llm import _retry

if TYPE_CHECKING:  # type-only; no runtime import
    from gchat_agent.config import Config

# OpenRouter attribution headers (mirrors llm.openrouter).
_HTTP_REFERER = "https://github.com/gchat-agent"
_X_TITLE = "gchat-agent"

# Extra application-level backoff on transient failures (the SDK retries too).
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.5  # seconds; base of the exponential backoff (with jitter)
_BACKOFF_CAP = 30.0  # ceiling so a server Retry-After can't park us forever

# Per-request timeout — a hung TTS call must not freeze a poll cycle. Synthesis
# of a short narration is quick, but keep it generous for a cold model.
_REQUEST_TIMEOUT = 90.0  # seconds

# A short, well-known marker the mock emits so tests can recognize fake audio.
_MOCK_PREFIX = b"MOCK-TTS\x00"


@runtime_checkable
class TTSClient(Protocol):
    """Synthesize speech audio from text, returned as in-memory bytes."""

    def synthesize(self, text: str) -> bytes:
        """Return audio bytes (MP3) for ``text``. Empty input ⇒ empty bytes."""
        ...


class OpenRouterTTS:
    """A `TTSClient` backed by OpenRouter's `audio.speech` via the `openai` SDK."""

    def __init__(self, config: "Config") -> None:
        self._config = config
        self._model = config.TTS_MODEL
        self._voice = config.TTS_VOICE
        self._format = config.TTS_FORMAT or "mp3"
        self._base_url = config.OPENROUTER_BASE_URL
        self._api_key = config.OPENROUTER_API_KEY
        self._use_langfuse = config.OBSERVABILITY == "langfuse"
        self._client: Any | None = None  # lazily constructed

    # --- client construction (lazy import) -----------------------------------
    def _get_client(self) -> Any:
        """Construct (once) and return the OpenAI-compatible client, sourcing the
        langfuse drop-in when observability is on (so synthesis is auto-traced)."""
        if self._client is not None:
            return self._client
        if self._use_langfuse:
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

    _is_transient = staticmethod(_retry.is_transient)  # back-compat alias

    def synthesize(self, text: str) -> bytes:
        """Synthesize ``text`` to MP3 bytes, streamed fully into memory.

        Uses `audio.speech.with_streaming_response` and accumulates the chunks in
        a `BytesIO` so the audio never touches disk. Extra backoff on transient
        429/5xx, matching the OpenRouter chat transport."""
        text = (text or "").strip()
        if not text:
            return b""
        client = self._get_client()
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                buf = io.BytesIO()
                with client.audio.speech.with_streaming_response.create(
                    model=self._model,
                    voice=self._voice,
                    input=text,
                    response_format=self._format,
                    extra_headers=self._default_headers,
                ) as response:
                    for chunk in response.iter_bytes():
                        buf.write(chunk)
                return buf.getvalue()
            except Exception as exc:  # noqa: BLE001 - re-raised below if fatal
                last_exc = exc
                if attempt < _MAX_RETRIES - 1 and _retry.is_transient(exc):
                    time.sleep(_retry.backoff_delay(
                        attempt, base=_BASE_BACKOFF, cap=_BACKOFF_CAP,
                        retry_after=_retry.retry_after_seconds(exc),
                    ))
                    continue
                raise
        assert last_exc is not None  # unreachable: loop returns or raises
        raise last_exc


class MockTTS:
    """A deterministic `TTSClient` for offline tests — no network, no key.

    Returns a short marker prefix followed by the UTF-8 input so a test can assert
    the narration text reached synthesis and that audio bytes flowed through the
    upload/post path, without decoding any real audio."""

    def synthesize(self, text: str) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        return _MOCK_PREFIX + text.encode("utf-8")


def build_tts(config: "Config") -> "TTSClient | None":
    """Provider factory for the voice-report path.

    Returns ``None`` when delivery never needs TTS (``REPORT_DELIVERY == "disk"``)
    so the disk-only path constructs nothing. Otherwise mirrors `build_llm`:
    `MockTTS` for the mock provider, `OpenRouterTTS` (requires an API key) for
    OpenRouter. An unknown provider raises, like `build_llm`."""
    delivery = (config.REPORT_DELIVERY or "disk").strip().lower()
    if delivery not in ("voice", "both"):
        return None
    provider = (config.LLM_PROVIDER or "").strip().lower()
    if provider == "mock":
        return MockTTS()
    if provider == "gemini":
        # The voice-report TTS here is the legacy OpenRouter/`openai` transport;
        # spoken delivery is now handled by the Gemini Live call on resolve, so the
        # default `gemini` provider has no TTS. Return None ⇒ the runner degrades to
        # the on-disk report (and REPORT_DELIVERY defaults to "disk" anyway).
        return None
    if provider == "openrouter":
        if not config.OPENROUTER_API_KEY:
            raise RuntimeError("set OPENROUTER_API_KEY or LLM_PROVIDER=mock for voice reports")
        return OpenRouterTTS(config)
    raise RuntimeError(
        f"unknown LLM_PROVIDER={config.LLM_PROVIDER!r}; use 'gemini', 'mock', or 'openrouter'"
    )
