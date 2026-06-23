#!/usr/bin/env python3
"""gemini_voice.py — a BIDIRECTIONAL Gemini Live ⇄ phone-call audio bridge.

This is the "AI voice on the call" engine: it gives Gemini Live both a MOUTH and an
EAR on a live Google Chat / Meet call by routing audio through two virtual PulseAudio
devices, so a HUMAN on the other end and Gemini can actually talk to each other.

Topology (all virtual — no real mic/speaker involved, so no echo into the room):

    Gemini Live  ──audio out (24 kHz)──►  ffmpeg ──►  ai_mic_sink (null sink)
                                                          │ .monitor
                                                          ▼
                                              ai_mic (remap-source, = DEFAULT source)
                                                          │
                                                          ▼  browser getUserMedia
                                              the call's outgoing mic  ──►  CALLEE hears Gemini

    CALLEE speaks ──► browser plays it ──► gemini_call_spk (null sink, = DEFAULT sink)
                                                          │ .monitor
                                                          ▼
                                              ffmpeg ──(16 kHz)──►  Gemini Live  (the EAR)

Why virtual devices and not Chrome fake-audio flags: the call runs in a real logged-in
browser, and we need a LIVE, two-way stream, not a one-shot file. The default-source /
default-sink swap makes the (pre-granted) browser grab ``ai_mic`` as its microphone and
play the remote voice into ``gemini_call_spk`` automatically — the exact trick that made
the one-way "AI mouth" tone work, now mirrored on the output side for the ear. Fully
reversible: ``teardown()`` restores the previous default source + sink and unloads every
module it loaded.

This module owns ONLY the audio + the Gemini session. Placing/holding the actual call
(ring, join detection, mic-unmute, hang-up detection) is delegated to
``meet_call_browser`` by the orchestrator ``call/gemini_call.py`` — keep them split.

Standalone checks (no call needed):
    python call/gemini_voice.py --devices-test     # set up the 2 sinks, probe, tear down
    python call/gemini_voice.py --selftest         # Gemini text→audio round-trip → WAV

⚠️  Live calls automate the Google UI (ToS / account-flag risk) — demo accounts only.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave

# --- audio formats (Gemini Live contract) ----------------------------------------
# Live API: realtime INPUT is 16 kHz mono s16le PCM; model OUTPUT is 24 kHz mono s16le.
EAR_RATE = 16_000          # what we feed Gemini (the callee's voice)
MOUTH_RATE = 24_000        # what Gemini emits (its own voice)
EAR_CHUNK = 4096           # bytes per ear read (~128 ms @ 16 kHz mono s16le)

# --- virtual device names ----------------------------------------------------------
MOUTH_SINK = "ai_mic_sink"        # Gemini's voice is played into this null sink …
MOUTH_SOURCE = "ai_mic"           # … and re-exposed here as the browser's microphone
EAR_SINK = "gemini_call_spk"      # the call's loudspeaker (browser plays the callee here)

# Browser playback streams (sink-inputs) we move onto the capture sink — by app name.
_BROWSER_MATCH = ("brave", "chrom", "chrome", "meet", "google")

# Default model + voice. The model id tracks Google's Live preview (see the bundled
# docs/gemini_live mirror + the official command-line example); override with --model
# if it 404s. Prebuilt voices are language-agnostic — they speak whatever Gemini outputs.
DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_VOICE = "Aoede"

# Default persona: Gemini is the CALLER, speaking English (the callee here is Duc).
DEFAULT_SYSTEM = (
    "You are a friendly AI assistant who is CALLING someone over Google Chat — "
    "you are the one placing the call. When the call connects, greet them briefly and "
    "naturally, introduce yourself as an AI assistant, then ask how you can help. Talk "
    "naturally, in short sentences, with a warm and polite tone. ALWAYS speak English, "
    "unless the other person switches to another language first. Do not read this text "
    "aloud; just have a conversation."
)
# Sent as the first user turn when --greet is on, to make Gemini speak first.
GREET_TRIGGER = "(The call just connected. Greet the person you're calling right now.)"
# Max seconds to keep the ear CLOSED waiting for the opening (greeting OR incident
# briefing) to finish before opening it anyway (so a missed turn_complete can't deafen
# Gemini forever). Generous enough to cover a multi-sentence incident briefing.
GREET_MAX_WAIT = 20.0

# --- voice-activity detection (the callee's turn) ----------------------------------
# Server-side automatic VAD decides when the callee started/stopped talking, i.e. when
# the model should answer. A live-call sink monitor is NEVER digitally silent (comfort
# noise / room tone / line hiss), so the default VAD can both (a) miss the callee's
# speech as a turn-start and (b) never see an end-of-speech — leaving the model waiting
# forever and never replying (observed: ear open, real audio in, yet zero response).
# Bias it to hear speech eagerly (start HIGH) and to treat a short pause as end-of-turn
# (end HIGH + a natural ~0.8s of silence) so the model actually takes its turn. Tune
# these if it over-triggers (cuts the callee off) or under-triggers (still won't reply).
VAD_START_SENSITIVITY = "START_SENSITIVITY_HIGH"   # how readily speech-start is detected
VAD_END_SENSITIVITY = "END_SENSITIVITY_HIGH"       # how readily a pause ends the turn
VAD_SILENCE_MS = 800                               # silence that counts as end-of-turn

# --- silence watchdog (the CALLER re-engages) --------------------------------------
# Gemini Live is turn-based: the model only replies after its VAD sees the callee FINISH
# a turn. A callee who simply stays silent never produces an end-of-turn, so the model
# would sit mute indefinitely — but on a phone call the CALLER is the active party and
# should check back in ("still there? anything else?"). The callee has every right to be
# quiet; the bot does not get to be. This watchdog injects a check-in text turn (same
# mechanism as the greeting) after a stretch of MUTUAL silence, a bounded number of times;
# after MAX_NUDGES it goes quiet and lets the call's mutual-silence hang-up end things.
NUDGE_AFTER_SILENCE_S = 12.0   # mutual quiet before the caller checks in
MAX_NUDGES = 3                 # consecutive unanswered check-ins before giving up
NUDGE_TRIGGER = (
    "(The other person has gone quiet for a while. In ONE short, warm sentence, check "
    "whether they're still there or need anything else. If they earlier asked for a "
    "moment to think, just gently reassure them you're still on the line. Do NOT repeat "
    "your previous message, and do not read this instruction aloud.)"
)


# --- persistent debug log -----------------------------------------------------------
# Every [voice] event + the full transcript is mirrored to a timestamped per-call file
# under <repo>/logs/ (elapsed-since-start stamps make latency easy to investigate — e.g.
# how long pickup → greet-sent → greet-delivered → first-audio actually takes). stdout is
# unchanged; the file is best-effort (a write failure never breaks the call).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_LOG_LOCK = threading.Lock()
_LOGF = None          # open file handle, or None (→ stdout-only)
_LOG_T0 = None        # monotonic start, for "+N.NNs" elapsed stamps
_LOG_PATH = None      # the .log path (sibling audio recordings derive from it)


def _open_logfile(repo_root: "str | None" = None) -> "str | None":
    """Open a timestamped per-call debug log under <repo_root>/logs/. Returns its path,
    or None if it couldn't be created (logging then stays stdout-only). Safe to call more
    than once — the first successful open wins for the process."""
    global _LOGF, _LOG_T0, _LOG_PATH
    if _LOGF is not None:
        return _LOG_PATH
    try:
        d = os.path.join(repo_root or _REPO_ROOT, "logs")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"gemini_call_{time.strftime('%Y%m%d_%H%M%S')}.log")
        _LOGF = open(path, "a", encoding="utf-8", buffering=1)  # line-buffered
        _LOG_T0 = time.monotonic()
        _LOG_PATH = path
        _LOGF.write(f"{_stamp()} [voice] === log opened: {path} ===\n")
        return path
    except OSError:
        _LOGF = None
        return None


def _sibling_path(suffix: str) -> "str | None":
    """A path next to the debug log, e.g. _sibling_path('_ear.wav') →
    logs/gemini_call_<ts>_ear.wav. None if no log file is open."""
    if not _LOG_PATH:
        return None
    base = _LOG_PATH[:-4] if _LOG_PATH.endswith(".log") else _LOG_PATH
    return base + suffix


def _stamp() -> str:
    el = (time.monotonic() - _LOG_T0) if _LOG_T0 is not None else 0.0
    return f"{time.strftime('%H:%M:%S')} +{el:7.2f}s"


def _logfile_write(line: str) -> None:
    """Append a raw line (already formatted) to the debug log. Never raises."""
    if _LOGF is None:
        return
    try:
        with _LOG_LOCK:
            _LOGF.write(line if line.endswith("\n") else line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _log(msg: str) -> None:
    print(f"   [voice] {msg}", flush=True)
    _logfile_write(f"{_stamp()} [voice] {msg}")


def _run(cmd: "list[str]", *, timeout: float = 10.0) -> "tuple[int, str]":
    """Run a short command → (returncode, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def _tools_ok() -> "str | None":
    for t in ("pactl", "ffmpeg"):
        if not shutil.which(t):
            return f"missing '{t}' (need pactl + ffmpeg for the audio bridge)"
    return None


def load_gemini_key(repo_root: "str | None" = None) -> "str | None":
    """GEMINI_API_KEY from the environment, else parsed out of the repo .env.

    The .env parse mirrors the hard-won rule from MEMORY.md: split on the FIRST '=',
    strip surrounding quotes, and drop a trailing ` # comment` only when the value is
    NOT quoted (so a key containing '#' inside quotes survives)."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return key.strip()
    root = repo_root or os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    env_path = os.path.join(root, ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
                    continue
                v = v.strip()
                if v[:1] in ("'", '"') and v[-1:] == v[:1] and len(v) >= 2:
                    v = v[1:-1]
                else:
                    v = v.split(" #", 1)[0].strip()  # inline comment on unquoted value
                if v:
                    return v
    except OSError:
        pass
    return None


def build_live_config(*, system: str, voice: str, language: "str | None" = None) -> dict:
    """The LiveConnectConfig as a plain dict (the SDK coerces it). AUDIO out, both
    transcriptions on (so the bridge can print what each side said), prebuilt voice."""
    cfg: dict = {
        "response_modalities": ["AUDIO"],
        "system_instruction": system,
        "output_audio_transcription": {},
        "input_audio_transcription": {},
        "speech_config": {
            "voice_config": {"prebuilt_voice_config": {"voice_name": voice}},
        },
        # Make the model actually take its turn when the callee speaks (see the VAD_*
        # constants): a live-call monitor is never truly silent, so the stock VAD can
        # leave the model listening forever without ever replying.
        "realtime_input_config": {
            "automatic_activity_detection": {
                "start_of_speech_sensitivity": VAD_START_SENSITIVITY,
                "end_of_speech_sensitivity": VAD_END_SENSITIVITY,
                "silence_duration_ms": VAD_SILENCE_MS,
            },
        },
    }
    if language:
        cfg["speech_config"]["language_code"] = language
    return cfg


class GeminiVoiceBridge:
    """Lifecycle: setup_devices() → (worker thread) asyncio.run(run()) → signal_stop()
    → teardown_devices(). setup/teardown are sync (pactl) and run in the orchestrator's
    MAIN thread BEFORE/AFTER the call so the default source+sink are in place when the
    browser places the call; run() (the ffmpeg I/O + Gemini session) runs in a worker
    thread with its own event loop, since the main thread is busy driving the browser."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        system: str = DEFAULT_SYSTEM,
        language: "str | None" = None,
        greet: bool = True,
        greet_text: "str | None" = None,
        record: bool = True,
        nudge_on_silence: bool = True,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.config = build_live_config(system=system, voice=voice, language=language)
        self.greet = greet
        # Caller re-engagement on a silent callee (see NUDGE_* constants).
        self.nudge_on_silence = nudge_on_silence
        self._last_voice_activity = 0.0   # monotonic ts of the last speech EITHER way
        self._nudges_sent = 0             # consecutive unanswered check-ins
        # What the model is told to say first on pickup. Default = the generic English greeting;
        # an incident-report run overrides it with the briefing trigger (see gemini_call).
        self.greet_text = greet_text or GREET_TRIGGER
        self.record = record          # dump both audio directions to WAV for debugging
        self._mouth_wav = None        # wave.Wave_write — Gemini's voice (24 kHz)
        self._ear_wav = None          # wave.Wave_write — the callee's voice (16 kHz)
        self._ear_frames = 0          # samples written each side (for a duration log)
        self._mouth_frames = 0
        self._modules: "list[str]" = []
        self._prev_source: "str | None" = None
        self._prev_sink: "str | None" = None
        self._mouth: "subprocess.Popen | None" = None   # Gemini PCM → ai_mic_sink
        self._ear: "subprocess.Popen | None" = None      # gemini_call_spk.monitor → PCM
        self._devices_ready = False
        # cross-thread stop: _stopping (threading) ticks the ear read loop; _stop_async
        # (asyncio, created inside run()) unblocks the receive/queue tasks via the loop.
        self._stopping = threading.Event()
        self._loop: "asyncio.AbstractEventLoop | None" = None
        self._stop_async: "asyncio.Event | None" = None
        self._session = None          # set in run(); used by trigger_greet()
        self._greeted = False         # greet fires once (on answer)
        # The EAR is GATED until the callee truly answers: pre-pickup audio (ringback /
        # null-sink silence the model mis-hears as speech) must NOT reach Gemini, or it
        # babbles a reply to nobody during the ring and the on-pickup greeting is no
        # longer the FIRST thing said. _ear_to_gemini drains+discards until this is set.
        self._answered = threading.Event()
        # The greeting must be DELIVERED before the ear opens, so the callee's noise /
        # voice / silence can never pre-empt or cut it (the user's contract: pick up →
        # AI greets immediately, no matter what the callee's side sounds like).
        # _gemini_to_queue sets _greet_done on the greeting turn's turn_complete; the ear
        # waits on it (bounded by GREET_MAX_WAIT) before opening.
        self._greet_done = threading.Event()
        self._awaiting_greet = False  # True between greet-sent and its turn_complete

    # -- device plumbing (sync, main thread) ------------------------------------------
    def setup_devices(self) -> bool:
        path = _open_logfile(_REPO_ROOT)
        if path:
            _log(f"debug log → {path}")
        reason = _tools_ok()
        if reason:
            _log(f"unavailable: {reason}")
            return False
        # Self-heal: a prior run killed before teardown (e.g. a hard Ctrl+C during the
        # call) can leak our virtual modules AND leave them as the system default —
        # breaking the real mic/speakers in every app, and poisoning the "previous
        # default" captured below (teardown would then "restore" to a dead device). Clear
        # any such leftovers first, so each run starts from clean, real hardware defaults.
        self._unload_stale_modules()
        # MOUTH: null sink + remap its monitor as a capture source = the browser's mic.
        if self._load("module-null-sink", f"sink_name={MOUTH_SINK}",
                       "sink_properties=device.description=AI_Voice_Mic_Sink") is None:
            self.teardown_devices()
            return False
        if self._load("module-remap-source", f"master={MOUTH_SINK}.monitor",
                       f"source_name={MOUTH_SOURCE}",
                       "source_properties=device.description=AI_Voice_Mic") is None:
            self.teardown_devices()
            return False
        # EAR: a null sink the browser plays the call into; we record its monitor.
        if self._load("module-null-sink", f"sink_name={EAR_SINK}",
                       "sink_properties=device.description=Gemini_Call_Speaker") is None:
            self.teardown_devices()
            return False
        # Make them the defaults so the (pre-granted) browser grabs them for the call.
        # Never record OUR OWN virtual device as the "previous" default (a leak we missed,
        # or a re-entrant setup) — restoring to it on teardown would leave the real
        # hardware unselected.
        rc, out = _run(["pactl", "get-default-source"])
        prev_src = out.strip() if rc == 0 else ""
        self._prev_source = prev_src if prev_src and prev_src != MOUTH_SOURCE else None
        rc, out = _run(["pactl", "get-default-sink"])
        prev_snk = out.strip() if rc == 0 else ""
        self._prev_sink = prev_snk if prev_snk and prev_snk != EAR_SINK else None
        _run(["pactl", "set-default-source", MOUTH_SOURCE])
        _run(["pactl", "set-default-sink", EAR_SINK])
        # Pin the AI mic to unity + unmute. ai_mic is now the system DEFAULT source, so
        # the OS "Microphone" slider points at it — whatever it was left at (low, or
        # muted) becomes the gain on Gemini's voice into the call, making the AI quiet or
        # silent for reasons unrelated to the call. 100% = unity (no amplification), so
        # the model's loudness to the callee is deterministic regardless of the slider.
        _run(["pactl", "set-source-mute", MOUTH_SOURCE, "0"])
        _run(["pactl", "set-source-volume", MOUTH_SOURCE, "100%"])
        # Same for the ear: pin the callee's playback level into Gemini to unity/unmuted.
        _run(["pactl", "set-sink-mute", EAR_SINK, "0"])
        _run(["pactl", "set-sink-volume", EAR_SINK, "100%"])
        self._devices_ready = True
        _log(f"virtual devices ready: mic={MOUTH_SOURCE} (was {self._prev_source or '?'}), "
             f"speaker={EAR_SINK} (was {self._prev_sink or '?'})")
        return True

    def teardown_devices(self) -> None:
        self._stop_io()
        if self._prev_source:
            _run(["pactl", "set-default-source", self._prev_source])
            self._prev_source = None
        if self._prev_sink:
            _run(["pactl", "set-default-sink", self._prev_sink])
            self._prev_sink = None
        for mid in reversed(self._modules):
            _run(["pactl", "unload-module", mid])
        self._modules = []
        self._devices_ready = False
        _log("virtual devices torn down (default source + sink restored)")

    def _load(self, name: str, *params: str) -> "str | None":
        rc, out = _run(["pactl", "load-module", name, *params])
        mid = out.strip()
        if rc == 0 and mid.isdigit():
            self._modules.append(mid)
            return mid
        _log(f"load-module {name} failed: {out.strip()[:160]!r}")
        return None

    def _unload_stale_modules(self) -> None:
        """Unload any of OUR virtual-device modules left over from a previous run that
        didn't tear down cleanly. Matches only null-sink / remap-source modules whose
        arguments name our own sinks/source (MOUTH_SINK / MOUTH_SOURCE / EAR_SINK), so it
        never touches unrelated audio. Unloading them lets PulseAudio revert the system
        default back to real hardware before we capture it. Best-effort; never raises."""
        rc, out = _run(["pactl", "list", "short", "modules"])
        if rc != 0:
            return
        ours = (MOUTH_SINK, MOUTH_SOURCE, EAR_SINK)
        unloaded = 0
        for line in out.splitlines():
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            mid, mtype, args = cols[0], cols[1], cols[2]
            if mtype in ("module-null-sink", "module-remap-source") and any(
                    n in args for n in ours):
                r2, _ = _run(["pactl", "unload-module", mid])
                if r2 == 0:
                    unloaded += 1
        if unloaded:
            _log(f"cleaned up {unloaded} leftover virtual-device module(s) from a "
                 "prior run (real audio defaults restored)")

    # -- ffmpeg I/O (started inside run(), torn down in its finally) -------------------
    def _start_io(self) -> bool:
        # MOUTH: read Gemini's 24 kHz mono PCM on stdin → ai_mic_sink (resampled to 48k).
        mouth_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(MOUTH_RATE), "-ac", "1", "-i", "pipe:0",
            "-ac", "2", "-ar", "48000",
            # -buffer_duration 80: shrink the PulseAudio output buffer (ffmpeg's pulse
            # muxer defaults to a large ~1-2s buffer) so Gemini's voice reaches the callee
            # ~immediately, not seconds later — the lag that made the greeting feel late.
            "-f", "pulse", "-device", MOUTH_SINK, "-buffer_duration", "80",
            "gemini-voice-out",
        ]
        # EAR: record the call speaker's monitor → 16 kHz mono PCM on stdout.
        ear_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-f", "pulse", "-i", f"{EAR_SINK}.monitor",
            "-ac", "1", "-ar", str(EAR_RATE), "-f", "s16le", "pipe:1",
        ]
        try:
            self._mouth = subprocess.Popen(
                mouth_cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._ear = subprocess.Popen(
                ear_cmd, stdout=subprocess.PIPE,
                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            _log(f"could not start ffmpeg I/O: {exc}")
            self._stop_io()
            return False
        _log("audio I/O up (mouth→ai_mic_sink, ear←gemini_call_spk.monitor)")
        self._open_recordings()
        return True

    def _open_recordings(self) -> None:
        """Open WAV writers for both audio directions (debug). Best-effort: a failure just
        skips recording. mouth = Gemini's voice (24 kHz, what the callee hears); ear = the
        callee's voice (16 kHz, exactly the PCM streamed into Gemini, gated audio too)."""
        if not self.record:
            return
        mp, ep = _sibling_path("_mouth.wav"), _sibling_path("_ear.wav")
        try:
            if mp:
                self._mouth_wav = wave.open(mp, "wb")
                self._mouth_wav.setnchannels(1)
                self._mouth_wav.setsampwidth(2)
                self._mouth_wav.setframerate(MOUTH_RATE)
            if ep:
                self._ear_wav = wave.open(ep, "wb")
                self._ear_wav.setnchannels(1)
                self._ear_wav.setsampwidth(2)
                self._ear_wav.setframerate(EAR_RATE)
            if mp or ep:
                _log(f"recording audio → {os.path.basename(mp or '')} (AI) + "
                     f"{os.path.basename(ep or '')} (callee)")
        except Exception as exc:  # noqa: BLE001
            _log(f"could not open audio recordings (continuing without): {exc}")
            self._mouth_wav = self._ear_wav = None

    def _close_recordings(self) -> None:
        for tag, w, rate, frames in (
                ("AI/mouth", self._mouth_wav, MOUTH_RATE, self._mouth_frames),
                ("callee/ear", self._ear_wav, EAR_RATE, self._ear_frames)):
            if w is None:
                continue
            try:
                w.close()
                _log(f"saved {tag} recording: {frames / rate:.1f}s")
            except Exception:  # noqa: BLE001
                pass
        self._mouth_wav = self._ear_wav = None

    def _stop_io(self) -> None:
        self._close_recordings()
        for proc in (self._mouth, self._ear):
            if proc is None:
                continue
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._mouth = self._ear = None

    # -- async session (worker thread) ------------------------------------------------
    def signal_stop(self) -> None:
        """Ask run() to stop — safe to call from the orchestrator's main thread."""
        self._stopping.set()
        loop, ev = self._loop, self._stop_async
        if loop is not None and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except Exception:  # noqa: BLE001
                pass

    def on_join(self) -> None:
        """meet_call_browser's EARLY join signal — may fire DURING the ringback (the flaky
        WebRTC track-count bump), so do nothing audible here. Kept as a hook for future
        side-effect-free prep; the real work waits for on_pickup (a confirmed answer)."""
        return

    def on_pickup(self) -> None:
        """The callee TRULY answered (meet_call_browser's ringback-safe pickup signal).
        Greet FIRST, then connect + open the ear — so the model's hello is the first thing
        said, never lost into the ringback. Runs the blocking move/open off the call loop's
        thread (a short daemon) so it never stalls meet_call_browser's poller."""
        threading.Thread(
            target=self._on_pickup_work, name="pickup", daemon=True).start()

    def _on_pickup_work(self) -> None:
        # 1) Say hi FIRST — fires immediately on the text trigger, independent of any
        #    callee audio (so it greets the same on noise / voice / silence).
        self.trigger_greet()
        # 2) Connect the callee's voice to Gemini's ear: move the browser's call playback
        #    onto the capture sink (the default-sink preset alone is unreliable — Chrome
        #    pins its output device at renderer start, so the call stream can keep playing
        #    to the OLD default). Blocks ~0.6–1.2s while it retries.
        self.move_browser_playback()
        # 3) Hold the ear CLOSED until the greeting is delivered, so the callee's side can
        #    never pre-empt or cut it. Bounded so a missed turn_complete can't deafen us.
        if self.greet:
            if self._greet_done.wait(timeout=GREET_MAX_WAIT):
                _log("greeting delivered — opening the ear")
            else:
                _log(f"greeting not confirmed within {GREET_MAX_WAIT:.0f}s — "
                     "opening the ear anyway")
        # 4) NOW open the ear; from here Gemini hears the callee live (two-way). Start the
        #    silence clock here so the watchdog measures quiet from the moment we're
        #    actually listening, not from session connect.
        self._last_voice_activity = time.monotonic()
        self._answered.set()
        _log("ear opened — Gemini is now listening to the callee")

    def move_browser_playback(self, *, attempts: int = 10, delay: float = 0.6) -> bool:
        """Best-effort: move the browser's call playback sink-input(s) onto EAR_SINK so
        their monitor carries the callee's voice. Retries (the call's playback stream
        appears a beat after answer) and logs each sink-input it sees for debuggability.
        Returns True once it moved ≥1 stream. Mirrors AudioInjector.move_browser_mic."""
        for attempt in range(1, attempts + 1):
            rc, out = _run(["pactl", "list", "sink-inputs"])
            if rc != 0:
                time.sleep(delay)
                continue
            moved = 0
            for chunk in out.split("Sink Input #")[1:]:
                m = re.match(r"\s*(\d+)", chunk)
                if not m:
                    continue
                siid = m.group(1)
                low = chunk.lower()
                app = re.search(r'application\.name = "([^"]*)"', chunk)
                cur = re.search(r"\n\s*Sink: (\d+)", chunk)
                is_browser = any(k in low for k in _BROWSER_MATCH)
                if attempt == 1 or is_browser:
                    _log(f"  sink-input #{siid} app={(app.group(1) if app else '?')!r} "
                         f"sink={cur.group(1) if cur else '?'} "
                         f"browser={'y' if is_browser else 'n'}")
                if not is_browser:
                    continue
                rc2, err = _run(["pactl", "move-sink-input", siid, EAR_SINK])
                if rc2 == 0:
                    moved += 1
                else:
                    _log(f"  move #{siid} → {EAR_SINK} failed: {err.strip()[:80]!r}")
            if moved:
                _log(f"moved {moved} browser playback stream(s) → {EAR_SINK} "
                     f"(attempt {attempt}/{attempts}) — Gemini's ear is live")
                return True
            time.sleep(delay)
        _log(f"no browser playback sink-input found after {attempts} attempts — Gemini "
             "may hear nothing (the call audio is playing elsewhere)")
        return False

    def trigger_greet(self) -> None:
        """Make Gemini greet NOW — call this the instant the callee answers (an
        on-connect greeting would be lost during the ring). Safe to call from another
        thread (e.g. meet_call_browser's on_join in the main thread); schedules the
        send on the session's own event loop. No-op if greeting is disabled or the
        session isn't connected yet."""
        if not self.greet or self._greeted:
            return
        loop, session = self._loop, self._session
        if loop is None or session is None:
            return
        self._greeted = True
        self._awaiting_greet = True  # cleared when the greeting turn completes

        async def _do() -> None:
            try:
                # MUST be send_realtime_input, NOT send_client_content: on
                # gemini-3.1-flash-live-preview the latter only SEEDS history and does
                # not trigger an immediate response, so the greeting wouldn't be spoken
                # until the model got a realtime audio turn (= after the ear opened / the
                # callee made a sound) — the long wait before hearing the greeting. This
                # makes the model speak the instant the callee answers. (Proven by
                # demo_incident_call.py for this exact model.)
                await session.send_realtime_input(text=self.greet_text)
                _log("greet trigger sent (callee answered)")
            except Exception as exc:  # noqa: BLE001
                _log(f"greet trigger failed (continuing reactive): {exc}")

        try:
            asyncio.run_coroutine_threadsafe(_do(), loop)
        except Exception:  # noqa: BLE001
            pass

    async def run(self) -> None:
        """Connect to Gemini Live and bridge audio until signal_stop() (or an error)."""
        from google import genai

        self._loop = asyncio.get_running_loop()
        self._stop_async = asyncio.Event()
        if not self._start_io():
            return
        out_q: "asyncio.Queue[bytes]" = asyncio.Queue()
        client = genai.Client(api_key=self.api_key)
        try:
            async with client.aio.live.connect(model=self.model, config=self.config) as session:
                self._session = session
                _log(f"connected to Gemini Live ({self.model}) — bridging the call")
                # NOTE: the greeting is NOT sent here — it's driven by trigger_greet()
                # on the answer (an on-connect greeting would be lost during the ring).
                tasks = [
                    asyncio.create_task(self._ear_to_gemini(session)),
                    asyncio.create_task(self._gemini_to_queue(session, out_q)),
                    asyncio.create_task(self._queue_to_mouth(out_q)),
                    asyncio.create_task(self._stop_async.wait()),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            _log(f"Gemini Live session error: {type(exc).__name__}: {exc}")
        finally:
            self._session = None
            self._stop_io()
            _log("Gemini Live session closed")

    async def _ear_to_gemini(self, session) -> None:
        """Stream the callee's voice (ear ffmpeg stdout) into Gemini as realtime input."""
        assert self._ear is not None and self._ear.stdout is not None
        stdout = self._ear.stdout
        while not self._stopping.is_set():
            data = await asyncio.to_thread(stdout.read, EAR_CHUNK)
            if not data:
                break
            # Debug recording: capture EVERYTHING the ear pipeline produced (incl. the
            # pre-pickup audio that's gated out below), so the WAV shows what Gemini's ear
            # heard at every moment — useful for diagnosing the gate / silence / routing.
            if self._ear_wav is not None:
                try:
                    self._ear_wav.writeframes(data)
                    self._ear_frames += len(data) // 2
                except Exception:  # noqa: BLE001
                    pass
            # GATED until the callee answers: keep DRAINING the pipe (so it never backs up
            # and stays current) but DISCARD it — pre-pickup ringback/silence must not make
            # Gemini speak before the greeting. on_pickup sets _answered after it greets.
            if not self._answered.is_set():
                continue
            # Caller re-engagement: if BOTH sides have been quiet past the threshold (and
            # the opening briefing is delivered), nudge the model to check in. Done HERE,
            # in the one task that sends to Gemini, so the text turn never interleaves with
            # an audio frame on the wire. Bounded by MAX_NUDGES; the callee speaking resets
            # both the clock and the count (see _gemini_to_queue).
            if (self.nudge_on_silence and not self._awaiting_greet
                    and self._nudges_sent < MAX_NUDGES
                    and self._last_voice_activity > 0.0):
                idle = time.monotonic() - self._last_voice_activity
                if idle >= NUDGE_AFTER_SILENCE_S:
                    try:
                        await session.send_realtime_input(text=NUDGE_TRIGGER)
                        self._nudges_sent += 1
                        self._last_voice_activity = time.monotonic()
                        _log(f"silence nudge sent ({self._nudges_sent}/{MAX_NUDGES}) — "
                             f"callee quiet {idle:.0f}s")
                    except Exception as exc:  # noqa: BLE001
                        _log(f"silence nudge failed: {exc}")
            try:
                await session.send_realtime_input(
                    audio={"data": data, "mime_type": f"audio/pcm;rate={EAR_RATE}"})
            except Exception as exc:  # noqa: BLE001
                _log(f"send_realtime_input failed: {exc}")
                break

    async def _gemini_to_queue(self, session, out_q: "asyncio.Queue[bytes]") -> None:
        """Receive Gemini's audio + transcripts; queue audio for the mouth, print + log the
        conversation, and time the greeting (sent→first-audio) for latency investigation."""
        # Which speaker's console line is currently open: 'ai' | 'callee' | None. The
        # emoji prefix is printed ONCE when a speaker starts an utterance; their streamed
        # chunks then append to that same line, so the console reads as one continuous
        # sentence instead of re-stamping the prefix on every chunk.
        cur_spk: "str | None" = None
        greet_audio_logged = False
        tx_parts: "list[str]" = []
        tx_spk = ""

        def flush_tx() -> None:
            """Write the buffered utterance as one timestamped line in the debug log."""
            nonlocal tx_parts
            if tx_parts and tx_spk:
                _logfile_write(f"{_stamp()} {tx_spk} {''.join(tx_parts).strip()}")
            tx_parts = []

        while not self._stopping.is_set():
            turn = session.receive()
            async for resp in turn:
                sc = getattr(resp, "server_content", None)
                # Barge-in: the callee interrupted → drop audio we haven't played yet.
                if sc is not None and getattr(sc, "interrupted", False):
                    drained = 0
                    while not out_q.empty():
                        try:
                            out_q.get_nowait()
                            drained += 1
                        except Exception:  # noqa: BLE001
                            break
                    if drained:
                        _log(f"barge-in: callee interrupted — dropped {drained} queued "
                             "audio chunk(s)")
                # Audio out (prefer the SDK convenience, fall back to model_turn parts).
                got_audio = False
                data = getattr(resp, "data", None)
                if isinstance(data, bytes) and data:
                    out_q.put_nowait(data)
                    got_audio = True
                elif sc is not None and getattr(sc, "model_turn", None):
                    for part in sc.model_turn.parts or []:
                        inline = getattr(part, "inline_data", None)
                        if inline is not None and isinstance(inline.data, bytes):
                            out_q.put_nowait(inline.data)
                            got_audio = True
                # Latency probe: when the greeting's first audio lands (still awaiting its
                # turn_complete), log it — the gap from "greet trigger sent" is the model's
                # speak-up latency, the thing the user investigates when the hello is slow.
                if got_audio and self._awaiting_greet and not greet_audio_logged:
                    greet_audio_logged = True
                    _log("first greeting audio chunk → mouth (model started speaking)")
                # The AI speaking counts as activity → resets the silence watchdog so it
                # measures quiet from the END of the AI's turn, not the last callee word.
                if got_audio:
                    self._last_voice_activity = time.monotonic()
                # Transcripts (console stream + per-utterance lines in the debug log).
                # On a speaker change: close the open console line, flush the finished
                # utterance to the log, then print the new speaker's emoji prefix ONCE.
                if sc is not None:
                    ot = getattr(sc, "output_transcription", None)
                    if ot is not None and getattr(ot, "text", None):
                        if cur_spk != "ai":
                            if cur_spk is not None:
                                print(flush=True)
                            flush_tx()
                            print("   🤖 ", end="", flush=True)
                            cur_spk = "ai"
                        print(ot.text, end="", flush=True)
                        tx_parts.append(ot.text)
                        tx_spk = "🤖 AI:"
                    it = getattr(sc, "input_transcription", None)
                    if it is not None and getattr(it, "text", None):
                        if cur_spk != "callee":
                            if cur_spk is not None:
                                print(flush=True)
                            flush_tx()
                            print("   🧑 ", end="", flush=True)
                            cur_spk = "callee"
                        print(it.text, end="", flush=True)
                        tx_parts.append(it.text)
                        tx_spk = "🧑 callee:"
                        # Callee spoke → reset the silence clock AND the consecutive-nudge
                        # count (they re-engaged, so the cap is per silent stretch, not
                        # per call).
                        self._last_voice_activity = time.monotonic()
                        self._nudges_sent = 0
                    # Turn finished: close the console line + flush the utterance to the
                    # log; if it was the greeting turn, release the ear (see _on_pickup_work
                    # — turn_complete means the spoken hello was fully emitted, uncuttable).
                    if getattr(sc, "turn_complete", False):
                        if cur_spk is not None:
                            print(flush=True)
                            cur_spk = None
                        flush_tx()
                        if self._awaiting_greet:
                            self._awaiting_greet = False
                            self._greet_done.set()

    async def _queue_to_mouth(self, out_q: "asyncio.Queue[bytes]") -> None:
        """Write queued Gemini audio into the mouth ffmpeg (→ ai_mic_sink → callee)."""
        assert self._mouth is not None and self._mouth.stdin is not None
        stdin = self._mouth.stdin
        while not self._stopping.is_set():
            try:
                chunk = await asyncio.wait_for(out_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            # Debug recording: capture exactly what we feed the mouth = Gemini's voice.
            if self._mouth_wav is not None:
                try:
                    self._mouth_wav.writeframes(chunk)
                    self._mouth_frames += len(chunk) // 2
                except Exception:  # noqa: BLE001
                    pass
            try:
                await asyncio.to_thread(stdin.write, chunk)
                await asyncio.to_thread(stdin.flush)
            except Exception:  # noqa: BLE001 - mouth pipe closed (teardown) → stop
                break


# -- standalone checks --------------------------------------------------------------
def _devices_test(seconds: float) -> int:
    """Set up the two sinks, record the ai_mic source briefly to prove the mic chain,
    then tear everything down. No Gemini, no call."""
    br = GeminiVoiceBridge(api_key="unused")
    if not br.setup_devices():
        return 1
    try:
        import tempfile
        probe = os.path.join(tempfile.gettempdir(), "gemini_voice_probe.wav")
        _log(f"recording {MOUTH_SOURCE} for {seconds:.0f}s (should be near-silent) …")
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
              "-f", "pulse", "-i", MOUTH_SOURCE, "-t", str(seconds),
              "-ac", "1", "-ar", "16000", probe], timeout=seconds + 20)
        ok = os.path.isfile(probe) and os.path.getsize(probe) > 1000
        print(f"DEVICES_TEST {'OK' if ok else 'FAIL'} probe={probe} "
              f"size={os.path.getsize(probe) if os.path.isfile(probe) else 0}")
        return 0 if ok else 1
    finally:
        br.teardown_devices()


def _selftest(model: str, voice: str) -> int:
    """Prove the Gemini Live key/model/audio path WITHOUT a call or PulseAudio: connect,
    ask for a one-line spoken greeting, collect the audio, write it to a WAV, print the
    transcript. Network + a valid GEMINI_API_KEY required."""
    key = load_gemini_key()
    if not key:
        print("SELFTEST FAIL: no GEMINI_API_KEY (env or .env)")
        return 2

    async def _go() -> int:
        from google import genai
        cfg = build_live_config(
            system="You are a friendly assistant. Reply with one short spoken sentence.",
            voice=voice)
        client = genai.Client(api_key=key)
        pcm = bytearray()
        text = []
        async with client.aio.live.connect(model=model, config=cfg) as session:
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": "Say a short friendly hello."}]},
                turn_complete=True)
            try:
                async with asyncio.timeout(30):
                    turn = session.receive()
                    async for resp in turn:
                        d = getattr(resp, "data", None)
                        if isinstance(d, bytes):
                            pcm += d
                        sc = getattr(resp, "server_content", None)
                        if sc is not None:
                            ot = getattr(sc, "output_transcription", None)
                            if ot is not None and getattr(ot, "text", None):
                                text.append(ot.text)
                            if getattr(sc, "turn_complete", False):
                                break
            except asyncio.TimeoutError:
                pass
        import tempfile
        out = os.path.join(tempfile.gettempdir(), "gemini_voice_selftest.wav")
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(MOUTH_RATE)
            w.writeframes(bytes(pcm))
        print(f"SELFTEST {'OK' if pcm else 'FAIL'} bytes={len(pcm)} wav={out}")
        if text:
            print(f"  transcript: {''.join(text).strip()!r}")
        return 0 if pcm else 1

    try:
        return asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        print(f"SELFTEST FAIL: {type(exc).__name__}: {exc}")
        return 1


def _main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Gemini Live ⇄ call audio bridge (engine).")
    ap.add_argument("--devices-test", action="store_true",
                    help="set up the 2 virtual sinks, probe the mic chain, tear down.")
    ap.add_argument("--selftest", action="store_true",
                    help="Gemini Live text→audio round-trip → WAV (needs key + network).")
    ap.add_argument("--seconds", type=float, default=3.0, help="--devices-test probe time.")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    a = ap.parse_args(argv)
    if a.devices_test:
        return _devices_test(a.seconds)
    if a.selftest:
        return _selftest(a.model, a.voice)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
