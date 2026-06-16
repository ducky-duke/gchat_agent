"""Bot self-filtering: the bot must never detect/clarify its OWN account's
messages (§5.7/§6).

The live `GoogleChatClient` only learns its own `users/<id>` *after its first
post* (`me()`), and `build_runner` seeds that id from persisted `.state/`. On a
fresh start (deleted state) cycle 1 therefore has no self id — detection can't
drop the bot's own messages and the bot clarifies with itself. `GOOGLE_BOT_USER_ID`
pins the id so self-filtering works from the very first cycle.

These pin:
* `_normalize_user_id` accepts a bare id or the full form, blank ⇒ None;
* `load_config` reads `GOOGLE_BOT_USER_ID`;
* `build_runner` seeds the client's `me()` from the configured id (taking
  precedence over persisted state, falling back to it when unset);
* the runner's detection self-filter: with a known own id, the bot's own
  issue-shaped message is NOT detected; an identical staff message IS — and with
  NO own id (the bootstrap gap) the bot's own message leaks through.

Stdlib `unittest`; offline (MockLLM + FakeChatClient / lazy GoogleChatClient); no
network.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace

from gchat_agent.agent.analyzer import Analyzer
from gchat_agent.agent.state import IssueStore
from gchat_agent.config import load_config
from gchat_agent.llm.mock import MockLLM
from gchat_agent.runner import Runner, _normalize_user_id, build_runner
from tests.fakes import FakeChatClient

BOT_ID = "users/bot"
STAFF_ID = "users/staff-ops"
SEED_TEXT = "Payments are failing in production and blocking checkout, need help asap."


def _config(tmp: str, **over):
    """A real Config off the defaults, paths redirected to a temp dir, offline
    (mock LLM, no KB so the retriever is bypassed), early backfill so the first
    cycle fetches the seed."""
    cfg = replace(
        load_config(env_file=os.path.join(tmp, "no-such.env")),
        LLM_PROVIDER="mock",
        STATE_FILE=os.path.join(tmp, "state", "issues.json"),
        REPORTS_DIR=os.path.join(tmp, "reports"),
        KB_DIR=os.path.join(tmp, "no-kb"),
        GOOGLE_SPACE="spaces/MAIN",
        POLL_BACKFILL_SINCE="2020-01-01T00:00:00Z",
    )
    return replace(cfg, **over) if over else cfg


class NormalizeUserIdTest(unittest.TestCase):
    def test_bare_id_gets_users_prefix(self) -> None:
        self.assertEqual(_normalize_user_id("1234567890"), "users/1234567890")

    def test_full_form_passthrough(self) -> None:
        self.assertEqual(_normalize_user_id("users/1234567890"), "users/1234567890")

    def test_whitespace_trimmed(self) -> None:
        self.assertEqual(_normalize_user_id("  users/42  "), "users/42")
        self.assertEqual(_normalize_user_id("  42  "), "users/42")

    def test_blank_and_none_are_none(self) -> None:
        self.assertIsNone(_normalize_user_id(""))
        self.assertIsNone(_normalize_user_id("   "))
        self.assertIsNone(_normalize_user_id(None))


class ConfigLoadTest(unittest.TestCase):
    def test_load_config_reads_bot_user_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, ".env")
            with open(env, "w", encoding="utf-8") as fh:
                fh.write("GOOGLE_BOT_USER_ID=users/12345\n")
            cfg = load_config(env_file=env)
            self.assertEqual(cfg.GOOGLE_BOT_USER_ID, "users/12345")

    def test_default_bot_user_id_is_blank(self) -> None:
        cfg = load_config(env_file="no-such.env")
        self.assertEqual(cfg.GOOGLE_BOT_USER_ID, "")


class BuildRunnerSeedsSelfIdTest(unittest.TestCase):
    """`build_runner` must hand the live client its own id from cycle 1 so a fresh
    start (no persisted state) still self-filters."""

    def test_configured_id_seeds_client_me_on_fresh_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, GOOGLE_BOT_USER_ID="1234567890")  # bare id
            runner = build_runner(cfg)
            self.assertEqual(runner.chat.me(), "users/1234567890")

    def test_configured_id_takes_precedence_over_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, GOOGLE_BOT_USER_ID="users/from-config")
            # A different id already persisted from a prior run.
            seed_store = IssueStore(cfg.STATE_FILE)
            seed_store.load()
            seed_store.set_bot_user_id("users/persisted-old")
            seed_store.save()

            runner = build_runner(cfg)
            self.assertEqual(runner.chat.me(), "users/from-config")
            self.assertEqual(runner.store.get_bot_user_id(), "users/from-config")

    def test_falls_back_to_persisted_when_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp)  # GOOGLE_BOT_USER_ID blank
            seed_store = IssueStore(cfg.STATE_FILE)
            seed_store.load()
            seed_store.set_bot_user_id("users/persisted")
            seed_store.save()

            runner = build_runner(cfg)
            self.assertEqual(runner.chat.me(), "users/persisted")


class DetectionSelfFilterTest(unittest.TestCase):
    """The whole point: a known own id drops the bot's own messages from
    detection; an unknown own id (the bootstrap gap) lets them leak through."""

    def _run_detect(self, *, me, author):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            chat = FakeChatClient(me=me)
            chat.inject(author, SEED_TEXT)
            store = IssueStore(config.STATE_FILE)
            runner = Runner(
                chat, Analyzer(MockLLM(), retriever=None, top_k=0), store, config
            )
            return runner.run_cycle()["detected"]

    def test_bot_own_message_not_detected_when_id_known(self) -> None:
        self.assertEqual(self._run_detect(me=BOT_ID, author=BOT_ID), 0)

    def test_staff_message_still_detected_when_id_known(self) -> None:
        self.assertEqual(self._run_detect(me=BOT_ID, author=STAFF_ID), 1)

    def test_bootstrap_gap_bot_message_leaks_without_known_id(self) -> None:
        # Documents WHY GOOGLE_BOT_USER_ID exists: with no own id, the bot's own
        # message is detected (the self-loop). Seeding the id (above) closes it.
        self.assertEqual(self._run_detect(me=None, author=BOT_ID), 1)


if __name__ == "__main__":
    unittest.main()
