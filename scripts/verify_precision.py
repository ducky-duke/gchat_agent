#!/usr/bin/env python3
"""Verify the demo's CONTROL CASE: the bot saw the benign "noise" messages and
chose NOT to act on them — i.e. it ignored them by *judgment*, not because they
were self-filtered (its own account) or never fetched.

The hollow-pass trap this guards against: if the noise were posted by the bot's
own account, the bot would drop it by the self-filter and "opened 0 issues for
the noise" would prove nothing. So this checks, against the LIVE space + the
bot's own state, three independent signals for the noise thread:

  DELIVERED   k/n   the noise messages actually landed in the space
  BOT_REPLIES m     messages the BOT posted INTO the noise thread (must be 0 —
                    a non-zero count means it engaged/clarified the chatter)
  NOISE_SEEN  k/n   noise ids present in the bot's recent `seen_message_ids`
                    window (direct proof it fetched them; the window is bounded,
                    so a miss is INCONCLUSIVE, never a fail)

It also re-confirms the noise sender is NOT the bot account (the whole point).

Output: machine-readable `KEY value` lines + a final `VERDICT <PASS|REGRESSION|
INCONCLUSIVE>`. Exit code: 0 = PASS, 2 = REGRESSION (bot engaged the noise),
3 = INCONCLUSIVE (could not confirm the bot fetched the noise). Stdlib + the
package's own Google client; reads the live space, mutates nothing.

    python scripts/verify_precision.py \
        --noise-thread spaces/X/threads/T \
        --noise-msg spaces/X/messages/a --noise-msg spaces/X/messages/b \
        --state .state/issues.json --bot-token secrets/token_bot.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from gchat_agent.chat.google_rest import GoogleChatClient  # noqa: E402
from gchat_agent.config import load_config  # noqa: E402


def _bot_id(config, chat: GoogleChatClient) -> str | None:
    """The bot's own ``users/<id>`` — the configured pin if set, else live."""
    return (config.GOOGLE_BOT_USER_ID or "").strip() or chat.me()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_precision")
    parser.add_argument("--noise-thread", required=True, help="the noise thread id.")
    parser.add_argument(
        "--noise-msg",
        action="append",
        default=[],
        help="a seeded noise message resource name (repeatable).",
    )
    parser.add_argument("--state", default=".state/issues.json")
    parser.add_argument("--bot-token", default="secrets/token_bot.json")
    args = parser.parse_args(argv)

    config = load_config()
    chat = GoogleChatClient(config, token_file=args.bot_token)
    bot = _bot_id(config, chat)

    # Live view of the space as the bot sees it.
    messages = chat.fetch_messages(None)
    in_thread = [m for m in messages if m.thread_id == args.noise_thread]
    present_ids = {m.id for m in messages}

    noise_ids = list(args.noise_msg)
    delivered = [mid for mid in noise_ids if mid in present_ids]
    # The control fails the instant the bot posts ANYTHING into the noise thread.
    bot_replies = [m for m in in_thread if bot is not None and m.sender == bot]
    # Direct proof the bot fetched the noise (bounded recent-id window in state).
    seen: set[str] = set()
    try:
        with open(args.state, encoding="utf-8") as fh:
            seen = set(json.load(fh).get("seen_message_ids", []) or [])
    except (OSError, ValueError):
        pass
    seen_noise = [mid for mid in noise_ids if mid in seen]

    # Re-confirm the noise did NOT come from the bot account (else self-filtered).
    noise_senders = {m.sender for m in in_thread if not (bot and m.sender == bot)}
    from_bot_account = bool(bot) and any(m.sender == bot for m in in_thread if m.id in set(noise_ids))

    n = len(noise_ids)
    print(f"NOISE_SENDER {'|'.join(sorted(noise_senders)) or '(none seen)'}")
    print(f"BOT_ACCOUNT {bot}")
    print(f"FROM_BOT_ACCOUNT {'yes' if from_bot_account else 'no'}")
    print(f"DELIVERED {len(delivered)}/{n}")
    print(f"BOT_REPLIES {len(bot_replies)}")
    print(f"NOISE_SEEN {len(seen_noise)}/{n}")

    # Verdict. A bot reply in the noise thread is an unambiguous regression.
    if bot_replies:
        print("VERDICT REGRESSION")
        return 2
    # If the noise came from the bot's own account, the test is meaningless.
    if from_bot_account or not noise_senders:
        print("VERDICT INCONCLUSIVE")
        return 3
    # We want positive proof the bot FETCHED at least one noise message; the
    # seen-id window is bounded, so a miss is inconclusive (not a pass, not a fail).
    if not seen_noise:
        print("VERDICT INCONCLUSIVE")
        return 3
    print("VERDICT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
