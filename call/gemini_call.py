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
import dm_resolve            # noqa: E402  (URL normalize + scrape the callee's name)
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


# ── Incident-report persona: ONE English prompt, output language parametrized ──────────
# A single shared instruction template (the contract: neutral intermediary, facts-only,
# names {owner} as the owner, declines anything outside the report). The SPOKEN language is
# a parameter ({output_language}) — the model relays the English report in that language and
# the Live speech language_code is pinned to match. Add a language by adding ONE row to
# _INCIDENT_LANGS; there is no second prompt to keep in sync. Placeholders: {callee} {owner}
# {seeds} {facts} {output_language} (str.format, so the template must contain no other { }).
# NOTE (temporary, 2026-06-23): the native Vietnamese prompt was removed — `--language vi`
# now drives THIS English prompt with output_language=Vietnamese (vi-VN). Re-add a native
# block (the old _INCIDENT_SYSTEM_VI) if VI naturalness/register regresses on a real call.
_INCIDENT_SYSTEM = """You are the INCIDENT-DUTY ASSISTANT (an AI) for the engineering team at an iGaming operator. You are CALLING {callee} (the engineering team lead) to RELAY an ongoing production incident on behalf of the on-call engineer, {owner}. You are an INTERMEDIARY passing the information along — you are NOT the person fixing it, and NOT the person responsible for the incident. This is a live voice call, not a chat.

OUTPUT LANGUAGE — MANDATORY: Speak and respond ONLY in {output_language}, from the very first word through the goodbye. The incident report below is written in English, but you MUST relay it in {output_language} — translate it as you speak. Do NOT switch to English; only unavoidable technical terms / identifiers stay verbatim (API gateway, 504, p99, INFRA-2207). Even if {callee} addresses you in another language, keep answering in {output_language}.

Role & MANDATORY rules:
- You ONLY relay information that is in the "Incident report" below. You do NOT own and do NOT work this incident.
- When asked "who is responsible / who is handling it / who owns it", say clearly it is {owner} (Platform on-call team this week). NEVER take responsibility yourself — you are only relaying the report.
- Refer to {owner} in the THIRD PERSON. The report below may be written in {owner}'s own voice (first person "I") — when you relay it, switch to third person; do NOT speak as if you were {owner}.
- USE ONLY the facts in the report. If asked anything NOT in the report, say plainly (in {output_language}) that it is not in the report you have and that you will check with {owner} / the team and get back to them. NEVER guess, NEVER invent figures, names, timestamps, or root causes.

How to behave on the call:
- OPEN IMMEDIATELY when {callee} picks up: greet them, say you're the incident-duty assistant calling to relay an incident {owner} just raised, then summarize in 2-3 sentences: what is broken, how it affects players, and who is handling it. Urgent and clear — don't read it like a script.
- Then answer {callee}'s questions directly and briefly (1-2 sentences each), strictly from the report.
- ALWAYS speak natural {output_language}. Keep technical terms as-is (API gateway, 504, p99, INFRA-2207...). Do NOT read markdown/bullets aloud.
- ENDING THE CALL: Judge from what {callee} SAYS whether they want to finish (e.g. "thanks", "that's enough", "ok keep me posted", "talk later", "bye"). If it is clearly a goodbye, say a SHORT warm goodbye in {output_language}, THEN call the `end_call` function to hang up. If you are NOT sure they want to hang up (it might just be a pause, or they may still have a question), ask ONE short confirming question first in {output_language} (e.g. "Is there anything else I can help with, or shall I let you go?") and only call `end_call` after they confirm. NEVER call `end_call` while {callee} still has questions or is mid-sentence. Calling `end_call` hangs up the call — only do it when you are confident the conversation is over.

Incident report (everything you are allowed to relay — NOTHING beyond this):
- Reporter & owner of the incident: {owner} (Platform on-call team this week)
- Situation: {seeds}
{facts}
"""
_INCIDENT_OPENING = (
    "(The call just connected — you hear {callee} pick up. START NOW, speaking {output_language}: "
    "greet {callee}, introduce yourself as the incident-duty assistant calling to "
    "relay a production incident {owner} just raised, then summarize the incident — all in {output_language}.)"
)

# language key → (spoken-language display name, BCP-47 speech language_code). The ONE shared
# English prompt above is rendered with output_language = this display name; the speech code
# pins the Live voice to that language. Add a language by adding a row here — no new prompt.
_INCIDENT_LANGS = {
    "en": ("English", "en-US"),
    "vi": ("Vietnamese", "vi-VN"),
    "ru": ("Russian", "ru-RU"),
    "uk": ("Ukrainian", "uk-UA"),
}


def _persona_lang_key(language: "str | None") -> str:
    """Normalize a --language value (a key like 'vi' or a BCP-47 code like 'vi-VN') to one
    of the _INCIDENT_LANGS keys. Matches by prefix so 'uk-UA' → 'uk'; default 'en'."""
    lang = (language or "").lower()
    for key in _INCIDENT_LANGS:
        if key != "en" and lang.startswith(key):
            return key
    return "en"


