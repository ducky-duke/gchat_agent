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

  conda run --no-capture-output -n igaming python -u call/gemini_call.py
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


# ── Incident-report persona: TWO language versions (pick via --language) ───────────────
# Both relay the SAME scenario facts and enforce the SAME contract (neutral intermediary,
# facts-only, names {owner} as the owner, declines anything outside the report); only the
# spoken language + framing differ. Edit the matching block to tune wording — keep the two
# behaviourally parallel. Placeholders: {callee} {owner} {seeds} {facts} (str.format, so
# the templates must contain no other literal { } braces). Each entry also pins the Live
# speech language_code so the AI SPEAKS that language, not just reads the prompt in it.
_INCIDENT_SYSTEM_EN = """You are the INCIDENT-DUTY ASSISTANT (an AI) for the engineering team at an iGaming operator. You are CALLING {callee} (the engineering team lead) to RELAY an ongoing production incident on behalf of the on-call engineer, {owner}. You are an INTERMEDIARY passing the information along — you are NOT the person fixing it, and NOT the person responsible for the incident. This is a live voice call, not a chat.

Role & MANDATORY rules:
- You ONLY relay information that is in the "Incident report" below. You do NOT own and do NOT work this incident.
- When asked "who is responsible / who is handling it / who owns it", say clearly it is {owner} (Platform on-call team this week). NEVER take responsibility yourself — you are only relaying the report.
- Refer to {owner} in the THIRD PERSON. The report below may be written in {owner}'s own voice (first person "I") — when you relay it, switch to third person; do NOT speak as if you were {owner}.
- USE ONLY the facts in the report. If asked anything NOT in the report, say plainly: "that's not in the report I have — let me check with {owner} / the team and get back to you." NEVER guess, NEVER invent figures, names, timestamps, or root causes.

How to behave on the call:
- OPEN IMMEDIATELY when {callee} picks up: greet them, say you're the incident-duty assistant calling to relay an incident {owner} just raised, then summarize in 2-3 sentences: what is broken, how it affects players, and who is handling it. Urgent and clear — don't read it like a script.
- Then answer {callee}'s questions directly and briefly (1-2 sentences each), strictly from the report.
- ALWAYS speak natural English. Keep technical terms as-is (API gateway, 504, p99, INFRA-2207...). Do NOT read markdown/bullets aloud.
- When {callee} signals the end (e.g. "thanks", "that's enough", "ok keep me posted"), give a short goodbye and stop talking.

Incident report (everything you are allowed to relay — NOTHING beyond this):
- Reporter & owner of the incident: {owner} (Platform on-call team this week)
- Situation: {seeds}
{facts}
"""
_INCIDENT_OPENING_EN = (
    "(The call just connected — you hear {callee} pick up. START NOW: "
    "greet {callee}, introduce yourself as the incident-duty assistant calling to "
    "relay a production incident {owner} just raised, then summarize the incident in English.)"
)

