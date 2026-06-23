#!/usr/bin/env python3
"""Capture the Meet call's audio (the remote voices the bot's browser receives) to a
WAV, as the INPUT path for a future Gemini Live loop ("put an AI ear on the call").

Output format: 16 kHz, mono, signed-16 LE PCM in a WAV container — exactly what the
Gemini Live API wants as realtime input, so the next increment is a straight swap of
"write to WAV" for "stream frames to Gemini".

Two capture modes:
  * "monitor" (DEFAULT, robust): record the DEFAULT sink's `.monitor` source. Whatever
    the browser plays (the remote voices, Meet's join chime) goes to the default sink
    and is mirrored on its monitor — we just record that. No routing changes, nothing
    to restore, and the operator still HEARS the call. Caveat: it also captures any
    other audio the machine plays to that sink (keep the test focused). This is the
    mode that survives the Bluetooth-default-sink setup here.
  * "isolate" (opt-in via --audio-isolate): a dedicated null sink + MOVE the browser's
    playback stream into it + record its monitor → clean, call-only audio. More fragile
    (depends on matching the browser's PulseAudio stream by app name; if the match
    misses, NOTHING is captured) and it silences the call for the operator. Kept for a
    future per-tab-isolated capture; the matcher still needs hardening against the
    actual Brave stream metadata.

Why monitor beats isolate here (learned live 2026-06-18): a real call run with isolate
captured 37s of pure silence — the mover never matched Brave's sink-input (no "routed"
log line), so Brave kept playing to the BT headset and the null sink stayed empty.
The null-sink mechanism itself is sound (a realtime paplay tone into it was captured at
-25 dB); the breakage was purely the match step. Monitor mode has no match step.

Driven by call/meet_call_browser.py via --capture-audio (start after the call
connects, stop on hang-up). Standalone smoke tests:
    python call/meet_audio_capture.py --selftest                 # 2s, no call
    python call/meet_audio_capture.py --out /tmp/x.wav --seconds 20

All pactl/ffmpeg calls are best-effort: a failure logs + degrades, never raises into
the call loop (audio is an extra sink, not the system of record).
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

# Gemini Live realtime-input format (16 kHz mono s16le PCM).
GEMINI_RATE = 16_000
GEMINI_CHANNELS = 1

# sink-inputs whose app id matches any of these are the browser's audio streams
# (isolate mode only).
_BROWSER_MATCH = ("chrom", "brave", "chrome", "meet", "google")

_SINK_NAME = "meet_capture"


def _log(msg: str) -> None:
    print(f"   [audio] {msg}", flush=True)


def _run(cmd: list[str], *, timeout: float = 10.0) -> "tuple[int, str]":
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
            return f"missing '{t}' (need pactl + ffmpeg for audio capture)"
    return None


def _default_sink() -> "str | None":
    rc, out = _run(["pactl", "get-default-sink"])
    name = out.strip()
    return name if rc == 0 and name else None


def _proc_cmdline(pid: str) -> str:
    """`/proc/<pid>/cmdline` as a space-joined string ('' if unreadable/gone)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _ppid(pid: str) -> "str | None":
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return line.split()[1]
    except Exception:  # noqa: BLE001
        pass
    return None


def _pid_in_tree(pid: str, match: str, *, max_depth: int = 8) -> bool:
    """True if `pid` OR any of its ancestors (up to max_depth) has `match` in its
    cmdline. This is how we scope a PulseAudio sink-input to ONE browser instance:
    the caller Brave is a dedicated profile, so `match` = its --user-data-dir path
    uniquely identifies its whole process tree (the audio-service utility process
    inherits the flag directly; the ppid-walk is a belt-and-suspenders fallback).
    The daily Brave (a different profile) never matches → never captured/muted."""
    seen = 0
    cur: "str | None" = pid
    while cur and cur != "1" and seen < max_depth:
        if match in _proc_cmdline(cur):
            return True
        cur = _ppid(cur)
        seen += 1
    return False


