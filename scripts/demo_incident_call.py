#!/usr/bin/env python3
"""Place a Gemini Live API "phone call" that REPORTS the API-gateway-timeout incident.

The AI plays **Alex**, the platform on-call engineer from the ``apigw`` scenario in
``data/scenarios.json``, "calling" **Duc** (trantrongducqt@gmail.com) to report the
production API-gateway 504 incident (INFRA-2207). Alex OPENS the call with a tight
spoken briefing, then answers Duc's questions live, in character, from the held facts.

This is a real-time, bidirectional **VOICE** session over the Gemini Live API
(the ``google-genai`` SDK, WebSockets under the hood — see
``docs/gemini_live/``). It is NOT a Google Chat voice message: the Chat API can't
carry a live call (a hard ceiling documented in ``MEMORY.md``), so the call itself
runs locally on your mic/speaker; the Google Chat demo world only supplies the
*scenario* and the *callee*. Pass ``--announce`` to post a one-line "📞 incident
call starting" heads-up into ``GOOGLE_SPACE`` first.

Modes
-----
* **voice** (default) — full-duplex mic + speaker call with native barge-in. Needs a
  microphone, ``google-genai`` and ``pyaudio`` (+ the system PortAudio library).
* **--text** — type your questions, hear Alex's spoken answers (and read the live
  transcript). Needs only ``google-genai``; if ``pyaudio`` is missing it degrades
  to a transcript-only call (no audio hardware required at all).

Auth
----
``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``, which the google-genai SDK also honors)
from the environment or ``.env`` — a Google AI Studio key
(https://aistudio.google.com/apikey). This is a Google service, **distinct** from
the project's ``OPENROUTER_API_KEY`` (OpenRouter does not proxy the Live API).

Setup (one-time, in the ``igaming`` env)::

    conda run -n igaming pip install google-genai pyaudio
    # pyaudio needs PortAudio:  sudo apt-get install portaudio19-dev   (Debian/Ubuntu)

Run::

    python scripts/demo_incident_call.py                  # voice call (default)
    python scripts/demo_incident_call.py --text           # type questions, hear answers
    python scripts/demo_incident_call.py --persona apigw  # scenario (default: apigw)
    python scripts/demo_incident_call.py --announce        # also ping GOOGLE_SPACE

Use headphones for the voice mode — the default mic/speaker have no echo
cancellation, so without them Alex can hear (and interrupt) himself.

Stdlib only at module top level; ``google-genai`` / ``pyaudio`` and the project's
own modules are imported lazily so ``--help`` and the preflight work with nothing
installed.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading

# Allow running straight from a checkout without installing the package.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

# --- audio constants (Live API: 16-bit PCM, mono; in 16kHz, out 24kHz) ----------
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHANNELS = 1
CHUNK_SIZE = 1024

# --- defaults (overridable by flag / env) ---------------------------------------
DEFAULT_MODEL = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
DEFAULT_VOICE = os.environ.get("GEMINI_LIVE_VOICE", "Kore")  # documented-valid voice
DEFAULT_CALLEE_NAME = "Duc"
DEFAULT_CALLEE_EMAIL = "trantrongducqt@gmail.com"

# Best-effort grace for the spoken sign-off to finish before we hang up (text mode).
SIGNOFF_GRACE_SECONDS = 6.0

# ANSI dim/italic for the *caller* (your) transcript, so the two voices read apart.
_DIM = "\033[2;3m"
_RST = "\033[0m"


class _HangUp(Exception):
    """Raised to end the call cleanly (callee hung up / typed /quit / EOF)."""


def _first_leaf(eg: BaseException) -> BaseException:
    """Unwrap nested ExceptionGroups to the first underlying leaf exception."""
    while isinstance(eg, BaseExceptionGroup) and eg.exceptions:
        eg = eg.exceptions[0]
    return eg


def _friendly_error(exc: BaseException) -> str:
    """A one-line, actionable description of a Live-API/transport failure instead
    of a raw traceback (auth, bad model id, bad voice, transport drop)."""
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if "not_found" in low or "not found" in low:
        return f"model not found — check --model / GEMINI_LIVE_MODEL ({msg})"
    if "invalid_argument" in low or ("invalid" in low and "voice" in low):
        return f"invalid argument — check --voice ({msg})"
    if any(k in low for k in ("api key", "permission", "unauthenticated", "401", "403")):
        return f"auth failed — check GEMINI_API_KEY ({msg})"
    return f"{exc.__class__.__name__}: {msg}"


def _write_wav(path: str, pcm: bytes, rate: int = RECEIVE_SAMPLE_RATE) -> None:
    """Wrap raw 16-bit mono PCM in a WAV container (Live API output is 24kHz)."""
    import wave

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(rate)
        w.writeframes(pcm)


# --------------------------------------------------------------------------------
# API key + persona / prompt construction
# --------------------------------------------------------------------------------
def _resolve_api_key() -> str | None:
    """GEMINI_API_KEY (or the SDK's GOOGLE_API_KEY alias) from the process env,
    else from the repo ``.env`` — matching ``config.load_config``'s precedence
    (process env overrides .env; ``_parse_env_file`` supplies the .env values)."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return key.strip() or None
    try:
        from gchat_agent.config import _parse_env_file  # lazy: project module
    except Exception:  # noqa: BLE001 - package not importable ⇒ no .env fallback
        return None
    env = _parse_env_file(os.path.join(_REPO_ROOT, ".env"))
    val = (env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or "").strip()
    return val or None


def _load_persona(persona_id: str) -> dict:
    """Load one persona ({role, facts, withholding_policy, seed_messages}) from
    ``data/scenarios.json`` (the single source of truth shared with run_staff.py)."""
    from gchat_agent.agent.staff import load_personas  # lazy: project module

    personas = load_personas(os.path.join(_REPO_ROOT, "data", "scenarios.json"))
    if persona_id not in personas:
        have = ", ".join(sorted(personas)) or "(none)"
        raise SystemExit(f"persona {persona_id!r} not in scenarios.json (have: {have})")
    return personas[persona_id]


def _reporter_name(role: str) -> str:
    """Pull the persona's first name out of the 'You are <Name>, ...' role string."""
    head = role.strip()
    if head.lower().startswith("you are "):
        head = head[len("you are "):]
    name = head.split(",", 1)[0].strip()
    return name or "the on-call engineer"


def build_system_instruction(persona: dict, callee_name: str) -> str:
    """Turn a scenario persona into a *phone-call reporter* system prompt.

    Keeps the persona's identity + held facts, but REPLACES the chat-style
    "reveal one fact per reply / withhold" behaviour with how a real on-call
    engineer talks on a live incident call: open with a tight briefing, then
    answer questions directly and concisely. Facts are the hard ceiling — Alex
    never invents specifics he wasn't given.
    """
    name = _reporter_name(persona.get("role", ""))
    identity = persona.get("role", "").split(".", 1)[0].strip() or f"You are {name}"
    facts = persona.get("facts", {}) or {}
    seeds = persona.get("seed_messages", []) or []

    fact_lines = "\n".join(f"- {key}: {val}" for key, val in facts.items())
    seed_text = " ".join(s.strip() for s in seeds).strip()

    return f"""{identity}.
You are on a LIVE PHONE CALL with {callee_name}, the engineering lead, to report a
production incident you are actively handling. This is a real-time voice call, not a chat.

How to behave on the call:
- Open immediately. Greet {callee_name} by name, say who you are ("{name}, platform
  on-call"), and give a tight 2-3 sentence briefing: what is broken, the player
  impact, and that you're on it. Sound like a competent, slightly stressed on-call
  engineer, not a script.
- Then answer {callee_name}'s questions directly and concisely. This is a live call
  — be forthcoming and clear; do NOT withhold or dole out one fact at a time. Keep
  each answer to a sentence or two, the way you'd actually speak on a call.
- Stay strictly in character as {name}. Use ONLY the incident facts below. If asked
  something they don't cover, say you'll check and follow up — never invent numbers,
  names, or timelines.
- Speak naturally for VOICE: no markdown, no reading out bullet lists, say "504s" as
  "five-oh-fours", "p99" as "p ninety-nine", "EOD" as "end of day".
- When {callee_name} signals the call is over (e.g. "thanks", "that's all", "good,
  keep me posted"), give a brief sign-off and stop talking.

What you're calling about (your held facts — this is everything you know):
- Situation: {seed_text}
{fact_lines}
"""


def build_opening_trigger(callee_name: str) -> str:
    """A first 'user' turn that makes the model speak first (open the call)."""
    return (
        f"(The call just connected — you can hear {callee_name} pick up and say "
        f'"Hey, what\'s up?". Go ahead and start: greet {callee_name} and give your '
        "opening incident briefing now.)"
    )


# --------------------------------------------------------------------------------
# The live call
# --------------------------------------------------------------------------------
class LiveCall:
    """One Gemini Live API voice session, driven as an incident-report 'phone call'."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str | None,
        system_instruction: str,
        opening: str,
        mode: str,           # "voice" | "text"
        want_playback: bool,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._system_instruction = system_instruction
        self._opening = opening
        self._mode = mode
        self._want_playback = want_playback

        self._out_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._mic_q: asyncio.Queue[dict] = asyncio.Queue(maxsize=5)
        self._pya = None         # pyaudio.PyAudio (lazy)
        self._mic_stream = None  # input stream (owned/closed by _capture_mic)
        self._last_was_caller = False  # transcript interleaving state
        self._stdin_thread = None  # daemon stdin reader (text mode)
        self._reply_done = asyncio.Event()  # set on generation/turn complete
        self.exit_code = 0       # 0 ok · 1 call dropped · 2 setup error

    def _config(self) -> dict:
        cfg: dict = {
            "response_modalities": ["AUDIO"],
            "system_instruction": self._system_instruction,
            "output_audio_transcription": {},
            "input_audio_transcription": {},
        }
        if self._voice:
            cfg["speech_config"] = {
                "voice_config": {"prebuilt_voice_config": {"voice_name": self._voice}}
            }
        return cfg

    async def run(self) -> None:
        from google import genai  # lazy: optional dep

        try:
            client = genai.Client(api_key=self._api_key)
        except Exception as exc:  # noqa: BLE001 - bad SDK setup → friendly exit
            print(f"ERROR: could not init the Gemini client: {exc}", file=sys.stderr)
            self.exit_code = 2
            return

        if self._want_playback or self._mode == "voice":
            import pyaudio  # lazy: optional dep

            self._pya = pyaudio.PyAudio()

        # Fail fast (before connecting) when voice mode has no microphone.
        if self._mode == "voice":
            try:
                self._pya.get_default_input_device_info()
            except (OSError, IOError):
                print(
                    "ERROR: no microphone / default input device found.\n"
                    "  Re-run with --text to type your questions and hear the answers.",
                    file=sys.stderr,
                )
                self.exit_code = 2
                self._pya.terminate()
                return

        try:
            async with client.aio.live.connect(
                model=self._model, config=self._config()
            ) as session:
                print("📞 Connected. Alex is calling…\n", flush=True)
                # Make the model speak first — open the call with its briefing.
                # On gemini-3.1-flash-live-preview, live text MUST go through
                # send_realtime_input (send_client_content only seeds history).
                await session.send_realtime_input(text=self._opening)
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._receive(session))
                    if self._want_playback:
                        tg.create_task(self._playback())
                    if self._mode == "voice":
                        tg.create_task(self._capture_mic())
                        tg.create_task(self._pump_mic(session))
                    else:
                        tg.create_task(self._read_stdin(session))
        except* _HangUp:
            print("\n📴 Call ended.", flush=True)
        except* Exception as eg:  # transport / audio / server failure mid-call
            self.exit_code = 1
            print(
                f"\n📴 Call dropped: {_friendly_error(_first_leaf(eg))}",
                file=sys.stderr,
            )
        finally:
            # The stdin reader is a daemon thread (text mode) — a blocked readline
            # is abandoned at interpreter exit, never joined, so teardown is instant.
            if self._pya is not None:
                self._pya.terminate()

    # --- non-interactive: capture Alex's opening incident report -----------------
    async def run_brief(self, save_path: str | None, max_seconds: float = 60.0) -> str:
        """Place the call, let Alex deliver his opening incident briefing, capture
        the audio + transcript, then hang up — no mic/interaction needed. Returns
        the spoken transcript; writes a WAV to ``save_path`` when given."""
        from google import genai  # lazy: optional dep

        try:
            client = genai.Client(api_key=self._api_key)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: could not init the Gemini client: {exc}", file=sys.stderr)
            self.exit_code = 2
            return ""

        audio = bytearray()
        text_parts: list[str] = []

        async def _collect(session) -> None:
            async for response in session.receive():
                sc = getattr(response, "server_content", None)
                if not sc:
                    continue
                mt = getattr(sc, "model_turn", None)
                if mt and getattr(mt, "parts", None):
                    for part in mt.parts:
                        data = getattr(getattr(part, "inline_data", None), "data", None)
                        if isinstance(data, bytes):
                            audio.extend(data)
                ot = getattr(sc, "output_transcription", None)
                if ot and getattr(ot, "text", None):
                    text_parts.append(ot.text)
                if getattr(sc, "generation_complete", None) or getattr(
                    sc, "turn_complete", None
                ):
                    return

        try:
            async with client.aio.live.connect(
                model=self._model, config=self._config()
            ) as session:
                print("📞 Placing the call… (Alex will report the incident)\n", flush=True)
                await session.send_realtime_input(text=self._opening)
                try:
                    await asyncio.wait_for(_collect(session), timeout=max_seconds)
                except asyncio.TimeoutError:
                    pass
        except Exception as exc:  # noqa: BLE001 - friendly, no raw traceback
            self.exit_code = 1
            print(f"📴 Call failed: {_friendly_error(_first_leaf(exc))}", file=sys.stderr)
            return ""

        transcript = "".join(text_parts).strip()
        if transcript:
            print(f"🗣️  Alex: {transcript}\n", flush=True)
        if save_path and audio:
            _write_wav(save_path, bytes(audio))
            secs = len(audio) / (RECEIVE_SAMPLE_RATE * 2)
            print(f"💾 Saved {secs:.1f}s of audio → {save_path}", flush=True)
        elif save_path:
            print("note: no audio was returned (transcript only).", flush=True)
        return transcript

    # --- receiving (Alex's audio + both transcripts) -----------------------------
    async def _receive(self, session) -> None:
        while True:
            turn = session.receive()
            async for response in turn:
                sc = getattr(response, "server_content", None)
                if not sc:
                    continue
                if getattr(sc, "interrupted", None):
                    self._drain_output()  # barge-in: drop stale audio at once
                model_turn = getattr(sc, "model_turn", None)
                if model_turn and getattr(model_turn, "parts", None):
                    for part in model_turn.parts:
                        data = getattr(getattr(part, "inline_data", None), "data", None)
                        if isinstance(data, bytes) and self._want_playback:
                            self._out_q.put_nowait(data)
                self._print_transcript(sc)
                if getattr(sc, "generation_complete", None) or getattr(
                    sc, "turn_complete", None
                ):
                    self._reply_done.set()
            # End of a turn (natural or interrupted): drop any stale playback.
            self._drain_output()

    def _print_transcript(self, sc) -> None:
        out = getattr(sc, "output_transcription", None)
        if out and getattr(out, "text", None):
            if self._last_was_caller:
                print(flush=True)
                self._last_was_caller = False
            print(out.text, end="", flush=True)
            if out.text.rstrip()[-1:] in ".!?":
                print(flush=True)
        inp = getattr(sc, "input_transcription", None)
        if inp and getattr(inp, "text", None):
            if not self._last_was_caller:
                print(flush=True)
                self._last_was_caller = True
            print(f"{_DIM}{inp.text}{_RST}", end="", flush=True)
            if inp.text.rstrip()[-1:] in ".!?":
                print(flush=True)

    def _drain_output(self) -> None:
        while not self._out_q.empty():
            try:
                self._out_q.get_nowait()
            except asyncio.QueueEmpty:
                break

    # --- speaker playback --------------------------------------------------------
    async def _playback(self) -> None:
        stream = await asyncio.to_thread(
            self._pya.open,
            format=self._pya.get_format_from_width(2),  # paInt16
            channels=CHANNELS,
            rate=RECEIVE_SAMPLE_RATE,
            output=True,
        )
        try:
            while True:
                chunk = await self._out_q.get()
                await asyncio.to_thread(stream.write, chunk)
        finally:
            try:
                stream.stop_stream()
            except Exception:  # noqa: BLE001 - best-effort on a closing stream
                pass
            stream.close()

    # --- microphone capture (voice mode) -----------------------------------------
    async def _capture_mic(self) -> None:
        mic_info = self._pya.get_default_input_device_info()
        self._mic_stream = await asyncio.to_thread(
            self._pya.open,
            format=self._pya.get_format_from_width(2),  # paInt16
            channels=CHANNELS,
            rate=SEND_SAMPLE_RATE,
            input=True,
            input_device_index=mic_info["index"],
            frames_per_buffer=CHUNK_SIZE,
        )
        try:
            while True:
                data = await asyncio.to_thread(
                    self._mic_stream.read, CHUNK_SIZE, exception_on_overflow=False
                )
                await self._mic_q.put(
                    {"data": data, "mime_type": "audio/pcm;rate=16000"}
                )
        finally:
            # Own the stream's teardown here (loop side) rather than racing the
            # outer finally while a blocking read may still be in flight.
            try:
                self._mic_stream.stop_stream()
            except Exception:  # noqa: BLE001
                pass
            self._mic_stream.close()
            self._mic_stream = None

    async def _pump_mic(self, session) -> None:
        while True:
            msg = await self._mic_q.get()
            await session.send_realtime_input(audio=msg)

    # --- typed questions (text mode) ---------------------------------------------
    async def _read_stdin(self, session) -> None:
        loop = asyncio.get_running_loop()
        # Read stdin on a DAEMON thread feeding an asyncio.Queue. A daemon thread
        # blocked in readline is abandoned (not joined) at interpreter exit, so a
        # mid-call drop or Ctrl+C tears down instantly — whereas a ThreadPoolExecutor
        # worker would be joined at exit, hanging on the un-cancellable readline.
        q: asyncio.Queue = asyncio.Queue()

        def _reader() -> None:
            while True:
                line = sys.stdin.readline()
                try:
                    loop.call_soon_threadsafe(q.put_nowait, line)
                except RuntimeError:
                    break  # loop already closed (call ended) — deliver nowhere
                if line == "":  # EOF
                    break

        self._stdin_thread = threading.Thread(target=_reader, name="stdin", daemon=True)
        self._stdin_thread.start()

        # Let Alex's opening briefing land before the first prompt.
        await asyncio.sleep(0.5)
        while True:
            print(f"\n{_DIM}you>{_RST} ", end="", flush=True)
            line = await q.get()
            if not line:  # EOF (Ctrl+D)
                raise _HangUp
            text = line.strip()
            if text.lower() in ("/quit", "/q", "/bye", "bye", "quit", "exit"):
                await self._sign_off(session)
                raise _HangUp
            if not text:
                continue
            await session.send_realtime_input(text=text)

    async def _sign_off(self, session) -> None:
        """Send a closing line, then let Alex's spoken sign-off finish — synced on
        the turn-complete event (bounded by SIGNOFF_GRACE_SECONDS), not a blind
        sleep — so the goodbye isn't clipped or padded with dead air."""
        self._reply_done.clear()
        await session.send_realtime_input(
            text="Thanks Alex, that's all I needed — keep me posted."
        )
        try:
            await asyncio.wait_for(self._reply_done.wait(), timeout=SIGNOFF_GRACE_SECONDS)
        except asyncio.TimeoutError:
            pass
        # Best-effort: let any audio still queued for playback drain.
        for _ in range(int(SIGNOFF_GRACE_SECONDS * 10)):
            if self._out_q.empty():
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)