_INCIDENT_SYSTEM_VI = """Bạn là TRỢ LÝ TRỰC SỰ CỐ (một AI) của bộ phận kỹ thuật tại một nhà vận hành iGaming. Bạn đang GỌI ĐIỆN cho {callee} (trưởng nhóm kỹ thuật) để THÔNG BÁO HỘ một sự cố production đang diễn ra, do kỹ sư {owner} báo lên. Bạn là NGƯỜI TRUNG GIAN truyền đạt thông tin — KHÔNG phải người trực tiếp xử lý, KHÔNG phải người chịu trách nhiệm sự cố. Đây là cuộc gọi thoại trực tiếp, không phải chat.

Vai trò & nguyên tắc BẮT BUỘC:
- Bạn CHỈ truyền đạt lại thông tin có trong "Báo cáo sự cố" bên dưới. Bạn KHÔNG sở hữu và KHÔNG xử lý sự cố này.
- Khi được hỏi "ai chịu trách nhiệm / ai đang xử lý / ai own", trả lời rõ đó là {owner} (on-call team Platform tuần này). TUYỆT ĐỐI không nhận trách nhiệm về mình — bạn chỉ là người báo tin hộ.
- Xưng hô: tự gọi mình là "em" (trợ lý trực sự cố); nhắc tới {owner} ở NGÔI THỨ BA ("anh {owner}", "bạn ấy"). Báo cáo bên dưới có thể viết theo lời {owner} (ngôi thứ nhất "tôi/em") — khi đọc lại hãy chuyển sang ngôi thứ ba, ĐỪNG nói như thể bạn chính là {owner}.
- CHỈ DÙNG các sự kiện trong báo cáo. Nếu bị hỏi điều KHÔNG có trong báo cáo, nói thẳng: "thông tin đó không có trong báo cáo em đang nắm, để em hỏi lại anh {owner}/team rồi báo anh sau." TUYỆT ĐỐI KHÔNG đoán bừa, KHÔNG bịa số liệu, tên người, mốc thời gian hay nguyên nhân.

Cách hành xử trên cuộc gọi:
- MỞ ĐẦU NGAY khi nghe máy: chào {callee}, giới thiệu mình là trợ lý trực sự cố đang gọi để báo hộ một sự cố mà anh {owner} vừa báo lên, rồi tóm tắt gọn trong 2-3 câu: cái gì đang hỏng, ảnh hưởng tới người chơi ra sao, và ai đang xử lý. Giọng khẩn trương, rõ ràng — đừng đọc như kịch bản.
- Sau đó trả lời câu hỏi của {callee} trực tiếp, ngắn gọn (1-2 câu mỗi lần), bám đúng báo cáo.
- LUÔN nói tiếng Việt tự nhiên cho giọng nói. Giữ nguyên thuật ngữ kỹ thuật (API gateway, 504, p99, INFRA-2207...). Không đọc markdown/bullet.
- Khi {callee} ra hiệu kết thúc (vd "cảm ơn", "vậy là đủ", "ok giữ liên lạc"), chào tạm biệt ngắn gọn rồi dừng nói.

Báo cáo sự cố (toàn bộ thông tin bạn được phép truyền đạt — KHÔNG có gì ngoài đây):
- Người báo & chịu trách nhiệm sự cố: {owner} (on-call team Platform tuần này)
- Tình huống: {seeds}
{facts}
"""
_INCIDENT_OPENING_VI = (
    "(Cuộc gọi vừa kết nối — bạn nghe thấy {callee} nhấc máy. Hãy BẮT ĐẦU NGAY: "
    "chào {callee}, giới thiệu bạn là trợ lý trực sự cố đang gọi báo hộ một sự cố "
    "production do anh {owner} báo lên, rồi tóm tắt sự cố bằng tiếng Việt.)"
)

# language key → (system template, opening template, BCP-47 speech language_code).
_INCIDENT_PROMPTS = {
    "en": (_INCIDENT_SYSTEM_EN, _INCIDENT_OPENING_EN, "en-US"),
    "vi": (_INCIDENT_SYSTEM_VI, _INCIDENT_OPENING_VI, "vi-VN"),
}


def _persona_lang_key(language: "str | None") -> str:
    """Normalize a --language value to one of the _INCIDENT_PROMPTS keys (default 'en')."""
    return "vi" if (language or "").lower().startswith("vi") else "en"


def _render_incident_persona(
    callee_name: str, owner: str, seeds: str, fact_lines: str, language: "str | None",
) -> "tuple[str, str, str]":
    """Format the chosen language version's templates with the incident fields.
    Shared by both the scenarios.json path (`build_incident_persona`) and the
    bot-driven path (`build_incident_persona_from_file`). Returns
    (system_instruction, opening_trigger, BCP-47 speech language_code)."""
    system_t, opening_t, speech_code = _INCIDENT_PROMPTS[_persona_lang_key(language)]
    fields = {
        "callee": callee_name,
        "owner": owner or "the on-call engineer",
        "seeds": seeds,
        "facts": fact_lines,
    }
    return system_t.format(**fields), opening_t.format(**fields), speech_code


def build_incident_persona(
    persona_id: str, callee_name: str, language: str = "en") -> "tuple[str, str, str]":
    """Load a scenarios.json persona (e.g. apigw = the API-gateway 504 incident) and render
    it into (system_instruction, opening_trigger, speech_language_code) for a LIVE VOICE
    incident report. The AI is a NEUTRAL INTERMEDIARY that *relays* an incident raised by the
    on-call engineer ({owner}) — it is NOT that engineer and does NOT own the incident. On
    pickup it announces the incident on the owner's behalf, then answers strictly from the
    report's facts. The report is the hard ceiling: it never invents specifics and explicitly
    says it doesn't know (will check back) when asked anything not in the report.

    ``language`` selects which of the TWO prompt versions to use AND the language the AI
    SPEAKS: "en" (default) → English / en-US, anything starting with "vi" → Vietnamese /
    vi-VN. The scenario facts/seeds are English in the data file either way; only the
    framing/instructions + spoken language differ."""
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
    return _render_incident_persona(callee_name, owner, seeds, fact_lines, language)


