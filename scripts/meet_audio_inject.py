#!/usr/bin/env python3
"""Inject audio into the OUTGOING mic of the browser call — the "AI mouth" path.

Counterpart to ``meet_audio_capture.py`` (the "AI ear"). Capture records the call's
DECODED remote audio off an OS sink monitor; this module does the REVERSE: it makes
the CALLER play arbitrary audio that the CALLEE hears, by

  1. creating a VIRTUAL microphone — a PulseAudio/PipeWire ``module-null-sink``
     (``ai_mic_sink``) whose monitor is re-exposed as a capture source via
     ``module-remap-source`` (``ai_mic``);
  2. making ``ai_mic`` the DEFAULT capture device, so the browser's getUserMedia for
     the call grabs it as the microphone (the call must START after this, which the
     orchestrator guarantees — setup() runs before the call button is clicked);
  3. playing an audio file into ``ai_mic_sink`` with ffmpeg (``-re``, optionally
     looping) → it flows monitor → remap → browser mic → the callee.

Why a virtual mic and NOT Chrome's ``--use-file-for-fake-audio-capture``: the call
runs in the user's already-running daily Brave over CDP, so we can't relaunch it with
fake-device flags. The virtual-mic + default-source swap works on a live browser and
is fully REVERSIBLE — stop() restores the previous default source and unloads the
modules. As a belt-and-suspenders, move_browser_mic() can also move the browser's live
mic source-output onto ``ai_mic`` after the callee answers (covers a getUserMedia that
pinned a specific deviceId instead of "default").

Eventual use: swap the static file for Gemini Live's TTS output streamed into
``ai_mic_sink`` so the AI actually TALKS on the call (format is whatever ffmpeg can
decode; it resamples to the sink rate).

All pactl/ffmpeg calls are best-effort: a failure logs + degrades, never raises into
the call orchestrator.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import wave

_SINK_NAME = "ai_mic_sink"     # null sink we play into
_SOURCE_NAME = "ai_mic"        # remapped capture source the browser uses as its mic
# A browser mic source-output we may move onto ai_mic (move_browser_mic fallback).
_BROWSER_MATCH = ("brave", "chrom", "chrome", "meet", "google")


def _log(msg: str) -> None:
    print(f"   [inject] {msg}", flush=True)


def _run(cmd: "list[str]", *, timeout: float = 10.0) -> "tuple[int, str]":
    """Run a short command → (returncode, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def _tools_ok() -> "str | None":
    """None if pactl+ffmpeg are present, else a human reason string."""
    for t in ("pactl", "ffmpeg"):
        if not shutil.which(t):
            return f"missing '{t}' (need pactl + ffmpeg for audio injection)"
    return None


def _make_test_tone(path: str, *, rate: int = 48_000) -> str:
    """Write a short, UNMISTAKABLE 4-note rising arpeggio (C5-E5-G5-C6) to ``path``.

    Deliberately NOT a steady tone — a phone ringback is steady, so a melodic run is
    instantly recognisable on the callee's device as "the caller is sending this on
    purpose", not the ring. ~2.3 s; looped by the player.
    """
    notes = (523.25, 659.25, 783.99, 1046.50)  # C5 E5 G5 C6
    note_s, gap_s, amp = 0.30, 0.07, 0.6
    fade = int(rate * 0.006)  # 6 ms click-free envelope
    frames = bytearray()
    for f in notes:
        n = int(rate * note_s)
        for i in range(n):
            env = 1.0
            if i < fade:
                env = i / fade
            elif i > n - fade:
                env = max(0.0, (n - i) / fade)
            s = amp * env * math.sin(2 * math.pi * f * (i / rate))
            frames += struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
        frames += b"\x00\x00" * int(rate * gap_s)
    frames += b"\x00\x00" * int(rate * 0.4)  # trailing gap = clear loop boundary
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


class AudioInjector:
    """Lifecycle: setup() → play() → (optionally move_browser_mic) → stop().

    Idempotent + self-restoring: stop() always restores the previous default source
    and unloads every module it loaded, even if setup() only partially succeeded.
    """

    def __init__(
        self,
        audio_path: "str | None" = None,
        *,
        loop: bool = True,
    ) -> None:
        self.audio_path = audio_path  # None → a generated test tone (made in setup)
        self.loop = loop
        self.sink_name = _SINK_NAME
        self.source_name = _SOURCE_NAME
        self._modules: "list[str]" = []      # loaded module ids (unload in reverse)
        self._prev_default_source: "str | None" = None
        self._play: "subprocess.Popen | None" = None
        self._tmp_tone: "str | None" = None
        self._ready = False

    # -- lifecycle ---------------------------------------------------------------
    def setup(self) -> bool:
        reason = _tools_ok()
        if reason:
            _log(f"unavailable: {reason}")
            return False
        # 1) null sink we play into
        mid = self._load_module(
            "module-null-sink",
            f"sink_name={self.sink_name}",
            "sink_properties=device.description=AI_Caller_Mic_Sink",
        )
        if mid is None:
            _log("could not load null sink — injection disabled")
            self.stop()
            return False
        # 2) re-expose its monitor as a real capture source (the virtual mic)
        mid = self._load_module(
            "module-remap-source",
            f"master={self.sink_name}.monitor",
            f"source_name={self.source_name}",
            "source_properties=device.description=AI_Caller_Mic",
        )
        if mid is None:
            _log("could not load remap source — injection disabled")
            self.stop()
            return False
        # 3) make the virtual mic the default capture device (so the call grabs it)
        rc, out = _run(["pactl", "get-default-source"])
        self._prev_default_source = out.strip() if rc == 0 and out.strip() else None
        rc, _ = _run(["pactl", "set-default-source", self.source_name])
        if rc != 0:
            _log(f"warning: could not set default source to {self.source_name} "
                 "(will rely on move_browser_mic after answer)")
        else:
            _log(f"default mic → {self.source_name} "
                 f"(was: {self._prev_default_source or 'unknown'})")
        # 4) resolve the audio to play
        if not self.audio_path:
            self._tmp_tone = os.path.join(
                tempfile.gettempdir(), "ai_caller_test_tone.wav")
            _make_test_tone(self._tmp_tone)
            self.audio_path = self._tmp_tone
            _log("no --inject-audio file: using a generated 4-note test tone")
        if not os.path.isfile(self.audio_path):
            _log(f"audio file not found: {self.audio_path} — injection disabled")
            self.stop()
            return False
        self._ready = True
        _log(f"virtual mic ready: {self.source_name} ← {self.sink_name} "
             f"← {os.path.basename(self.audio_path)}")
        return True

    def play(self) -> bool:
        """Start streaming the audio into ai_mic_sink (callee hears it once answered)."""
        if not self._ready:
            return False
        if self._play is not None and self._play.poll() is None:
            return True  # already playing
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-re"]
        if self.loop:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", self.audio_path,
                "-ac", "2", "-ar", "48000",
                "-f", "pulse", "-device", self.sink_name, "ai-caller-inject"]
        try:
            self._play = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as exc:  # noqa: BLE001
            _log(f"could not start ffmpeg player: {exc}")
            return False
        time.sleep(0.5)  # let ffmpeg open the device / fail fast
        if self._play.poll() is not None:
            err = (self._play.stderr.read().decode("utf-8", "replace")
                   if self._play.stderr else "")
            _log(f"ffmpeg player exited immediately: {err.strip()[:200]!r}")
            self._play = None
            return False
        _log(f"playing {os.path.basename(self.audio_path)} into {self.sink_name} "
             f"({'looping' if self.loop else 'once'}) — the callee hears this as the caller")
        return True

    def move_browser_mic(self, proc_match: "str | None" = None, *,
                          attempts: int = 8, delay: float = 0.7) -> bool:
        """Best-effort: move the browser's live mic source-output onto ai_mic.

        Covers the case where getUserMedia pinned a specific deviceId (so the
        default-source swap in setup() didn't take). Retries (the capture stream can
        appear a beat after answer, and only exists once the mic is UNMUTED) and logs
        every source-output it sees — app name, current source, corked state — so a
        silent run is debuggable (e.g. "no browser source-output" ⇒ mic still muted).
        Matches by app-name keywords; with ``proc_match`` also matches a source-output
        whose process id is in that tree. Returns True if it moved ≥1 stream.
        """
        for attempt in range(1, attempts + 1):
            rc, out = _run(["pactl", "list", "source-outputs"])
            if rc != 0:
                time.sleep(delay)
                continue
            chunks = out.split("Source Output #")[1:]
            moved = 0
            for chunk in chunks:
                m = re.match(r"\s*(\d+)", chunk)
                if not m:
                    continue
                soid = m.group(1)
                low = chunk.lower()
                app = re.search(r'application\.name = "([^"]*)"', chunk)
                cur = re.search(r"\n\s*Source: (\d+)", chunk)
                corked = "corked: yes" in low
                is_browser = any(k in low for k in _BROWSER_MATCH)
                if not is_browser and proc_match:
                    pm = re.search(r'application\.process\.id = "(\d+)"', chunk)
                    if pm and proc_match in _proc_cmdline(pm.group(1)):
                        is_browser = True
                if attempt == 1 or is_browser:
                    _log(f"  source-output #{soid} app={(app.group(1) if app else '?')!r} "
                         f"src={cur.group(1) if cur else '?'} "
                         f"corked={'y' if corked else 'n'} browser={'y' if is_browser else 'n'}")
                if not is_browser:
                    continue
                rc2, err = _run(["pactl", "move-source-output", soid, self.source_name])
                if rc2 == 0:
                    moved += 1
                else:
                    _log(f"  move #{soid} → {self.source_name} failed: {err.strip()[:80]!r}")
            if moved:
                _log(f"moved {moved} browser mic stream(s) → {self.source_name} "
                     f"(attempt {attempt}/{attempts})")
                return True
            time.sleep(delay)
        _log(f"no browser mic source-output found after {attempts} attempts — the bot "
             "mic is likely MUTED or not capturing (callee will hear silence)")
        return False

    def stop(self) -> None:
        # 1) stop the player
        if self._play is not None:
            try:
                self._play.terminate()
                try:
                    self._play.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    self._play.kill()
            except Exception:  # noqa: BLE001
                pass
            self._play = None
        # 2) restore the previous default source
        if self._prev_default_source:
            _run(["pactl", "set-default-source", self._prev_default_source])
            self._prev_default_source = None
        # 3) unload our modules (reverse order: remap-source before its null sink)
        for mid in reversed(self._modules):
            _run(["pactl", "unload-module", mid])
        self._modules = []
        # 4) drop the generated tone
        if self._tmp_tone and os.path.isfile(self._tmp_tone):
            try:
                os.remove(self._tmp_tone)
            except OSError:
                pass
            self._tmp_tone = None
        self._ready = False

    # -- internals ---------------------------------------------------------------
    def _load_module(self, name: str, *params: str) -> "str | None":
        rc, out = _run(["pactl", "load-module", name, *params])
        mid = out.strip()
        if rc == 0 and mid.isdigit():
            self._modules.append(mid)
            return mid
        _log(f"load-module {name} failed: {out.strip()[:160]!r}")
        return None


