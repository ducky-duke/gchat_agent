#!/usr/bin/env python3
"""Run the issue-spotter bot's polling loop (§5.7).

Thin entrypoint: load config, wire a `Runner` via `build_runner`, print a startup
banner, then either run a single cycle (`--once`) or loop forever. The bot reads
the configured Google Chat space, detects issues, clarifies until clear, and
writes resolution reports.

    python scripts/run_poller.py            # loop forever (single-runner lock)
    python scripts/run_poller.py --once     # one cycle, then exit

Stdlib only; the LLM / Google deps live behind their lazy modules.
"""
from __future__ import annotations

import argparse
import os
import sys

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from gchat_agent.config import load_config  # noqa: E402
from gchat_agent.runner import build_runner  # noqa: E402


def _banner(config) -> str:
    """A one-paragraph startup banner: space, provider/model, KB on/off."""
    space = config.GOOGLE_SPACE or "(unset GOOGLE_SPACE)"
    provider = config.LLM_PROVIDER
    if provider == "openrouter":
        provider = f"openrouter:{config.OPENROUTER_MODEL}"
    kb = "on" if (config.KB_DIR and os.path.isdir(config.KB_DIR)) else "off"
    dense = " (dense)" if config.RAG_DENSE else ""
    obs = config.OBSERVABILITY
    bot_id = config.GOOGLE_BOT_USER_ID.strip()
    self_filter = (
        f"pinned ({bot_id})" if bot_id
        else "auto-detect via tokeninfo (set GOOGLE_BOT_USER_ID to pin/skip lookup)"
    )
    return (
        "gchat issue-spotter poller\n"
        f"  space:    {space}\n"
        f"  provider: {provider}\n"
        f"  KB/RAG:   {kb}{dense} (top_k={config.RAG_TOP_K}, dir={config.KB_DIR})\n"
        f"  reports:  {config.REPORTS_DIR}\n"
        f"  state:    {config.STATE_FILE}\n"
        f"  obs:      {obs}\n"
        f"  self:     {self_filter}\n"
        f"  poll:     every {config.POLL_INTERVAL_SECONDS}s"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_poller",
        description="Run the Google Chat issue-spotter bot polling loop.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single poll cycle and exit (single-runner lock, fail-fast).",
    )
    args = parser.parse_args(argv)

    config = load_config()
    print(_banner(config))

    runner = build_runner(config)
    if args.once:
        summary = runner.run_once()
        print(f"cycle summary: {summary}")
        return 0

    runner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
