#!/usr/bin/env python3
"""Run one LLM staff persona against the live Google Chat space (§5.8 / §11).

A staff persona is a thin Chat participant that (a) seeds an issue-laden scenario
and (b) answers the issue-spotter bot's clarifying questions in character,
revealing one held fact per reply so the bot's multi-round loop runs to a
resolution. It posts through its *own* account's OAuth token.

    python scripts/run_staff.py --persona ops --token secrets/token_ops.json
    python scripts/run_staff.py --persona promo --token secrets/token_promo.json --once

`--persona` selects the entry in `data/scenarios.json`; `--token` overrides
`config.GOOGLE_TOKEN_FILE` so this process posts as that persona's Gmail account.

Stdlib only; the LLM / Google deps live behind their lazy modules.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from gchat_agent.agent.staff import StaffAgent, load_personas  # noqa: E402
from gchat_agent.chat.google_rest import GoogleChatClient  # noqa: E402
from gchat_agent.config import load_config  # noqa: E402
from gchat_agent.llm.openrouter import build_llm  # noqa: E402
from gchat_agent.models import Message, SenderType  # noqa: E402


def _banner(persona_id: str, token_file: str, config) -> str:
    space = config.GOOGLE_SPACE or "(unset GOOGLE_SPACE)"
    provider = config.LLM_PROVIDER
    if provider == "openrouter":
        provider = f"openrouter:{config.OPENROUTER_MODEL}"
    return (
        f"gchat staff persona: {persona_id}\n"
        f"  space:    {space}\n"
        f"  provider: {provider}\n"
        f"  token:    {token_file}\n"
        f"  poll:     every {config.POLL_INTERVAL_SECONDS}s"
    )


def _latest_bot_question(
    chat: GoogleChatClient,
    thread_id: str,
    own_id: str | None,
) -> Message | None:
    """The most recent message in `thread_id` authored by anyone *other* than
    this persona (i.e. the bot's clarifying question), or None if none yet.

    We treat any non-self message as a candidate question; the bot is the only
    other participant expected to reply in a seeded thread during the demo.
    """
    messages = [m for m in chat.fetch_messages(None) if m.thread_id == thread_id]
    for m in reversed(messages):
        if own_id is not None and m.sender == own_id:
            continue
        if own_id is None and m.sender_type == SenderType.APP:
            # Best-effort fallback before we know our own id.
            continue
        return m
    return None


def _run(staff: StaffAgent, chat: GoogleChatClient, config, once: bool) -> None:
    """Seed once, then poll each seeded thread for a new bot question and answer.

    Tracks the last question message id answered per thread so we never re-answer
    the same question, and stays quiet when a persona has nothing left to reveal.
    """
    seeded = staff.seed()
    threads = sorted({m.thread_id for m in seeded if m.thread_id})
    print(f"seeded {len(seeded)} message(s) across {len(threads)} thread(s)")
    # Emit the seeded thread + message ids (machine-readable) so an orchestrator
    # (e.g. demo_live.sh's precision check) can verify the bot's handling of them.
    for m in seeded:
        if m is not None and m.id:
            print(f"SEEDED_MSG {m.id}")
    if staff.seed_thread_id:
        print(f"SEEDED_THREAD {staff.seed_thread_id}")

    # thread_id -> last bot-question message id we have already answered.
    answered: dict[str, str] = {}

    while True:
        own_id = chat.me()
        for thread_id in threads:
            try:
                question = _latest_bot_question(chat, thread_id, own_id)
            except Exception as exc:  # noqa: BLE001 - one bad thread must not kill the loop
                print(f"[{thread_id}] fetch failed: {exc}")
                continue
            if question is None:
                continue  # no new bot question yet
            if answered.get(thread_id) == question.id:
                continue  # already answered this one
            try:
                reply = staff.answer_question(thread_id, question.text)
            except Exception as exc:  # noqa: BLE001
                print(f"[{thread_id}] answer failed: {exc}")
                continue
            answered[thread_id] = question.id
            if reply is not None:
                print(f"[{thread_id}] answered: {reply.text[:80]}")
            else:
                print(f"[{thread_id}] nothing left to disclose")
        if once:
            return
        time.sleep(max(1, config.POLL_INTERVAL_SECONDS))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_staff",
        description="Run an LLM staff persona against the live Google Chat space.",
    )
    parser.add_argument(
        "--persona",
        required=True,
        choices=("ops", "promo", "apigw", "noise"),
        help="which persona from data/scenarios.json to run. 'noise' is a "
        "control persona: benign small talk with no issue, used to prove the "
        "bot does NOT file an issue for non-issue chatter.",
    )
    parser.add_argument(
        "--token",
        required=True,
        help="path to this persona's OAuth token JSON (overrides GOOGLE_TOKEN_FILE).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="seed, run a single answer pass, then exit.",
    )
    parser.add_argument(
        "--seed-suffix",
        default="",
        help="append a per-run suffix to every post's request_id so a RE-RUN "
        "against the same space posts fresh seeds/answers instead of deduping to "
        "a previous run's (old) messages. Empty (default) keeps the stable, "
        "crash-idempotent ids.",
    )
    args = parser.parse_args(argv)

    config = load_config()
    print(_banner(args.persona, args.token, config))

    personas = load_personas()
    if args.persona not in personas:
        available = ", ".join(sorted(personas)) or "(none)"
        parser.error(f"persona {args.persona!r} not in scenarios.json (have: {available})")

    llm = build_llm(config)
    chat = GoogleChatClient(config, token_file=args.token)
    staff = StaffAgent(llm, chat, personas[args.persona], request_suffix=args.seed_suffix)

    _run(staff, chat, config, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
