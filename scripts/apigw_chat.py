#!/usr/bin/env python3
"""Chat with the AI about an incident in the report DM, and ask it to call you.

A standalone, manually-launched loop (the `chat_apigw.sh` wrapper drives it) that
makes `GOOGLE_CHAT_REPORT_SPACE` a two-way channel for ONE scenario incident
(default `apigw` = the API-gateway-timeout incident, `data/scenarios.json`):

  * **Chat freely** — text the DM and the AI answers from the incident's facts
    (grounded, UNTRUSTED-framed; it says it doesn't know rather than inventing).
  * **Ask it to call you** — text "call me" / "gọi lại" (any language) and it
    spawns the real outbound voice call (`call_apigw.sh`) to ring you with the
    incident relay. If a call is MISSED it proactively offers to ring again.

This is the conversational front-end to `call_apigw.sh`: instead of the call just
ringing once and the process hanging when you don't pick up, you keep a chat open
and re-trigger the call by texting.

    python scripts/apigw_chat.py                  # loop (apigw, English call)
    python scripts/apigw_chat.py --language vi     # call back in Vietnamese
    python scripts/apigw_chat.py --persona ops     # a different scenario
    python scripts/apigw_chat.py --once            # one poll cycle, then exit

⚠️  Do NOT run this at the same time as the poller's REPORT_ASSISTANT pointed at
the SAME DM — both would answer every message (double replies). This is the
manual/demo alternative to that always-on path.

The chat uses the configured LLM (LLM_PROVIDER). The voice CALL additionally needs
GEMINI_API_KEY (the call subprocess's gate) + the demo caller browser — without a
key the chat still works and a call-back politely declines. Stdlib only here; the
LLM / Google deps live behind their lazy modules.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# Allow running straight from a checkout without installing the package.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from gchat_agent.config import load_config  # noqa: E402


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# A friendly headline per scenario (scenarios.json has no title field). Used for
# the call-missed offer wording; falls back to "the <persona> incident".
_PERSONA_TITLES = {
    "apigw": "API gateway timeout (504s in production)",
    "dupe": "API gateway timeout (504s in production)",
    "ops": "Skrill payout webhook timeouts",
    "promo": "weekend welcome-bonus promo setup",
}


def _reporter_name(role: str) -> str:
    """Pull the persona's first name out of the 'You are <Name>, ...' role string
    (mirrors call/gemini_call.py so the owner reads the same on chat and call)."""
    head = (role or "").strip()
    if head.lower().startswith("you are "):
        head = head[len("you are "):]
    return head.split(",", 1)[0].strip() or "the on-call engineer"


def _humanize(key: str) -> str:
    """A scenario fact key (e.g. `repro_steps`) → a readable label (`Repro steps`)."""
    return key.replace("_", " ").strip().capitalize()


def _persona_brief(persona_id: str, persona: dict) -> "tuple[str, str, str, list[tuple[str, str]]]":
    """Render a scenarios.json persona into (title, owner, situation, fact_pairs)
    for `prompts.render_incident_brief`."""
    owner = _reporter_name(persona.get("role", ""))
    facts = persona.get("facts", {}) or {}
    situation = " ".join(
        s.strip() for s in (persona.get("seed_messages") or [])
    ).strip()
    fact_pairs = [
        (_humanize(k), str(v)) for k, v in facts.items() if str(v).strip()
    ]
    title = _PERSONA_TITLES.get(persona_id, f"the {persona_id} incident")
    return title, owner, situation, fact_pairs


class _CallLauncher:
    """The `call_back` for the assistant: spawn `call_apigw.sh` as a detached,
    serialized subprocess so a "call me" text rings the report DM with the incident
    relay. Mirrors `runner._spawn_call`: gates on GEMINI_API_KEY + a destination,
    serializes one call at a time (the caller browser + audio host one), and detaches
    the child (`start_new_session=True`) so it outlives a `--once` run. Returns True
    iff a process started; every skip/failure is logged and swallowed."""

    def __init__(self, repo_root: str, config, *, language: str) -> None:
        self._repo_root = repo_root
        self._config = config
        self._language = language
        self._proc: "subprocess.Popen | None" = None

    def __call__(self) -> bool:
        if not (self._config.GEMINI_API_KEY or "").strip():
            _log("call-back requested but no GEMINI_API_KEY — cannot place the call.")
            return False
        dest = (self._config.GOOGLE_CHAT_REPORT_SPACE or "").strip()
        if not dest:
            _log("call-back requested but GOOGLE_CHAT_REPORT_SPACE is unset.")
            return False
        script = os.path.join(self._repo_root, "call_apigw.sh")
        if not os.path.isfile(script):
            _log(f"call script not found: {script}")
            return False
        # Serialize: one call at a time (shared caller browser + audio devices).
        if self._proc is not None and self._proc.poll() is None:
            _log(f"a voice call is already in progress (pid {self._proc.pid}); skipping.")
            return False

        log_dir = (self._config.CALL_LOG_DIR or "logs").strip() or "logs"
        os.makedirs(os.path.join(self._repo_root, log_dir), exist_ok=True)
        log_path = os.path.join(self._repo_root, log_dir, "apigw-chat-call.log")
        argv = ["bash", script, "--url", dest, "--language", self._language]
        try:
            log_fh = open(log_path, "ab")
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=self._repo_root,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,  # detach: survives a --once run
                )
            finally:
                log_fh.close()
            self._proc = proc
            _log(f"placing apigw voice call (pid {proc.pid}); call log → {log_path}")
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort; never crash the loop
            _log(f"voice call launch failed: {exc}")
            return False


def _bot_id(config) -> str | None:
    """The bot's own `users/<id>` from GOOGLE_BOT_USER_ID, normalized — else None
    (the chat client then resolves it via the OAuth tokeninfo endpoint)."""
    val = (config.GOOGLE_BOT_USER_ID or "").strip()
    if not val:
        return None
    return val if val.startswith("users/") else f"users/{val}"


def _provider_label(config) -> str:
    provider = config.LLM_PROVIDER
    if provider == "gemini":
        return f"gemini:{config.GEMINI_MODEL}"
    if provider == "openrouter":
        return f"openrouter:{config.OPENROUTER_MODEL}"
    return provider


def _banner(config, persona_id: str, title: str, language: str, interval: float) -> str:
    key = "set" if (config.GEMINI_API_KEY or "").strip() else "MISSING (call-back disabled)"
    offer = "on" if config.REPORT_MISSED_CALL_OFFER else "off"
    return (
        "apigw chat assistant — chat about an incident + ask it to call you\n"
        f"  DM:       {config.GOOGLE_CHAT_REPORT_SPACE}\n"
        f"  incident: {title} (persona={persona_id})\n"
        f"  provider: {_provider_label(config)}\n"
        f"  call:     gemini key {key}; spoken language {language}\n"
        f"  offer:    missed-call heads-up {offer}\n"
        "  note:     don't also run the poller's REPORT_ASSISTANT on this DM\n"
        f"  poll:     every {interval:g}s — Ctrl-C to stop"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apigw_chat",
        description="Chat about an incident in the report DM and request a call.",
    )
    parser.add_argument(
        "--persona", default="apigw",
        help="scenario to chat about (data/scenarios.json), default 'apigw'.",
    )
    parser.add_argument(
        "--language", default="en",
        help="spoken language for the call-back (en|vi|ru|uk; default en).",
    )
    parser.add_argument(
        "--interval", type=float, default=None,
        help="poll interval seconds (default: config POLL_INTERVAL_SECONDS).",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="run a single poll cycle and exit (for testing / a manual nudge).",
    )
    args = parser.parse_args(argv)

    config = load_config()
    if not (config.GOOGLE_CHAT_REPORT_SPACE or "").strip():
        _log("ERROR: GOOGLE_CHAT_REPORT_SPACE is not set in .env — nowhere to chat.")
        return 2

    from gchat_agent.agent import prompts
    from gchat_agent.agent.staff import load_personas

    personas = load_personas(os.path.join(_REPO_ROOT, "data", "scenarios.json"))
    if args.persona not in personas:
        have = ", ".join(sorted(personas)) or "(none)"
        _log(f"ERROR: persona {args.persona!r} not in scenarios.json (have: {have}).")
        return 2
    title, owner, situation, fact_pairs = _persona_brief(
        args.persona, personas[args.persona]
    )
    system_prompt = (
        prompts.report_assistant_system_prompt()
        + "\n\n"
        + prompts.render_incident_brief(title, owner, situation, fact_pairs)
    )

    from gchat_agent.agent.incident_chat import IncidentChatAssistant
    from gchat_agent.chat.google_rest import GoogleChatClient
    from gchat_agent.llm.openrouter import build_llm

    llm = build_llm(config)
    chat = GoogleChatClient(
        config, user_id=_bot_id(config), space=config.GOOGLE_CHAT_REPORT_SPACE
    )
    launcher = _CallLauncher(_REPO_ROOT, config, language=args.language)
    assistant = IncidentChatAssistant(
        chat, config, llm,
        system_prompt=system_prompt, call_back=launcher, incident_title=title,
    )

    interval = (
        args.interval if args.interval is not None else config.POLL_INTERVAL_SECONDS
    )
    print(_banner(config, args.persona, title, args.language, interval))

    own_id = chat.me()
    if args.once:
        print(f"cycle summary: {assistant.step(own_id)}")
        return 0

    import time

    try:
        while True:
            try:
                summary = assistant.step(own_id)
            except Exception as exc:  # noqa: BLE001 — loop must outlive a bad cycle
                _log(f"[apigw-chat] step failed: {exc}")
                summary = {}
            if any(summary.values()):
                parts = " ".join(f"{k}={v}" for k, v in summary.items() if v)
                print(f"  cycle {parts}", flush=True)
            if own_id is None:  # retry the bootstrap self-id lookup
                own_id = chat.me()
            time.sleep(max(1.0, interval))
    except KeyboardInterrupt:
        print("\n  interrupted — stopping the apigw chat assistant.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
