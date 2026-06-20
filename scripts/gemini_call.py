#!/usr/bin/env python3
"""gemini_call.py — let GEMINI LIVE talk to a human on a real Google Chat call.

Gemini is the CALLER, you are the callee (on your phone). This is the bidirectional
follow-on to ``ai_call.py`` (which only injected a static tone): instead of a file,
Gemini Live drives the call's microphone AND listens to your voice, so you have a real
two-way conversation with the model over the call.

It is a thin orchestrator that composes three proven pieces:

  1. ``ai_call`` launches (or reuses) the DEDICATED, mic-PRE-GRANTED caller Brave
     (its own profile + CDP port, ``--use-fake-ui-for-media-stream``), and gates on the
     Google login.
  2. ``gemini_voice.GeminiVoiceBridge`` sets up two virtual PulseAudio devices and runs
     the Gemini Live session in a worker thread: Gemini's voice → the call mic (the
     callee hears it), the callee's voice → Gemini's ear. (See that module's header for
     the audio topology.)
  3. ``meet_call_browser`` (over CDP, ``--watch-join --ensure-mic-on``) places the
     ringing call, unmutes the bot mic on answer, and detects hang-up — WITHOUT its own
     audio injector, because the bridge owns all the audio.

Threading: the bridge's Gemini session is async and runs in a worker thread (its own
event loop); the main thread drives the browser (sync Playwright) and blocks until the
call ends, then stops the bridge and restores the audio devices.

  conda run --no-capture-output -n igaming python -u scripts/gemini_call.py
  # first run only: sign the dedicated profile in (ai_call.py prints how)

Requires GEMINI_API_KEY (env or .env) and `pip install google-genai` (already in the
igaming env). ⚠️  Automates the Google UI (ToS / account-flag risk) — demo accounts
only. Keep the caller window VISIBLE (native Wayland suspends an occluded renderer →
the call drops).
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

import ai_call               # noqa: E402  (browser launch / login / teardown helpers)
import gemini_voice          # noqa: E402  (the Gemini Live ⇄ call audio bridge)
import meet_call_browser     # noqa: E402  (the proven ring + join + hang-up engine)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _reporter_name(role: str) -> str:
    """Pull the persona's first name out of the 'You are <Name>, ...' role string."""
    head = role.strip()
    if head.lower().startswith("you are "):
        head = head[len("you are "):]
    return head.split(",", 1)[0].strip() or "kỹ sư on-call"


