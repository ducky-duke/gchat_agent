#!/usr/bin/env python3
"""Verify the demo's DEDUP/MERGE case: a SECOND reporter raised the SAME incident
in their own thread, and the bot folded both reports into ONE issue instead of
filing two (cross-thread near-duplicate merge in IssueStore — §6).

Pure state analysis: it reads the bot's own ``.state/issues.json`` and attributes
the tracked issues to the two seeded threads, mutating nothing.

  INCIDENT_ISSUES  n       issues anchored to the original incident thread (== 1)
  DUPE_ISSUES      n       issues anchored to the SECOND reporter's thread
                          (0 ⇒ folded into the incident issue, i.e. merged)
  DUPE_FOLDED_IN   yes/no  a dupe message id appears in an incident issue's
                          ``source_message_ids`` — positive proof the second
                          report's evidence was merged in, not dropped

Verdict:
  MERGED        the second report folded into the one incident issue (exit 0)
  SEPARATE      the dupe became its OWN issue — the merge did not fire (exit 2)
  INCONCLUSIVE  no incident issue yet, or the bot has not detected the dupe (3)

Note: the live merge depends on the frontier model phrasing the two reports
similarly enough to clear the IssueStore cross-thread bar — so SEPARATE here is a
best-effort miss, NOT a regression. The merge itself is proven deterministically
by tests/test_issue_store.py (IssueStoreCrossThreadMergeTest).

    python scripts/verify_dedup.py \
        --incident-thread spaces/X/threads/A --dupe-thread spaces/X/threads/B \
        --dupe-msg spaces/X/messages/b1 --dupe-msg spaces/X/messages/b2 \
        --state .state/issues.json
"""
from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_dedup")
    parser.add_argument("--incident-thread", required=True, help="the incident thread id.")
    parser.add_argument("--dupe-thread", required=True, help="the second reporter's thread id.")
    parser.add_argument(
        "--dupe-msg",
        action="append",
        default=[],
        help="a seeded duplicate message resource name (repeatable).",
    )
    parser.add_argument("--state", default=".state/issues.json")
    args = parser.parse_args(argv)

    try:
        with open(args.state, encoding="utf-8") as fh:
            issues = json.load(fh).get("issues") or []
    except (OSError, ValueError):
        issues = []

    dupe_ids = set(args.dupe_msg)
    incident_issues = [i for i in issues if i.get("thread_id") == args.incident_thread]
    dupe_issues = [i for i in issues if i.get("thread_id") == args.dupe_thread]
    folded = any(
        dupe_ids & set(i.get("source_message_ids") or []) for i in incident_issues
    )

    print(f"TOTAL_ISSUES {len(issues)}")
    print(f"INCIDENT_ISSUES {len(incident_issues)}")
    print(f"DUPE_ISSUES {len(dupe_issues)}")
    print(f"DUPE_FOLDED_IN {'yes' if folded else 'no'}")

    # A separate issue anchored to the dupe thread means the merge did not fire.
    if dupe_issues:
        print("VERDICT SEPARATE")
        return 2
    # The dupe's evidence landed inside the one incident issue → merged.
    if incident_issues and folded:
        print("VERDICT MERGED")
        return 0
    # Incident issue exists but the dupe is neither folded in nor its own issue —
    # the bot most likely has not detected the second report yet.
    print("VERDICT INCONCLUSIVE")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