class AudioCapture:
    """Lifecycle: start() → (browser plays) → stop(). Idempotent + self-restoring."""

    def __init__(
        self,
        out_path: str,
        *,
        mode: str = "monitor",
        rate: int = GEMINI_RATE,
        channels: int = GEMINI_CHANNELS,
        sink_name: str = _SINK_NAME,
        app_match: "tuple[str, ...]" = _BROWSER_MATCH,
        proc_match: "str | None" = None,
        match_all: bool = False,
    ) -> None:
        self.out_path = out_path
        self.mode = mode
        self.rate = rate
        self.channels = channels
        self.sink_name = sink_name
        self.app_match = app_match
        # When True (isolate mode), capture EVERY playback sink-input — no app/proc
        # match at all. This is the most robust path on a multi-sink machine where the
        # call's RING and VOICE land on DIFFERENT HDA sinks: moving all streams to one
        # null sink catches the voice wherever it goes. Trade-off: it relocates (mutes)
        # ALL desktop audio for the capture's duration (restored on stop). Use for a
        # focused call test where the call is the only thing playing. Overrides app_match.
        self.match_all = match_all
        # When set, isolate-mode scopes the capture to sink-inputs whose owning PID
        # (application.process.id) belongs to the process tree carrying this token —
        # the caller browser's --user-data-dir path. This is what makes capture
        # CALL-ONLY: it grabs the dedicated caller profile's audio (only the call
        # plays there) and never the daily browser or any other app. None ⇒ legacy
        # app-name matching (grabs ANY "Brave"/"Chrome" stream — not call-scoped).
        self.proc_match = proc_match
        self._module_id: "str | None" = None
        self._rec: "subprocess.Popen | None" = None
        self._moved: list[str] = []        # sink-input ids we relocated (isolate)
        self._stream_armed = False         # have we ever seen a matched stream present?
        self._mover: "threading.Thread | None" = None
        self._stop = threading.Event()
        self._started = False
        self._dbg_sig: "tuple | None" = None   # last mover-debug signature (throttle)
        self._mover_err_logged = False          # one-shot mover-exception log
        self._recs: "list[subprocess.Popen]" = []  # allsinks: one recorder per sink
        self._rec_tmps: list[str] = []             # allsinks: per-sink temp WAV paths

    # --- pactl helpers (isolate mode) -------------------------------------
    def _load_null_sink(self) -> bool:
        rc, out = _run([
            "pactl", "load-module", "module-null-sink",
            f"sink_name={self.sink_name}",
            f"sink_properties=device.description={self.sink_name}",
        ])
        out = out.strip()
        if rc == 0 and out.isdigit():
            self._module_id = out
            return True
        if self._sink_exists():
            _log(f"reusing existing null sink '{self.sink_name}'")
            return True
        _log(f"could not create null sink: {out!r}")
        return False

    def _sink_exists(self) -> bool:
        rc, out = _run(["pactl", "list", "short", "sinks"])
        return rc == 0 and any(
            line.split("\t")[1:2] == [self.sink_name]
            for line in out.splitlines() if "\t" in line
        )

    def _browser_sink_inputs(self) -> list[str]:
        """Sink-input ids to capture. With proc_match set: ONLY streams whose owning
        PID belongs to the caller browser's process tree (call-only). Otherwise: any
        stream whose app metadata matches app_match (legacy, NOT call-scoped)."""
        rc, out = _run(["pactl", "list", "sink-inputs"])
        if rc != 0:
            return []
        ids: list[str] = []
        cur: "str | None" = None
        blob: list[str] = []

        def _flush() -> None:
            if cur is None:
                return
            text = "\n".join(blob)
            if self.match_all:
                # No filter: grab EVERY playback stream. Skip only OUR OWN capture
                # null sink's monitor reader (never move the recorder's source).
                if self.sink_name not in text:
                    ids.append(cur)
                return
            if self.proc_match:
                pm = re.search(r'application\.process\.id\s*=\s*"?(\d+)"?', text)
                if pm and _pid_in_tree(pm.group(1), self.proc_match):
                    ids.append(cur)
                return
            if any(m in text.lower() for m in self.app_match):
                ids.append(cur)

        for line in out.splitlines():
            m = re.match(r"Sink Input #(\d+)", line.strip())
            if m:
                _flush()
                cur = m.group(1)
                blob = []
            else:
                blob.append(line)
        _flush()
        return ids

    def _sink_index_to_name(self) -> "dict[str, str]":
        """{sink_index: sink_name} from `pactl list short sinks` (col0=index, col1=name)."""
        rc, out = _run(["pactl", "list", "short", "sinks"])
        m: "dict[str, str]" = {}
        if rc == 0:
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0].strip().isdigit():
                    m[parts[0].strip()] = parts[1].strip()
        return m

    def _browser_output_sink_name(self) -> "str | None":
        """The NAME of the sink the BROWSER's audio stream is actually routed to, read from
        the sink-input's `Sink:` field. This is what fixes the routing-mismatch bug: on a
        multi-sink machine (HDA exposes several output sinks) the browser can play the call
        to a NON-default sink, so recording `get-default-sink`.monitor captured silence even
        though the operator HEARD the call. Returns None if no browser stream exists yet (the
        caller then falls back to the default sink). If several browser streams exist, prefers
        the one NOT on the default sink (the call audio is the one that revealed the mismatch),
        else the first."""
        rc, out = _run(["pactl", "list", "sink-inputs"])
        if rc != 0:
            return None
        idx_name = self._sink_index_to_name()
        default = _default_sink()
        cur: "str | None" = None
        blob: list[str] = []
        sink_idxs: list[str] = []  # sink indices browser streams are routed to

        def _flush() -> None:
            if cur is None:
                return
            text = "\n".join(blob)
            if any(m in text.lower() for m in self.app_match):
                sm = re.search(r"(?mi)^\s*Sink:\s*(\d+)\s*$", text)
                if sm:
                    sink_idxs.append(sm.group(1))

        for line in out.splitlines():
            m = re.match(r"Sink Input #(\d+)", line.strip())
            if m:
                _flush()
                cur = m.group(1)
                blob = []
            else:
                blob.append(line)
        _flush()
        names = [idx_name[i] for i in sink_idxs if i in idx_name]
        if not names:
            return None
        for n in names:  # prefer a browser stream on a non-default sink
            if n != default:
                return n
        return names[0]

    def _resolve_monitor_sink(self) -> "str | None":
        """The sink whose .monitor we should record: the sink the browser is actually playing
        to (polled briefly — the call stream can appear a beat after media connects), falling
        back to the default sink. Logs the decision so a silent capture is debuggable."""
        deadline = time.time() + 4.0
        while time.time() < deadline:
            s = self._browser_output_sink_name()
            if s:
                _log(f"browser audio routed to sink '{s}' — recording ITS monitor "
                     f"(not necessarily the default sink)")
                return s
            time.sleep(0.4)
        s = _default_sink()
        if s:
            _log(f"no active browser stream found yet — falling back to default sink '{s}'")
        return s

    def _move_browser_streams(self) -> None:
        rc_dbg, out_dbg = _run(["pactl", "list", "short", "sink-inputs"])
        total = len([l for l in out_dbg.splitlines() if l.strip()])
        ids = self._browser_sink_inputs()
        # Throttled diagnostic: log only when the picture changes (pactl health,
        # how many sink-inputs exist, how many we matched). This removes the blind
        # spot where a silent mover looked like "no streams" vs "couldn't see them".
        sig = (rc_dbg, total, tuple(ids))
        if sig != self._dbg_sig:
            self._dbg_sig = sig
            _log(f"[mover] pactl rc={rc_dbg} total_sink_inputs={total} "
                 f"matched={len(ids)} ids={ids}")
        if ids:
            self._stream_armed = True   # a matched call stream exists → arm hangup signal
        for sid in ids:
            if sid in self._moved:
                continue
            rc, out = _run(["pactl", "move-sink-input", sid, self.sink_name])
            if rc == 0:
                self._moved.append(sid)
                _log(f"routed caller audio stream #{sid} → {self.sink_name}"
                     if self.proc_match else
                     f"routed browser audio stream #{sid} → {self.sink_name}")
            else:
                _log(f"move stream #{sid} failed: {out.strip()!r}")

    def _mover_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._move_browser_streams()
            except Exception as exc:  # noqa: BLE001
                if not self._mover_err_logged:
                    self._mover_err_logged = True
                    _log(f"[mover] ERROR: {type(exc).__name__}: {exc}")
            self._stop.wait(1.0)

    def stream_seen(self) -> bool:
        """profile mode: has a matched caller stream been observed present at least once
        (real evidence the call's decoded audio is flowing)? Used to latch
        'media-connected' for hang-up detection, which the WebRTC layer can't supply
        in OS-capture mode. False without proc_match."""
        return bool(self.proc_match and self._stream_armed)

    def lost_stream(self) -> bool:
        """Hang-up signal for proc-scoped isolate: True once a matched call stream was
        seen present and has since DISAPPEARED (the call's audio element was torn down,
        so the caller's sink-input is removed). Returns False until a stream was ever
        observed (ringback also creates a stream, so 'present' alone isn't pickup — but
        its DISAPPEARANCE after presence is a clean, WebRTC-independent end signal). No-op
        without proc_match (legacy app-name match is too broad to trust for this)."""
        if not self.proc_match or not self._started:
            return False
        if self._browser_sink_inputs():
            self._stream_armed = True
            return False
        return self._stream_armed

    # --- recorder ---------------------------------------------------------
    def _start_recorder(self, source: str) -> bool:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
            "-f", "pulse", "-i", source,
            "-ar", str(self.rate), "-ac", str(self.channels),
            "-acodec", "pcm_s16le",
            self.out_path,
        ]
        try:
            self._rec = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"could not start ffmpeg recorder: {exc}")
            return False
        time.sleep(0.6)  # let ffmpeg open the device / fail fast
        if self._rec.poll() is not None:
            err = (self._rec.stderr.read().decode("utf-8", "replace")
                   if self._rec.stderr else "")
            _log(f"ffmpeg exited immediately: {err.strip()[:200]!r}")
            self._rec = None
            return False
        return True

    # --- public API -------------------------------------------------------
    def start(self) -> bool:
        """Start capture. Returns True if the recorder is live. Best-effort + self-
        cleaning on failure."""
        reason = _tools_ok()
        if reason:
            _log(reason + " — skipping audio capture")
            return False
        if self.mode == "allsinks":
            return self._start_allsinks()
        if self.mode == "isolate":
            return self._start_isolate()
        return self._start_monitor()

    def _all_output_sinks(self) -> list[str]:
        """Every output sink's NAME (col 1 of `pactl list short sinks`)."""
        rc, out = _run(["pactl", "list", "short", "sinks"])
        names: list[str] = []
        if rc == 0:
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0].strip().isdigit():
                    names.append(parts[1].strip())
        return names

    def _start_allsinks(self) -> bool:
        """Record the monitor of EVERY output sink with a SEPARATE recorder each, then MIX
        them at stop. This is the robust capture on a multi-sink machine: the call's audio
        (ring/voice) can land on a DIFFERENT HDA sink between calls (proven live — call 1's
        voice went elsewhere than its ring; call 4's voice was on the default sink). Locking
        ONE sink (monitor mode) can miss it; recording ALL sinks can't.

        ⚠️ Deliberately ONE ffmpeg PER SINK (not a single multi-input amix): the per-sink
        layout is the exact configuration PROVEN to capture the live remote voice (call 4 —
        −21 dB of speech on the default sink while 3 other sinks recorded silence). A single
        amix process couples the inputs; the independent recorders can't let one sink's state
        stall another. Silent sinks just yield silent temp WAVs that contribute nothing to
        the final mix. No routing change, nothing to restore, operator still hears the call.
        Blind to the local mic (fine — the bot has none)."""
        sinks = self._all_output_sinks()
        if not sinks:
            _log("no output sinks found — falling back to default-sink monitor")
            return self._start_monitor()
        base = os.path.splitext(self.out_path)[0]
        for i, s in enumerate(sinks):
            tmp = f"{base}.sink{i}.wav"
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
                   "-f", "pulse", "-i", f"{s}.monitor",
                   "-ar", str(self.rate), "-ac", str(self.channels),
                   "-acodec", "pcm_s16le", tmp]
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except Exception as exc:  # noqa: BLE001
                _log(f"could not start recorder for sink '{s}': {exc}")
                continue
            self._recs.append(p)
            self._rec_tmps.append(tmp)
        time.sleep(0.6)  # let them open / fail fast
        # Drop any that died immediately.
        alive = []
        for p, tmp in zip(self._recs, self._rec_tmps):
            if p.poll() is None:
                alive.append((p, tmp))
        self._recs = [p for p, _ in alive]
        self._rec_tmps = [tmp for _, tmp in alive]
        if not self._recs:
            _log("all per-sink recorders failed to start — skipping audio capture")
            return False
        self._started = True
        _log(f"capturing call audio → {self.out_path}  "
             f"({self.rate} Hz, {self.channels}ch, s16le PCM — Gemini Live format)")
        _log(f"source: {len(self._recs)} independent sink-monitor recorder(s), mixed at "
             "stop (multi-sink-safe; you'll still hear the call)")
        return True

    def _stop_allsinks(self) -> "str | None":
        """SIGINT every per-sink recorder, wait, then mix the temp WAVs into out_path and
        clean them up. Returns out_path on success. Keeps the temps if the mix fails so no
        audio is lost."""
        for p in self._recs:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:  # noqa: BLE001
                pass
        for p in self._recs:
            try:
                p.wait(timeout=6)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        tmps = [t for t in self._rec_tmps if os.path.exists(t) and os.path.getsize(t) > 44]
        self._recs = []
        if not tmps:
            _log("no per-sink temp WAVs produced — nothing to mix")
            return None
        if len(tmps) == 1:
            try:
                os.replace(tmps[0], self.out_path)
                return self.out_path
            except Exception as exc:  # noqa: BLE001
                _log(f"could not finalize single capture: {exc}")
                return tmps[0]
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
        filt = ""
        for i, t in enumerate(tmps):
            cmd += ["-i", t]
            filt += f"[{i}]"
        filt += f"amix=inputs={len(tmps)}:normalize=0:duration=longest"
        cmd += ["-filter_complex", filt,
                "-ar", str(self.rate), "-ac", str(self.channels),
                "-acodec", "pcm_s16le", self.out_path]
        rc, out = _run(cmd, timeout=30)
        if rc != 0:
            _log(f"mix failed (temps kept): {out.strip()[:200]!r}")
            return tmps[0]  # at least hand back one real recording
        for t in tmps:  # mix OK → clean up temps
            try:
                os.remove(t)
            except Exception:  # noqa: BLE001
                pass
        return self.out_path

    def _start_monitor(self) -> bool:
        sink = self._resolve_monitor_sink()
        if not sink:
            _log("could not resolve a sink to record — skipping audio capture")
            return False
        source = f"{sink}.monitor"
        if not self._start_recorder(source):
            return False
        self._started = True
        _log(f"capturing call audio → {self.out_path}  "
             f"({self.rate} Hz, {self.channels}ch, s16le PCM — Gemini Live format)")
        _log(f"source: {source}  (sink monitor; you'll still hear the call)")
        return True

    def _start_isolate(self) -> bool:
        if not self._load_null_sink():
            return False
        if not self._start_recorder(f"{self.sink_name}.monitor"):
            self._teardown_sink()
            return False
        if self.proc_match:
            _log(f"scoping capture to the caller process tree (match: {self.proc_match!r}) "
                 "— ONLY this browser's audio, never the daily browser or other apps")
        self._move_browser_streams()
        if self.proc_match and not self._moved:
            _log("⚠️  no caller audio stream matched yet — the mover keeps retrying every "
                 "1s (the call stream appears once media starts).")
        self._mover = threading.Thread(target=self._mover_loop, daemon=True)
        self._mover.start()
        self._started = True
        _log(f"capturing call audio → {self.out_path}  "
             f"({self.rate} Hz, {self.channels}ch, s16le PCM — Gemini Live format)")
        _log(f"source: {self.sink_name}.monitor  (isolated null sink; call muted to operator)")
        return True

    def stop(self) -> "str | None":
        """Stop recording, restore any routing, finalize the WAV. Returns the output
        path if a recording was produced, else None. Safe to call more than once."""
        if not self._started:
            return None
        self._started = False
        self._stop.set()
        if self.mode == "allsinks":
            path = self._stop_allsinks()
            if path:
                _log(f"saved call audio: {path}")
            return path
        if self._mover is not None:
            self._mover.join(timeout=3)
        for sid in self._moved:  # isolate mode: move streams back
            _run(["pactl", "move-sink-input", sid, "@DEFAULT_SINK@"])
        self._moved.clear()
        path = self._stop_recorder()
        self._teardown_sink()
        if path:
            _log(f"saved call audio: {path}")
        return path

    def _stop_recorder(self) -> "str | None":
        if self._rec is None:
            return None
        try:
            self._rec.send_signal(signal.SIGINT)  # ffmpeg finalizes the WAV header
            self._rec.wait(timeout=6)
        except Exception:  # noqa: BLE001
            try:
                self._rec.kill()
            except Exception:  # noqa: BLE001
                pass
        rc = self._rec.returncode
        self._rec = None
        # ffmpeg returns 255 on SIGINT but still finalizes a valid WAV.
        return self.out_path if rc in (0, 255, -2, None) else None

    def _teardown_sink(self) -> None:
        if self._module_id is not None:
            _run(["pactl", "unload-module", self._module_id])
            self._module_id = None