def _render_incident_persona(
    callee_name: str, owner: str, seeds: str, fact_lines: str, language: "str | None",
) -> "tuple[str, str, str]":
    """Render the ONE shared English prompt with the incident fields + the chosen
    spoken output language. Shared by both the scenarios.json path
    (`build_incident_persona`) and the bot-driven path
    (`build_incident_persona_from_file`). Returns (system_instruction,
    opening_trigger, BCP-47 speech language_code)."""
    lang_name, speech_code = _INCIDENT_LANGS[_persona_lang_key(language)]
    fields = {
        "callee": callee_name,
        "owner": owner or "the on-call engineer",
        "seeds": seeds,
        "facts": fact_lines,
        "output_language": lang_name,
    }
    return _INCIDENT_SYSTEM.format(**fields), _INCIDENT_OPENING.format(**fields), speech_code


def build_incident_persona(
    persona_id: str, callee_name: str, language: str = "en") -> "tuple[str, str, str]":
    """Load a scenarios.json persona (e.g. apigw = the API-gateway 504 incident) and render
    it into (system_instruction, opening_trigger, speech_language_code) for a LIVE VOICE
    incident report. The AI is a NEUTRAL INTERMEDIARY that *relays* an incident raised by the
    on-call engineer ({owner}) — it is NOT that engineer and does NOT own the incident. On
    pickup it announces the incident on the owner's behalf, then answers strictly from the
    report's facts. The report is the hard ceiling: it never invents specifics and explicitly
    says it doesn't know (will check back) when asked anything not in the report.

    ``language`` selects the language the AI SPEAKS — the ONE shared English prompt is
    rendered with that output language: "en" (default) → English / en-US, else the matching
    _INCIDENT_LANGS row (vi → Vietnamese, ru → Russian, uk → Ukrainian; BCP-47 forms like
    "uk-UA" are matched by prefix). The scenario facts/seeds are English in the data file
    either way; the model translates them into the output language as it speaks."""
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
    oq = open_questions if isinstance(open_questions, (list, tuple)) else []
    for q in oq:
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
    ap.add_argument("--url", default=None,
                    help="DM to call into (REQUIRED — no hardcoded default). Accepts a "
                         "full Chat URL, 'spaces/<id>', 'chat/<id>', or a bare '<id>'. "
                         "If omitted, falls back to GOOGLE_CHAT_REPORT_SPACE in .env; if that "
                         "is unset the call ABORTS with an error. Whatever resolves is "
                         "also where the callee's name is read from when --callee is "
                         "omitted.")
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
    ap.add_argument("--callee", default=None,
                    help="callee's name the AI addresses on the call. Omit it and the "
                         "name is read automatically from the --url DM (the partner's "
                         "display name); an explicit value always wins.")
    ap.add_argument("--system", default=None,
                    help="system instruction (persona). Default: an English AI caller.")
    ap.add_argument("--system-file", default=None,
                    help="read the system instruction from this file (overrides --system).")
    ap.add_argument("--language", default=None,
                    help="speech/report language. With --persona it also picks the briefing "
                         "language: default English ('en'), or vi/ru/uk (also accepts the "
                         "BCP-47 form, e.g. 'vi-VN', 'uk-UA'). Without --persona it's just "
                         "the BCP-47 speech language_code (e.g. vi-VN).")
    ap.add_argument("--no-greet", action="store_true",
                    help="don't make Gemini speak first; wait for the callee (reactive).")
    ap.add_argument("--no-record", action="store_true",
                    help="don't record the call audio to logs/ (recording is on by default "
                         "for debugging — both directions to WAV next to the debug log).")
    ap.add_argument("--no-nudge", action="store_true",
                    help="don't have the AI re-engage when the callee goes silent. By "
                         "default the caller checks in after a stretch of mutual silence "
                         "(Gemini Live is turn-based, so a silent callee would otherwise "
                         "leave the model mute) — bounded, and reset when the callee speaks.")
    ap.add_argument("--no-end-call", action="store_true",
                    help="don't give the AI the end_call tool. By default the model can "
                         "hang up the call itself when it judges (from what the callee "
                         "says) that they're done — saying a goodbye, asking ONE verifying "
                         "question if unsure, then ending the call AND closing the caller "
                         "browser. Use this to keep the call open until a real hang-up / "
                         "the duration cap.")
    ap.add_argument("--quit-browser", action="store_true",
                    help="stop the caller Brave on exit. Default: leave it running so the "
                         "login persists and the next call is instant. (Note: when the AI "
                         "ends the call via end_call, the caller browser is closed anyway.)")
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

    # Resolve the destination: --url, else GOOGLE_CHAT_REPORT_SPACE from .env. No hardcoded
    # fallback — if neither is set we abort with a clear error rather than silently
    # ringing some default DM. Accepts a full Chat URL, "spaces/<id>", "chat/<id>", or
    # a bare "<id>" — all reduce to the standalone DM deep link we ring AND scrape the
    # callee's name from.
    raw_url = a.url or dm_resolve.env_value(_REPO_ROOT, "GOOGLE_CHAT_REPORT_SPACE")
    if not raw_url:
        _log("ERROR: no call destination. Pass --url (a full Chat URL, 'spaces/<id>', "
             "'chat/<id>', or a bare '<id>'), or set GOOGLE_CHAT_REPORT_SPACE in .env.")
        return 2
    url = dm_resolve.normalize_dm_url(raw_url)

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
    if not ai_call._ensure_logged_in(a.port, url, wait_s=a.login_wait):
        _log("  (leaving the browser open so you can finish signing in.)")
        return 2

    # 2) Who are we calling? An explicit --callee wins; otherwise read the partner's
    #    name straight off the DM the caller just opened — so you can hand this tool
    #    JUST a URL/space id and skip --callee. (The REST API hides displayName under
    #    user OAuth, so the rendered UI is the only name source.) Falls back to a
    #    neutral label if the page can't be read.
    callee = (a.callee or "").strip()
    if not callee:
        _log("  resolving the callee's name from the DM …")
        callee = dm_resolve.resolve_callee_name(a.port, url, log=_log) or ""
        if callee:
            _log(f"  callee resolved from the DM: {callee}")
        else:
            callee = "the team lead"
            _log(f"  ⚠️  could not resolve the callee's name; using {callee!r}")

    # 3) Persona/system + the opening the AI says first on pickup. --persona turns the
    #    call into an incident REPORT (AI briefs the callee on the incident), otherwise
    #    it's the default friendly greeting. Precedence: --system-file > --system > persona.
    greet_text = None
    speech_language = a.language
    incident_mode = None  # set when relaying an incident (persona OR file)
    persona_speech = "en-US"
    if a.persona:
        incident_mode = f"persona={a.persona!r}"
        system, greet_text, persona_speech = build_incident_persona(
            a.persona, callee, language=_persona_lang_key(a.language))
    elif a.incident_file:
        incident_mode = f"file={a.incident_file!r}"
        system, greet_text, persona_speech = build_incident_persona_from_file(
            a.incident_file, callee, language=a.language or "")
    else:
        system = a.system or gemini_voice.DEFAULT_SYSTEM
    if incident_mode:
        # Make the AI SPEAK the report language: pin the Live speech language_code to the
        # version's code (en-US / vi-VN) unless the caller pinned one explicitly.
        if not speech_language:
            speech_language = persona_speech
        spoken = _INCIDENT_LANGS[_persona_lang_key(speech_language)][0]
        _log(f"  incident-report mode: {incident_mode} → reporting to {callee} "
             f"in {spoken} ({speech_language})")
    if a.system_file:
        try:
            with open(a.system_file, encoding="utf-8") as f:
                system = f.read().strip()
        except OSError as exc:
            _log(f"ERROR: could not read --system-file: {exc}")
            return 2

    # The AI can hang up the call itself (end_call tool): when the bridge fires this, set
    # the event the call loop polls so it ends the call. We ALSO close the caller browser
    # in that case (the finally below) — "end the call" means tear the whole thing down.
    end_event = threading.Event()

    def _on_ai_end_call() -> None:
        _log("  🔚 AI ended the call (callee asked to wrap up) — hanging up + closing browser")
        end_event.set()

    # 2) Audio devices up BEFORE the call, so the browser grabs the virtual mic/speaker.
    bridge = gemini_voice.GeminiVoiceBridge(
        api_key=key, model=a.model, voice=a.voice, system=system,
        language=speech_language, greet=not a.no_greet, greet_text=greet_text,
        record=not a.no_record, nudge_on_silence=not a.no_nudge,
        end_call_tool=not a.no_end_call, on_end_call=_on_ai_end_call)
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
        "--url", url,
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
            mcb_argv, on_join=bridge.on_join, on_pickup=bridge.on_pickup,
            stop_event=end_event)
    except KeyboardInterrupt:
        _log("\n  interrupted — ending the call and the Gemini session.")
    finally:
        bridge.signal_stop()
        worker.join(timeout=10)
        bridge.teardown_devices()
        # Close the caller browser if the user asked (--quit-browser) OR the AI ended the
        # call itself (end_call). _kill_profile_braves is SCOPED to this dedicated caller
        # profile path (a /proc cmdline match on `profile`), so it never touches the daily
        # Brave, the callee profile, or any other browser — only the caller we drove.
        ai_ended = end_event.is_set()
        if ai_ended or (launched and a.quit_browser):
            if ai_ended:
                _log("  🔚 closing the caller browser (AI ended the call) …")
            ai_call._kill_profile_braves(profile)
        elif launched:
            _log(f"\n  caller Brave left running on :{a.port} "
                 "(login persists; --quit-browser to stop it).")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