def build_incident_persona(persona_id: str, callee_name: str) -> "tuple[str, str]":
    """Load a scenarios.json persona (e.g. apigw = the API-gateway 504 incident) and turn
    it into (system_instruction, opening_trigger) for a LIVE VOICE incident report IN
    VIETNAMESE. The AI is a NEUTRAL INTERMEDIARY ("trợ lý trực sự cố") that *relays* an
    incident raised by the on-call engineer ({owner}) — it is NOT that engineer and does
    NOT own the incident. On pickup it announces the incident on the owner's behalf, then
    answers strictly from the report's facts. The report is the hard ceiling: it never
    invents specifics and explicitly says it doesn't know (will check back) when asked
    anything not in the report. When asked who is responsible, it names {owner}, never
    itself."""
    from gchat_agent.agent.staff import load_personas  # src is on sys.path (top of file)
    personas = load_personas(os.path.join(_REPO_ROOT, "data", "scenarios.json"))
    if persona_id not in personas:
        have = ", ".join(sorted(personas)) or "(none)"
        raise SystemExit(f"persona {persona_id!r} not in scenarios.json (have: {have})")
    p = personas[persona_id]
    owner = _reporter_name(p.get("role", ""))  # the engineer who owns the incident (e.g. Dave)
    facts = p.get("facts", {}) or {}
    seeds = " ".join(s.strip() for s in (p.get("seed_messages") or [])).strip()
    fact_lines = "\n".join(f"- {k}: {v}" for k, v in facts.items())

    system = f"""Bạn là TRỢ LÝ TRỰC SỰ CỐ (một AI) của bộ phận kỹ thuật tại một nhà vận hành iGaming. Bạn đang GỌI ĐIỆN cho {callee_name} (trưởng nhóm kỹ thuật) để THÔNG BÁO HỘ một sự cố production đang diễn ra, do kỹ sư {owner} báo lên. Bạn là NGƯỜI TRUNG GIAN truyền đạt thông tin — KHÔNG phải người trực tiếp xử lý, KHÔNG phải người chịu trách nhiệm sự cố. Đây là cuộc gọi thoại trực tiếp, không phải chat.

Vai trò & nguyên tắc BẮT BUỘC:
- Bạn CHỈ truyền đạt lại thông tin có trong "Báo cáo sự cố" bên dưới. Bạn KHÔNG sở hữu và KHÔNG xử lý sự cố này.
- Khi được hỏi "ai chịu trách nhiệm / ai đang xử lý / ai own", trả lời rõ đó là {owner} (on-call team Platform tuần này). TUYỆT ĐỐI không nhận trách nhiệm về mình — bạn chỉ là người báo tin hộ.
- Xưng hô: tự gọi mình là "em" (trợ lý trực sự cố); nhắc tới {owner} ở NGÔI THỨ BA ("anh {owner}", "bạn ấy"). Báo cáo bên dưới có thể viết theo lời {owner} (ngôi thứ nhất "tôi/em") — khi đọc lại hãy chuyển sang ngôi thứ ba, ĐỪNG nói như thể bạn chính là {owner}.
- CHỈ DÙNG các sự kiện trong báo cáo. Nếu bị hỏi điều KHÔNG có trong báo cáo, nói thẳng: "thông tin đó không có trong báo cáo em đang nắm, để em hỏi lại anh {owner}/team rồi báo anh sau." TUYỆT ĐỐI KHÔNG đoán bừa, KHÔNG bịa số liệu, tên người, mốc thời gian hay nguyên nhân.

Cách hành xử trên cuộc gọi:
- MỞ ĐẦU NGAY khi nghe máy: chào {callee_name}, giới thiệu mình là trợ lý trực sự cố đang gọi để báo hộ một sự cố mà anh {owner} vừa báo lên, rồi tóm tắt gọn trong 2-3 câu: cái gì đang hỏng, ảnh hưởng tới người chơi ra sao, và ai đang xử lý. Giọng khẩn trương, rõ ràng — đừng đọc như kịch bản.
- Sau đó trả lời câu hỏi của {callee_name} trực tiếp, ngắn gọn (1-2 câu mỗi lần), bám đúng báo cáo.
- LUÔN nói tiếng Việt tự nhiên cho giọng nói. Giữ nguyên thuật ngữ kỹ thuật (API gateway, 504, p99, INFRA-2207...). Không đọc markdown/bullet.
- Khi {callee_name} ra hiệu kết thúc (vd "cảm ơn", "vậy là đủ", "ok giữ liên lạc"), chào tạm biệt ngắn gọn rồi dừng nói.

Báo cáo sự cố (toàn bộ thông tin bạn được phép truyền đạt — KHÔNG có gì ngoài đây):
- Người báo & chịu trách nhiệm sự cố: {owner} (on-call team Platform tuần này)
- Tình huống: {seeds}
{fact_lines}
"""
    opening = (
        f"(Cuộc gọi vừa kết nối — bạn nghe thấy {callee_name} nhấc máy. Hãy BẮT ĐẦU NGAY: "
        f"chào {callee_name}, giới thiệu bạn là trợ lý trực sự cố đang gọi báo hộ một sự cố "
        f"production do anh {owner} báo lên, rồi tóm tắt sự cố bằng tiếng Việt.)"
    )
    return system, opening


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gemini_call",
        description="Place a ringing call and let Gemini Live converse with the callee.")
    ap.add_argument("--duration", type=float, default=180.0,
                    help="MAX seconds to hold the call (exits early on hang-up; default 180).")
    ap.add_argument("--url", default=ai_call._DEFAULT_URL,
                    help="exact Chat DM URL to call into (default: the bot↔Duc DM, u/0).")
    ap.add_argument("--port", type=int, default=ai_call._DEFAULT_PORT,
                    help=f"CDP/debug port for the caller Brave (default {ai_call._DEFAULT_PORT}).")
    ap.add_argument("--profile", default=ai_call._DEFAULT_PROFILE,
                    help="dedicated caller profile dir (holds the Google login).")
    ap.add_argument("--login-wait", type=float, default=180.0,
                    help="seconds to wait for a manual sign-in if not signed in (default 180).")
    ap.add_argument("--model", default=gemini_voice.DEFAULT_MODEL,
                    help=f"Gemini Live model (default {gemini_voice.DEFAULT_MODEL}).")
    ap.add_argument("--voice", default=gemini_voice.DEFAULT_VOICE,
                    help=f"prebuilt voice name (default {gemini_voice.DEFAULT_VOICE}).")
    ap.add_argument("--persona", default=None,
                    help="report an incident from data/scenarios.json on pickup, e.g. "
                         "'apigw' (the API-gateway 504 incident). Overrides the default "
                         "greeting with a spoken incident briefing in Vietnamese.")
    ap.add_argument("--callee", default="Duc",
                    help="callee's name the reporter addresses on the call (default Duc).")
    ap.add_argument("--system", default=None,
                    help="system instruction (persona). Default: a Vietnamese AI caller.")
    ap.add_argument("--system-file", default=None,
                    help="read the system instruction from this file (overrides --system).")
    ap.add_argument("--language", default=None,
                    help="optional BCP-47 speech language_code (e.g. vi-VN).")
    ap.add_argument("--no-greet", action="store_true",
                    help="don't make Gemini speak first; wait for the callee (reactive).")
    ap.add_argument("--no-record", action="store_true",
                    help="don't record the call audio to logs/ (recording is on by default "
                         "for debugging — both directions to WAV next to the debug log).")
    ap.add_argument("--quit-browser", action="store_true",
                    help="stop the caller Brave on exit. Default: leave it running so the "
                         "login persists and the next call is instant.")
    ap.add_argument("--diag-pickup", action="store_true",
                    help="log a per-poll pickup-signal snapshot while ringing (diagnoses "
                         "greeting latency — shows which real-answer signal lags + the "
                         "effective poll cadence). Passed through to meet_call_browser.")
    a = ap.parse_args(argv)

    key = gemini_voice.load_gemini_key(_REPO_ROOT)
    if not key:
        _log("ERROR: no GEMINI_API_KEY found (checked env + .env).")
        _log("  set GEMINI_API_KEY in .env or the environment, then re-run.")
        return 2

    # Persona/system + the opening the AI says first on pickup. --persona turns the call
    # into an incident REPORT (AI briefs the callee on the incident), otherwise it's the
    # default friendly-assistant greeting. Precedence: --system-file > --system > persona.
    greet_text = None
    if a.persona:
        system, greet_text = build_incident_persona(a.persona, a.callee)
        _log(f"  incident-report mode: persona={a.persona!r} → reporting to {a.callee}")
    else:
        system = a.system or gemini_voice.DEFAULT_SYSTEM
    if a.system_file:
        try:
            with open(a.system_file, encoding="utf-8") as f:
                system = f.read().strip()
        except OSError as exc:
            _log(f"ERROR: could not read --system-file: {exc}")
            return 2

    profile = os.path.abspath(a.profile)
    if not os.path.isdir(profile):
        _log(f"ERROR: caller profile not found: {profile}")
        _log("  ONE-TIME setup — sign it in (plain browser, NO automation):")
        _log(f'    brave-browser --user-data-dir="{profile}"')
        _log("  → sign in as the demo account ONLY, open the DM once, close it, re-run.")
        return 2

    _log("=== gemini_call → Gemini Live converses with the callee on a real call ===")

    # 1) Bring up (or reuse) the pre-granted caller browser + confirm the login.
    launched = None
    if ai_call._cdp_up(a.port):
        _log(f"  reusing caller Brave already on http://127.0.0.1:{a.port}")
    else:
        launched = ai_call._launch_caller_brave(a.port, profile)
        if not ai_call._cdp_up(a.port):
            _log("ERROR: caller Brave is not reachable over CDP — aborting.")
            if launched and a.quit_browser:
                ai_call._kill_profile_braves(profile)
            return 2
    if not ai_call._ensure_logged_in(a.port, a.url, wait_s=a.login_wait):
        _log("  (leaving the browser open so you can finish signing in.)")
        return 2

    # 2) Audio devices up BEFORE the call, so the browser grabs the virtual mic/speaker.
    bridge = gemini_voice.GeminiVoiceBridge(
        api_key=key, model=a.model, voice=a.voice, system=system,
        language=a.language, greet=not a.no_greet, greet_text=greet_text,
        record=not a.no_record)
    if not bridge.setup_devices():
        _log("ERROR: could not set up the virtual audio devices — aborting.")
        if launched and a.quit_browser:
            ai_call._kill_profile_braves(profile)
        return 2

    # 3) Start the Gemini Live bridge in a worker thread (its own asyncio loop).
    def _worker() -> None:
        import asyncio
        try:
            asyncio.run(bridge.run())
        except Exception as exc:  # noqa: BLE001
            _log(f"  ⚠️  Gemini bridge thread error: {type(exc).__name__}: {exc}")

    worker = threading.Thread(target=_worker, name="gemini-bridge", daemon=True)
    worker.start()

    # 4) Place + hold the call (main thread, blocking until hang-up / cap). The bridge
    #    owns the audio, so no --inject-audio — just unmute the bot mic on answer.
    mcb_argv = [
        "--cdp-url", f"http://127.0.0.1:{a.port}",
        "--url", a.url,
        "--watch-join",
        "--ensure-mic-on",
        "--duration", str(a.duration),
    ]
    if a.diag_pickup:
        mcb_argv.append("--diag-pickup")
    _log("  handing off to meet_call_browser (ring + join + hang-up) …\n")
    rc = 1
    try:
        # on_pickup (callee TRULY answered — ringback-safe) → greet first, then move the
        # call playback onto Gemini's ear + open it. on_join is the early (maybe-ringback)
        # signal; the bridge keeps it side-effect-free so the greeting never hits the ring.
        rc = meet_call_browser.main(
            mcb_argv, on_join=bridge.on_join, on_pickup=bridge.on_pickup)
    except KeyboardInterrupt:
        _log("\n  interrupted — ending the call and the Gemini session.")
    finally:
        bridge.signal_stop()
        worker.join(timeout=10)
        bridge.teardown_devices()
        if launched and a.quit_browser:
            ai_call._kill_profile_braves(profile)
        elif launched:
            _log(f"\n  caller Brave left running on :{a.port} "
                 "(login persists; --quit-browser to stop it).")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