# --------------------------------------------------------------------------------
# Optional Google Chat heads-up
# --------------------------------------------------------------------------------
def _announce_on_chat(persona: dict, callee_email: str) -> None:
    """Best-effort: post a one-line '📞 incident call starting' note into
    GOOGLE_SPACE so the call is visible in the Chat demo world. Never fatal."""
    try:
        from gchat_agent.chat.google_rest import GoogleChatClient
        from gchat_agent.config import load_config

        cfg = load_config(os.path.join(_REPO_ROOT, ".env"))
        if not cfg.GOOGLE_SPACE:
            print("--announce: GOOGLE_SPACE is unset; skipping the Chat heads-up.")
            return
        name = _reporter_name(persona.get("role", ""))
        ticket = (persona.get("facts", {}) or {}).get("ticket", "")
        text = (
            f"📞 {name} is starting a voice call to {callee_email} to report a "
            f"production incident. {ticket}".strip()
        )
        chat = GoogleChatClient(cfg, token_file=cfg.GOOGLE_TOKEN_FILE)
        chat.post_message(text)
        print(f"--announce: posted Chat heads-up to {cfg.GOOGLE_SPACE}")
    except Exception as exc:  # noqa: BLE001 - the call must proceed regardless
        print(f"--announce: skipped (Chat post failed: {exc})")