def _proc_cmdline(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


# -- standalone self-test (no call): prove the virtual-mic chain end-to-end ------
#   python scripts/meet_audio_inject.py --verify
# Sets up the virtual mic, plays the tone, RECORDS the ai_mic SOURCE directly (exactly
# what the browser would capture), prints mean/max dB, then tears everything down.
def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inject-audio", dest="audio", default=None,
                    help="audio file to play (default: a generated 4-note test tone)")
    ap.add_argument("--seconds", type=float, default=6.0,
                    help="how long to play / probe")
    ap.add_argument("--no-loop", action="store_true", help="play once, don't loop")
    ap.add_argument("--verify", action="store_true",
                    help="record the ai_mic source + report volume (proves the chain)")
    a = ap.parse_args()

    inj = AudioInjector(a.audio, loop=not a.no_loop)
    if not inj.setup():
        return 1
    if not inj.play():
        inj.stop()
        return 1
    try:
        if a.verify:
            probe = os.path.join(tempfile.gettempdir(), "ai_mic_probe.wav")
            _log(f"recording the {inj.source_name} source for {a.seconds:.0f}s …")
            _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                  "-f", "pulse", "-i", inj.source_name, "-t", str(a.seconds),
                  "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", probe],
                 timeout=a.seconds + 20)
            _, vd = _run(["ffmpeg", "-hide_banner", "-nostdin", "-i", probe,
                          "-af", "volumedetect", "-f", "null", "-"], timeout=30)
            mean = re.search(r"mean_volume:\s*(\S+ dB)", vd)
            mx = re.search(r"max_volume:\s*(\S+ dB)", vd)
            print(f"PROBE mean={mean.group(1) if mean else '?'} "
                  f"max={mx.group(1) if mx else '?'} file={probe}")
        else:
            time.sleep(a.seconds)
    finally:
        inj.stop()
        _log("torn down (default source restored, modules unloaded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