class BrowserAudioTap:
    """Capture the REMOTE participants' audio by tapping the inbound WebRTC tracks
    INSIDE the browser, NOT the OS output. The page hook (meet_call_browser._WEBRTC_HOOK
    with window.__MCB_CAPTURE set) installs a MediaRecorder on the inbound audio
    track(s) and pushes base64 chunks onto window.__audioChunks; this class drains them
    out to a temp .webm, then transcodes to 16 kHz mono s16le PCM WAV — the clean,
    OS-independent capture of exactly what the bot HEARS (the human's voice in the
    call), regardless of what else the machine is playing.

    Survives a backgrounded tab: MediaRecorder runs off the main thread (the OS-monitor
    approach also survived backgrounding, but it grabbed the whole desktop mix; this
    grabs only the call's inbound audio).

    Driven by the Playwright page; call start() once, drain() periodically from the
    call loop, stop() at teardown. Best-effort: never raises into the call loop.
    """

    def __init__(self, page, out_path: str, *, rate: int = GEMINI_RATE,
                 channels: int = GEMINI_CHANNELS) -> None:
        self.page = page
        self.out_path = out_path
        self.rate = rate
        self.channels = channels
        # One in-browser MediaRecorder = one self-contained webm SEGMENT (its own header).
        # A restart bumps the generation → a NEW segment file, never appended to the old
        # one (which is what truncated the WAV to ~3s). Segments are concatenated at stop.
        self._seg_base = os.path.splitext(out_path)[0]   # <base>.seg-<key>.webm
        self._segs: "dict[str, object]" = {}             # segKey -> open file handle (ordered)
        self._bytes = 0
        self._started = False

    def _frames(self):
        try:
            return list(self.page.frames)
        except Exception:  # noqa: BLE001
            return [self.page]

    def _owner_frames(self):
        """The frame(s) whose hook owns a live recorder (__mcbCaptureOwner) — drain ONLY
        those so two frames' webm streams never interleave into one segment. Before any
        recorder starts no frame is an owner yet, so fall back to every google frame (we
        still poke __mcbStartRec on them, which is what gets the PC frame recording)."""
        google, owners = [], []
        for fr in self._frames():
            if "google.com" not in (fr.url or ""):
                continue
            google.append(fr)
            try:
                if fr.evaluate("!!window.__mcbCaptureOwner"):
                    owners.append(fr)
            except Exception:  # noqa: BLE001
                pass
        return owners or google

    def start(self) -> bool:
        if not shutil.which("ffmpeg"):
            _log("missing 'ffmpeg' — skipping audio capture")
            return False
        self._started = True
        _log(f"capturing call audio (WebRTC INBOUND tap → the remote voice) → "
             f"{self.out_path}")
        _log(f"  ({self.rate} Hz, {self.channels}ch, s16le PCM — Gemini Live format)")
        return True

    _DEBUG_JS = (
        "(() => ({cap: !!window.__MCB_CAPTURE, "
        "rt: window.__remoteTracks||0, owner: !!window.__mcbCaptureOwner, gen: (window.__mcbGen||0), "
        "live: (window.__mcbLiveAudioTracks ? window.__mcbLiveAudioTracks().length : 0), "
        "rec: !!window.__mcbRecorder, st: (window.__mcbRecorder?window.__mcbRecorder.state:null), "
        "ch: (window.__audioChunks||[]).length, diag: (window.__mcbDiag||null), "
        "inv: (window.__mcbInventory ? window.__mcbInventory() : null), "
        "err: (window.__mcbErr||null)}))()")

    # Idempotent in-page recorder (re)starter — the hook exposes it; we poke it every
    # drain so a recorder that went 'inactive' (the raw-track-stops bug) is restarted.
    _ENSURE_JS = ("(() => { try { if (window.__mcbStartRec) window.__mcbStartRec(); } "
                  "catch(e){} })()")

    def debug_state(self):
        """Per-frame snapshot of the in-page capture globals — pinpoints where the
        tap breaks (capture flag unset in the PC's frame? recorder never created?
        chunks not accumulating?). Best-effort; returns a list of (url, state)."""
        rows = []
        for fr in self._frames():
            u = fr.url or ""
            if "google.com" not in u:
                continue
            try:
                st = fr.evaluate(self._DEBUG_JS)
            except Exception as exc:  # noqa: BLE001
                st = {"evalErr": str(exc)[:80]}
            rows.append((u[:64], st))
        return rows

    _DRAIN_JS = ("(() => { const c = window.__audioChunks || []; "
                 "window.__audioChunks = []; return c; })()")

    def drain(self) -> None:
        """Pull any pending tagged audio chunks from the page → per-segment .webm files.
        Also re-pokes the in-page recorder so it's restarted if it went inactive (a new
        recorder lands in a NEW segment, so the restart never corrupts the prior one)."""
        if not self._started:
            return
        for fr in self._owner_frames():
            try:
                fr.evaluate(self._ENSURE_JS)  # restart recorder if inactive
            except Exception:  # noqa: BLE001
                pass
            try:
                chunks = fr.evaluate(self._DRAIN_JS)
            except Exception:  # noqa: BLE001 - frame may be navigating
                continue
            for tagged in (chunks or []):
                self._write_chunk(tagged)

    def _write_chunk(self, tagged: str) -> None:
        """Route one "<frameId>:<gen>|<base64>" chunk to its segment file (base64 has no
        '|'/':' so the split is unambiguous; an untagged legacy chunk → segment '0')."""
        bar = tagged.find("|")
        if bar > 0 and ":" in tagged[:bar]:
            key, b64 = tagged[:bar], tagged[bar + 1:]
        else:
            key, b64 = "0", tagged
        try:
            data = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return
        fh = self._segs.get(key)
        if fh is None:
            try:
                fh = open(f"{self._seg_base}.seg-{key}.webm", "wb")
            except Exception as exc:  # noqa: BLE001
                _log(f"webrtc tap: cannot open segment file: {exc}")
                return
            self._segs[key] = fh
        fh.write(data)
        self._bytes += len(data)

    def stop(self) -> "str | None":
        """Flush the recorder's tail, drain, transcode each segment and concatenate into
        one WAV. Returns the WAV path or None if nothing was captured. Safe to call once."""
        if not self._started:
            return None
        self._started = False
        # Ask the in-page recorder to flush its final chunk.
        for fr in self._frames():
            try:
                if "google.com" not in (fr.url or ""):
                    continue
                fr.evaluate("(() => { try { if (window.__mcbRecorder && "
                            "window.__mcbRecorder.state !== 'inactive') "
                            "window.__mcbRecorder.stop(); } catch(e){} })()")
            except Exception:  # noqa: BLE001
                pass
        try:
            self.page.wait_for_timeout(900)  # let the final dataavailable fire
        except Exception:  # noqa: BLE001
            time.sleep(0.9)
        self.drain()
        seg_paths = [f"{self._seg_base}.seg-{k}.webm" for k in self._segs]
        for fh in self._segs.values():
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
        if self._bytes <= 0 or not seg_paths:
            _log("webrtc tap: no inbound audio captured (no remote audio track, or "
                 "nobody spoke). WAV not written.")
            return None
        return self._finalize(seg_paths)

    def _finalize(self, seg_paths: "list[str]") -> "str | None":
        """Decode each standalone webm segment → 16k mono s16le WAV, then concat the WAVs
        (same params → lossless stream copy). One segment is the common case; >1 means the
        recorder restarted mid-call and we stitch the pieces instead of losing all but the
        first. Segments that don't decode (e.g. a headerless fragment) are skipped, not
        fatal. Returns the final WAV path or None."""
        wavs: "list[str]" = []
        for i, sp in enumerate(seg_paths):
            if not (os.path.exists(sp) and os.path.getsize(sp) > 0):
                continue
            tw = f"{self._seg_base}.seg{i}.wav"
            rc, out = _run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", sp,
                "-ar", str(self.rate), "-ac", str(self.channels),
                "-acodec", "pcm_s16le", tw,
            ], timeout=60)
            if rc == 0 and os.path.exists(tw) and os.path.getsize(tw) > 44:
                wavs.append(tw)
            else:
                _log(f"webrtc tap: segment {os.path.basename(sp)} did not decode "
                     f"(skipped): {out.strip()[:120]!r}")
        if not wavs:
            _log("webrtc tap: no segment decoded — WAV not written "
                 f"(raw segments kept: {', '.join(os.path.basename(s) for s in seg_paths)}).")
            return None
        if len(wavs) == 1:
            try:
                os.replace(wavs[0], self.out_path)
            except Exception:  # noqa: BLE001
                shutil.copyfile(wavs[0], self.out_path)
        else:
            listfile = f"{self._seg_base}.concat.txt"
            try:
                with open(listfile, "w") as f:
                    for w in wavs:
                        f.write(f"file '{os.path.abspath(w)}'\n")
            except Exception as exc:  # noqa: BLE001
                _log(f"webrtc tap: cannot write concat list: {exc}")
                return None
            rc, out = _run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", listfile,
                "-c", "copy", self.out_path,
            ], timeout=60)
            if rc != 0 or not os.path.exists(self.out_path):
                _log(f"webrtc tap: concat failed: {out.strip()[:160]!r}")
                return None
            for w in wavs:  # best-effort cleanup of per-segment WAVs (raw webm kept)
                try:
                    os.remove(w)
                except Exception:  # noqa: BLE001
                    pass
        _log(f"saved call audio: {self.out_path}  "
             f"({len(wavs)} segment(s), {self._bytes} bytes raw)")
        return self.out_path