# --------------------------------------------------------------------------------
# Preflight + CLI
# --------------------------------------------------------------------------------
def _preflight(mode: str, api_key: str | None) -> tuple[bool, bool]:
    """Validate the environment. Returns ``(ok, want_playback)``. On a fatal
    problem prints guidance and returns ``ok=False``."""
    if not api_key:
        print(
            "ERROR: no GEMINI_API_KEY / GOOGLE_API_KEY found (checked the "
            "environment and .env).\n"
            "  Get a key at https://aistudio.google.com/apikey, then either add\n"
            "  GEMINI_API_KEY=... to .env or `export GEMINI_API_KEY=...`.",
            file=sys.stderr,
        )
        return False, False

    import importlib.util as iu

    if iu.find_spec("google.genai") is None:
        print(
            "ERROR: the `google-genai` SDK is not installed.\n"
            "  conda run -n igaming pip install google-genai"
            + ("" if mode in ("text", "brief") else " pyaudio"),
            file=sys.stderr,
        )
        return False, False

    if mode == "brief":
        return True, False  # non-interactive capture: needs neither mic nor speaker

    have_pyaudio = iu.find_spec("pyaudio") is not None
    if mode == "voice" and not have_pyaudio:
        print(
            "ERROR: voice mode needs `pyaudio` (+ system PortAudio).\n"
            "  conda run -n igaming pip install pyaudio\n"
            "  # Debian/Ubuntu also: sudo apt-get install portaudio19-dev\n"
            "  …or run with --text to type questions and just hear the answers.",
            file=sys.stderr,
        )
        return False, False

    # text mode: playback is a nice-to-have; transcript-only is fine without it.
    want_playback = have_pyaudio
    if mode == "text" and not have_pyaudio:
        print("note: pyaudio not installed → transcript-only call (no audio).")
    return True, want_playback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="demo_incident_call",
        description="Gemini Live API 'phone call': an on-call engineer reports the "
        "API-gateway-timeout incident and answers your questions, live, by voice.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="type your questions instead of speaking (you still hear the answers; "
        "transcript-only if pyaudio is absent). No microphone required.",
    )
    parser.add_argument(
        "--brief",
        action="store_true",
        help="non-interactive: place the call, capture Alex's opening incident "
        "report (audio + transcript) to a WAV, then hang up. No mic/speaker needed.",
    )
    parser.add_argument(
        "--save",
        default="",
        help="WAV path for --brief (default: reports/demo/incident_call_<persona>.wav).",
    )
    parser.add_argument(
        "--persona",
        default="apigw",
        choices=("apigw", "ops", "promo", "dupe"),
        help="which scenario from data/scenarios.json the caller reports "
        "(default: apigw = the API-gateway-timeout incident).",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Live API model id.")
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help="prebuilt voice name (e.g. Kore, Puck, Charon). Empty to use the "
        "model default.",
    )
    parser.add_argument(
        "--callee", default=DEFAULT_CALLEE_NAME, help="callee display name."
    )
    parser.add_argument(
        "--callee-email", default=DEFAULT_CALLEE_EMAIL, help="callee email (display)."
    )
    parser.add_argument(
        "--announce",
        action="store_true",
        help="also post a one-line '📞 call starting' heads-up into GOOGLE_SPACE "
        "(best-effort; needs the project's Google OAuth config).",
    )
    args = parser.parse_args(argv)

    mode = "brief" if args.brief else ("text" if args.text else "voice")
    api_key = _resolve_api_key()
    ok, want_playback = _preflight(mode, api_key)
    if not ok:
        return 2

    persona = _load_persona(args.persona)
    system_instruction = build_system_instruction(persona, args.callee)
    opening = build_opening_trigger(args.callee)

    if args.announce:
        _announce_on_chat(persona, args.callee_email)

    name = _reporter_name(persona.get("role", ""))
    print(
        f"=== Incident call: {name} → {args.callee} ({args.callee_email}) ===\n"
        f"  scenario: {args.persona}    model: {args.model}    "
        f"voice: {args.voice or '(model default)'}    mode: {mode}"
    )
    if mode == "voice":
        print("  Speak when you hear Alex pause. Use headphones. Ctrl+C to hang up.\n")
    elif mode == "text":
        print("  Type a question and press Enter. /quit to hang up.\n")
    else:
        print()

    call = LiveCall(
        api_key=api_key,  # type: ignore[arg-type]  # _preflight guaranteed non-None
        model=args.model,
        voice=(args.voice or None),
        system_instruction=system_instruction,
        opening=opening,
        mode=mode,
        want_playback=want_playback,
    )

    if mode == "brief":
        save_path = args.save or os.path.join(
            _REPO_ROOT, "reports", "demo", f"incident_call_{args.persona}.wav"
        )
        try:
            asyncio.run(call.run_brief(save_path))
        except KeyboardInterrupt:
            print("\n📴 Aborted.", flush=True)
            return 0
        return call.exit_code

    try:
        asyncio.run(call.run())
    except KeyboardInterrupt:
        print("\n📴 Hung up.", flush=True)
        return 0
    return call.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
