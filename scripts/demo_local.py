#!/usr/bin/env python3
"""Local end-to-end demo of the full agent loop — no Google Chat required.

Drives the *real* :class:`~gchat_agent.runner.Runner` +
:class:`~gchat_agent.agent.analyzer.Analyzer` (with live RAG over ``KB_DIR``) and
the *real* :class:`~gchat_agent.agent.staff.StaffAgent` personas from
``data/scenarios.json`` — all over an in-memory :class:`FakeChatClient` shared by
the bot and each staff member (each posting under its own ``users/<id>``, exactly
as the live demo gives every participant its own account in one Space).

The LLM is whatever ``.env`` selects: ``LLM_PROVIDER=gemini`` runs the live model
(the point of this script — it exercises detect → clarify rounds → resolve →
Markdown report against a real model); ``LLM_PROVIDER=mock`` runs fully offline.

    python scripts/demo_local.py                     # ops scenario, live LLM
    python scripts/demo_local.py --persona both      # ops + promo, two threads
    python scripts/demo_local.py --max-rounds 5      # raise the clarify cap

State is written to a throwaway temp dir (so detection always runs fresh) and
resolution reports land in ``reports/demo/`` (wiped each run, openable after).
Stdlib + the lazy LLM module only.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import replace

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from gchat_agent.agent.analyzer import Analyzer  # noqa: E402
from gchat_agent.agent.staff import StaffAgent, load_personas  # noqa: E402
from gchat_agent.agent.state import IssueStore  # noqa: E402
from gchat_agent.config import load_config  # noqa: E402
from gchat_agent.llm.openrouter import build_llm  # noqa: E402
from gchat_agent.llm.tts import build_tts  # noqa: E402
from gchat_agent.models import Status  # noqa: E402
from gchat_agent.rag.store import build_retriever  # noqa: E402
from gchat_agent.runner import Runner  # noqa: E402

BOT_ID = "users/bot"


def _friendly(role: str) -> str:
    """A short display name from a persona ``role`` ('You are Sam, an …' → 'Sam')."""
    role = (role or "").strip()
    if role.lower().startswith("you are "):
        rest = role[len("you are "):]
        name = rest.split(",")[0].split(".")[0].strip()
        return name or "staff"
    return "staff"


def _import_fakes():
    """The deterministic in-memory ChatClient + the shared `StaffChatView` live
    under ``tests/`` — add the repo root to the path and import them (no
    test-framework deps). Returns ``(FakeChatClient, StaffChatView)``."""
    repo_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    sys.path.insert(0, os.path.abspath(repo_root))
    from tests.fakes import FakeChatClient, StaffChatView  # noqa: E402
    return FakeChatClient, StaffChatView


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="demo_local",
        description="Run the full agent loop locally (no Google Chat) with the .env LLM.",
    )
    parser.add_argument("--persona", choices=("ops", "promo", "both"), default="ops",
                        help="which staff scenario(s) to seed (default: ops).")
    parser.add_argument("--max-rounds", type=int, default=5,
                        help="MAX_CLARIFY_ROUNDS for the demo (default: 5).")
    parser.add_argument("--max-cycles", type=int, default=12,
                        help="safety cap on run_cycle iterations (default: 12).")
    parser.add_argument("--scenarios", default="data/scenarios.json",
                        help="path to the staff scenarios file.")
    parser.add_argument("--voice", action="store_true",
                        help="deliver resolution reports as TTS voice notes "
                             "instead of Markdown; the synthesized MP3s are saved "
                             "under reports/demo/ so you can play them.")
    args = parser.parse_args(argv)

    FakeChatClient, StaffChatView = _import_fakes()

    # Fresh state each run (so detection always fires); reports persist for viewing.
    state_dir = tempfile.mkdtemp(prefix="gchat-demo-")
    reports_dir = "reports/demo"
    shutil.rmtree(reports_dir, ignore_errors=True)

    base = load_config()
    config = replace(
        base,
        STATE_FILE=os.path.join(state_dir, "issues.json"),
        REPORTS_DIR=reports_dir,
        # Backfill from before the FakeChatClient's fixed epoch (2026-01-01) so
        # the first cycle fetches the seed instead of pinning the cursor to "now".
        POLL_BACKFILL_SINCE="2020-01-01T00:00:00Z",
        MAX_CLARIFY_ROUNDS=args.max_rounds,
        # Voice mode: synthesize a spoken report and "post" it (recorded in the
        # FakeChatClient); GOOGLE_CHAT_REPORT_SPACE stays empty so it threads in-place.
        REPORT_DELIVERY="voice" if args.voice else base.REPORT_DELIVERY,
    )

    provider = config.LLM_PROVIDER
    if provider == "gemini":
        model = config.GEMINI_MODEL
    elif provider == "openrouter":
        model = config.OPENROUTER_MODEL
    else:
        model = "(mock)"
    print("=" * 72)
    print("gchat issue-spotter — LOCAL demo (in-memory Chat, no Google)")
    print(f"  provider : {provider}  model={model}")
    print(f"  KB/RAG   : dir={config.KB_DIR} (top_k={config.RAG_TOP_K}, dense={config.RAG_DENSE})")
    print(f"  persona  : {args.persona}   max_rounds={config.MAX_CLARIFY_ROUNDS}")
    print(f"  reports  : {reports_dir}/")
    if provider == "mock":
        print("  NOTE: not the live model — set LLM_PROVIDER=gemini in .env for the live demo.")
    print("=" * 72)

    llm = build_llm(config)
    tts = build_tts(config)  # None unless --voice
    retriever = build_retriever(config.KB_DIR, history=None, dense=config.RAG_DENSE)
    print(f"  retriever: {'BM25+boost over KB' if retriever else 'none (direct-context)'}")
    if args.voice:
        print(f"  delivery : voice (TTS model={config.TTS_MODEL}, voice={config.TTS_VOICE})")

    chat = FakeChatClient(me=BOT_ID)
    store = IssueStore(config.STATE_FILE)
    analyzer = Analyzer(llm, retriever, config.RAG_TOP_K)
    runner = Runner(chat, analyzer, store, config, reports_dir=reports_dir, llm=llm, tts=tts)

    personas = load_personas(args.scenarios)
    pids = ["ops", "promo"] if args.persona == "both" else [args.persona]

    names = {BOT_ID: "bot"}
    staff_by_thread: dict[str, StaffAgent] = {}

    print("\n--- staff seed the space -------------------------------------------")
    for pid in pids:
        if pid not in personas:
            print(f"  (skipping unknown persona {pid!r})")
            continue
        staff_id = f"users/staff-{pid}"
        names[staff_id] = f"{_friendly(personas[pid].get('role', ''))} ({pid})"
        staff = StaffAgent(llm, StaffChatView(chat, me=staff_id), personas[pid])
        seeded = staff.seed()
        if seeded:
            staff_by_thread[seeded[0].thread_id] = staff

    def label(sender: str) -> str:
        return names.get(sender, sender)

    printed = 0

    def flush_new() -> None:
        """Print every message appended to the space since the last flush."""
        nonlocal printed
        for m in chat.messages[printed:]:
            who = label(m.sender)
            for i, line in enumerate(m.text.splitlines() or [""]):
                prefix = f"  {who:>16} │ " if i == 0 else " " * 19 + "│ "
                print(prefix + line)
        printed = len(chat.messages)

    flush_new()

    # answered: (issue_id, round_index) so each distinct bot question is answered once.
    answered: set[tuple[str, int]] = set()

    print("\n--- the bot runs its loop ------------------------------------------")
    for cycle in range(1, args.max_cycles + 1):
        summary = runner.run_cycle()
        print(f"\n[cycle {cycle}] {summary}")
        flush_new()  # any bot questions / confirmations posted this cycle

        # Each owning staff member answers a fresh clarifying question in character.
        for issue in store.open_issues():
            staff = staff_by_thread.get(issue.thread_id)
            if staff is None or not issue.questions_asked:
                continue
            round_key = (issue.id, len(issue.questions_asked))
            if round_key in answered:
                continue
            answered.add(round_key)
            staff.answer_question(issue.thread_id, issue.questions_asked[-1])
        flush_new()  # any staff replies just posted

        # Done when every detected issue has closed (resolved or stale).
        if store.all_issues() and not store.open_issues():
            break

    # --- outcome ---------------------------------------------------------------
    print("\n--- outcome --------------------------------------------------------")
    issues = store.all_issues()
    if not issues:
        print("  no issues were detected.")
    for iss in issues:
        mark = {Status.RESOLVED: "✅ resolved", Status.STALE: "⏳ stale"}.get(
            iss.status, f"… {iss.status.value}")
        print(f"  {mark}: [{iss.severity.value}] {iss.title}  "
              f"(rounds={iss.rounds}, q&a={len(iss.qa)})")

    resolved = [i for i in issues if i.status == Status.RESOLVED]
    if resolved and args.voice:
        print("\n--- voice report(s) ------------------------------------------------")
        os.makedirs(reports_dir, exist_ok=True)
        for post in chat.voice_posts:
            audio = post.get("audio") or b""
            out = os.path.join(reports_dir, post.get("filename", "report.mp3"))
            with open(out, "wb") as fh:
                fh.write(audio)
            print(f"  🔊 {out}  ({len(audio)} bytes)  caption: {post.get('text', '')}")
        if not chat.voice_posts:
            print("  (no voice reports were produced)")
    elif resolved:
        print("\n--- resolution report(s) -------------------------------------------")
        for iss in resolved:
            path = os.path.join(reports_dir, f"issue-{iss.id}.md")
            print(f"\n# {path}")
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    print(fh.read().rstrip())

    shutil.rmtree(state_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
