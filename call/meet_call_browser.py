#!/usr/bin/env python3
"""Place the *native ringing* Google Chat call by driving a real logged-in browser
with Playwright — the "trick" path, because no public API can ring someone (only a
human clicking the call button in the Chat UI produces the ring; see the project
notes / docs/google_meet/).

What it does
------------
1. Opens a real Chromium-family browser (your system **Brave**) signed into Google,
   reusing a PERSISTENT profile so the login survives across runs and never fights
   your daily browser.
2. Navigates to a Google Chat DM/space.
3. Finds and clicks the **call** button → that fires Meet's internal
   ``MeetingSpaceService/ResolveForHangoutsChat`` RPC and RINGS the other person,
   exactly like clicking it by hand (Chrome generates the protobuf + SAPISIDHASH
   auth itself — far more robust than replaying the raw RPC).
4. Stays in the call, but EXITS THE MOMENT the call ends — when the callee hangs
   up (or declines / never answers) the call UI's leave control disappears / an
   end banner shows, and the script detects that and stops. ``--duration`` is a
   MAX cap, not a fixed wait (``--keep-open`` removes the cap → until Ctrl+C).

This is the browser/ring half. The AI **voice** half (capture the Meet's audio +
inject TTS so the AI talks on the call) is NOT here — that's virtual PulseAudio
devices + the Gemini Live loop from ``call/demo_incident_call.py`` pointed at
those devices. See the "AUDIO (next phase)" note at the bottom. Reason: audio is
WebRTC; Playwright drives the page, not the media — but this script is the WebRTC
client that must be *in* the call for that audio to flow, so it's the foundation.

⚠️  Reality check (do not be misled): this automates the Google web UI, which
violates Google's ToS and can get an account flagged. Use only the throwaway demo
accounts. Selectors into Google's obfuscated DOM are brittle by nature — if the
call button isn't found, the script DUMPS every visible button label so you can
pass the exact one via ``--button-name``.

First-time setup
----------------
  conda run -n igaming pip install playwright      # the system Brave is reused,
                                                   # so NO `playwright install` needed

First run opens an EMPTY profile → log into Google (the demo account) in the
window that appears, open the target DM once, then re-run. The cookies persist in
``--profile-dir`` so later runs are already signed in.

Run
---
  # 1) sign in once (headed), confirm the DM loads, find the button:
  python call/meet_call_browser.py --dry-run

  # 2) place the real ringing call (default target = GOOGLE_CHAT_REPORT_SPACE = the
  #    bot↔Duc DM), ring for 90s:
  python call/meet_call_browser.py --duration 90

  # most reliable targeting: open the DM in the window, copy the address-bar URL,
  # and pass it verbatim:
  python call/meet_call_browser.py --url 'https://chat.google.com/u/0/...'

  # reuse your DAILY Brave session instead of a dedicated profile (quit Brave, then
  # `brave-browser --remote-debugging-port=9222`, then):
  python call/meet_call_browser.py --cdp-url http://127.0.0.1:9222

Exit codes: 0 ok · 2 setup error (Playwright missing / no target) · 1 runtime
error (button not found / navigation failed).
"""
from __future__ import annotations

# Back-compat facade: meet_call_browser was a single 2.5k-line script; it is now
# split into cohesive sibling modules. These re-exports keep the public surface
# other scripts import (meet_call_browser.main / ._WEBRTC_HOOK / call-button +
# hang-up probe helpers used by call_network_capture) working unchanged.
from meet_call_run import main
from meet_call_js import _WEBRTC_HOOK
from meet_call_signals import (
    _IN_CALL_PATTERNS,
    _find_call_button,
    _dump_buttons,
    _find_button_in_frames,
    _in_call,
    _alone_signal,
)

__all__ = [
    '_IN_CALL_PATTERNS',
    '_WEBRTC_HOOK',
    '_alone_signal',
    '_dump_buttons',
    '_find_button_in_frames',
    '_find_call_button',
    '_in_call',
    'main',
]


# AUDIO (next phase) — make the AI actually TALK on this call
# -----------------------------------------------------------
# Playwright drives the PAGE; the call's audio is WebRTC, routed by the OS. To put
# an AI voice on the call once this browser is IN it:
#   1. Create virtual PulseAudio/PipeWire devices:
#        pactl load-module module-null-sink sink_name=ai_speaker      # AI hears Meet
#        pactl load-module module-null-sink sink_name=ai_mic_sink     # AI speaks in
#        pactl load-module module-remap-source source_name=ai_mic master=ai_mic_sink.monitor
#   2. Launch this browser pinned to them (per-app routing via pavucontrol, or run
#      under a PULSE_SINK/PULSE_SOURCE env so Brave uses ai_mic as its microphone
#      and ai_speaker as its output).
#   3. Run the Gemini Live loop from call/demo_incident_call.py with pyaudio
#      input = ai_speaker.monitor (Meet audio in) and output = ai_mic_sink (AI voice
#      out). The existing loop already does 16 kHz in / 24 kHz out bidirectional.
# That keeps the ring (this script) and the brain (Gemini Live) decoupled.
if __name__ == "__main__":
    raise SystemExit(main())