def _measure_dbfs(path: str) -> "float | None":
    """Mean volume (dBFS) of a WAV via ffmpeg volumedetect, or None."""
    rc, out = _run(["ffmpeg", "-hide_banner", "-i", path,
                    "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", out)
    return float(m.group(1)) if m else None


def _selftest() -> int:
    """Prove the capture machinery works WITHOUT a call: record 2s of the default
    sink monitor → a valid, non-empty WAV (likely silent if nothing's playing)."""
    out = "/tmp/meet_audio_selftest.wav"
    cap = AudioCapture(out)
    if not cap.start():
        print("SELFTEST FAIL: capture did not start", file=sys.stderr)
        return 1
    time.sleep(2)
    path = cap.stop()
    size = os.path.getsize(path) if path and os.path.exists(path) else 0
    print(f"SELFTEST {'OK' if size > 44 else 'FAIL'}: {path} ({size} bytes)")
    return 0 if size > 44 else 1


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="meet_audio_capture")
    ap.add_argument("--out", default="reports/meet_audio.wav",
                    help="output WAV path (default reports/meet_audio.wav).")
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="standalone capture window length (default 20s).")
    ap.add_argument("--isolate", action="store_true",
                    help="use the null-sink isolation mode instead of the default-sink "
                    "monitor (cleaner but fragile — see module docstring).")
    ap.add_argument("--selftest", action="store_true",
                    help="2s tooling smoke test (no call); writes /tmp/meet_audio_selftest.wav.")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    cap = AudioCapture(args.out, mode="isolate" if args.isolate else "monitor")
    if not cap.start():
        return 1
    print(f"Recording for {args.seconds:.0f}s — play/join the call now. Ctrl+C to stop early.")
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        print("\n   stopping …")
    path = cap.stop()
    if path:
        db = _measure_dbfs(path)
        print(f"saved {path}" + (f"  (mean {db:.1f} dBFS)" if db is not None else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
