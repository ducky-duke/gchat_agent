#!/usr/bin/env python3
"""Webhook ingress entrypoint — DEFERRED to Phase 2 (§5.4/§13 phase 9).

The v1 demo uses the REST poller as the only live ingress. The @mention webhook
(``webhook.py``: fast-ack + async post, mandatory bearer verification) is a
later, optional slice and is NOT built in v1 — this is a placeholder stub.
"""
from __future__ import annotations


def main() -> int:
    print("run_webhook: the Google Chat webhook ingress is deferred to Phase 2 "
          "(see PLAN §5.4/§13 phase 9); not implemented in v1.")
    print("v1 live ingress is the REST poller — run scripts/run_poller.py instead.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