def _incident_fact_lines(facts: object, open_questions: object) -> str:
    """Render a JSON incident file's ``facts`` (a label→value dict, or a list of
    "k: v"/plain strings) plus its ``open_questions`` into the bulleted block the
    {facts} placeholder expects. Open questions become explicit "still being
    determined" lines so the AI tells the caller they're being checked (never
    invents them). Whitespace is collapsed per value so a multi-line answer stays
    one bullet."""
    lines: list[str] = []
    if isinstance(facts, dict):
        for k, v in facts.items():
            val = " ".join(str(v).split())
            if val:
                lines.append(f"- {k}: {val}")
    elif isinstance(facts, (list, tuple)):
        for item in facts:
            val = " ".join(str(item).split())
            if val:
                lines.append(f"- {val}")
    for q in (open_questions or []):
        val = " ".join(str(q).split())
        if val:
            lines.append(
                f"- Still being determined (tell the caller this is being checked): {val}"
            )
    return "\n".join(lines)


def build_incident_persona_from_file(
    incident_path: str, callee_name: str, language: str = "") -> "tuple[str, str, str]":
    """Build (system, opening, speech_code) from a JSON incident file the BOT wrote
    (`runner.build_call_incident`) — the bot-driven counterpart to
    :func:`build_incident_persona`. Same call behavior and contract (neutral
    intermediary, facts-only); the facts come from the resolved issue's report
    instead of scenarios.json.

    Expected JSON keys (all optional, robust to absence): ``title``, ``owner``,
    ``situation`` (or ``seeds``), ``facts`` (dict or list), ``open_questions``
    (list), ``language``. ``language`` arg wins over the file's; both default to
    English. The title is folded into the spoken situation so the AI leads with
    the headline."""
    import json

    with open(incident_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"--incident-file {incident_path!r} is not a JSON object")

    owner = str(data.get("owner") or "").strip() or "the on-call engineer"
    seeds = " ".join(str(data.get("situation") or data.get("seeds") or "").split()).strip()
    title = " ".join(str(data.get("title") or "").split()).strip()
    if title and title.lower() not in seeds.lower():
        seeds = f"{title}. {seeds}".strip() if seeds else title
    fact_lines = _incident_fact_lines(data.get("facts") or {}, data.get("open_questions") or [])
    lang = language or str(data.get("language") or "") or "en"
    return _render_incident_persona(callee_name, owner, seeds, fact_lines, lang)


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
                         "greeting with a spoken incident briefing (English by default; "
                         "pass --language vi for Vietnamese).")
    ap.add_argument("--incident-file", default=None,
                    help="relay an incident from a JSON file (written by the bot's "
                         "CALL_ON_RESOLVE path) instead of a scenarios.json --persona. "
                         "Same call behavior; the facts come from the resolved issue. "
                         "Ignored when --persona is also given (--persona wins).")
    ap.add_argument("--callee", default="Duc",
                    help="callee's name the reporter addresses on the call (default Duc).")
    ap.add_argument("--system", default=None,
                    help="system instruction (persona). Default: a Vietnamese AI caller.")
    ap.add_argument("--system-file", default=None,
                    help="read the system instruction from this file (overrides --system).")
    ap.add_argument("--language", default=None,
                    help="speech/report language. With --persona it also picks the briefing "
                         "language: default English, or 'vi'/'vi-VN' for Vietnamese. Without "
                         "--persona it's just the BCP-47 speech language_code (e.g. vi-VN).")
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
    speech_language = a.language
    incident_mode = None  # set when relaying an incident (persona OR file)
    persona_speech = "en-US"
    if a.persona:
        incident_mode = f"persona={a.persona!r}"
        system, greet_text, persona_speech = build_incident_persona(
            a.persona, a.callee, language=_persona_lang_key(a.language))
    elif a.incident_file:
        incident_mode = f"file={a.incident_file!r}"
        system, greet_text, persona_speech = build_incident_persona_from_file(
            a.incident_file, a.callee, language=a.language or "")
    else:
        system = a.system or gemini_voice.DEFAULT_SYSTEM
    if incident_mode:
        # Make the AI SPEAK the report language: pin the Live speech language_code to the
        # version's code (en-US / vi-VN) unless the caller pinned one explicitly.
        if not speech_language:
            speech_language = persona_speech
        spoken = "Vietnamese" if _persona_lang_key(speech_language) == "vi" else "English"
        _log(f"  incident-report mode: {incident_mode} → reporting to {a.callee} "
             f"in {spoken} ({speech_language})")
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
        language=speech_language, greet=not a.no_greet, greet_text=greet_text,
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
        # This is a two-way conversation: the callee is silent while LISTENING to the AI.
        # Only end on long MUTUAL dead-air (not one-sided silence), so the call isn't cut
        # mid-answer. A clean hang-up is still caught fast by the roster-collapse signal.
        "--media-flatline-secs", "30",
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
