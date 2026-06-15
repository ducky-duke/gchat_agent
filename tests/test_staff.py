"""Tests for the staff personas (§5.8 + §12).

Exercises :class:`~gchat_agent.agent.staff.StaffAgent` and
:func:`~gchat_agent.agent.staff.load_personas` fully offline, injecting
:class:`~gchat_agent.llm.mock.MockLLM` + :class:`tests.fakes.FakeChatClient`
(no network, no Google/OpenRouter credentials).

The flows are deterministic because the MockLLM's ``chat`` returns only a generic
acknowledgement, so :class:`StaffAgent` falls back to the persona's literal
``facts`` text for each reply. Disclosure is progressive: one held fact per
reply, never repeated within a thread, and ``answer_question`` returns ``None``
once a persona has nothing left to add.
"""
from __future__ import annotations

import unittest

from gchat_agent.agent.staff import (
    _REQUIRED_KEYS,
    StaffAgent,
    load_personas,
)
from gchat_agent.llm.mock import _ISSUE_SIGNALS, MockLLM
from gchat_agent.models import Message
from tests.fakes import FakeChatClient

# Bundled scenario file the runner / live demo also use.
_SCENARIOS = "data/scenarios.json"


def _has_issue_signal(text: str) -> bool:
    """True if a message carries a detect-able issue signal or ends with '?'."""
    low = text.lower()
    return any(sig in low for sig in _ISSUE_SIGNALS) or text.rstrip().endswith("?")


class LoadPersonasTest(unittest.TestCase):
    def test_returns_ops_and_promo_with_required_keys(self) -> None:
        personas = load_personas(_SCENARIOS)
        # The two demo personas are present.
        self.assertIn("ops", personas)
        self.assertIn("promo", personas)
        # Every persona has the full required key set, correctly typed.
        for persona_id, persona in personas.items():
            for key in _REQUIRED_KEYS:
                self.assertIn(key, persona, f"{persona_id} missing {key}")
            self.assertIsInstance(persona["role"], str)
            self.assertTrue(persona["role"].strip(), f"{persona_id} empty role")
            self.assertIsInstance(persona["facts"], dict)
            self.assertTrue(persona["facts"], f"{persona_id} has no facts")
            self.assertIsInstance(persona["seed_messages"], list)
            self.assertTrue(persona["seed_messages"], f"{persona_id} no seeds")

    def test_seed_messages_carry_detectable_signals(self) -> None:
        # Seeds must trip the MockLLM detector so the bot has something to find.
        personas = load_personas(_SCENARIOS)
        for persona_id, persona in personas.items():
            self.assertTrue(
                any(_has_issue_signal(m) for m in persona["seed_messages"]),
                f"{persona_id} seed messages carry no issue signal",
            )

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_personas("data/does-not-exist.json")


class StaffSeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.personas = load_personas(_SCENARIOS)
        self.llm = MockLLM()

    def _agent(self, persona_id: str, me: str = "users/staff") -> StaffAgent:
        chat = FakeChatClient(me=me)
        return StaffAgent(self.llm, chat, self.personas[persona_id])

    def test_seed_posts_messages_visible_via_fetch(self) -> None:
        agent = self._agent("ops", me="users/staff-ops")
        posted = agent.seed()

        # seed() reports exactly the persona's seed messages, in order.
        expected = self.personas["ops"]["seed_messages"]
        self.assertEqual([m.text for m in posted], expected)

        # And they actually landed in the space (authored by the staff account).
        fetched = agent.chat.fetch_messages(None)
        self.assertEqual([m.text for m in fetched], expected)
        for message in fetched:
            self.assertIsInstance(message, Message)
            self.assertEqual(message.sender, "users/staff-ops")

    def test_seed_messages_share_one_thread(self) -> None:
        agent = self._agent("promo")
        posted = agent.seed()
        self.assertGreaterEqual(len(posted), 2)
        thread_ids = {m.thread_id for m in posted}
        self.assertEqual(len(thread_ids), 1, "seeds should form one thread")
        self.assertEqual(agent.seed_thread_id, posted[0].thread_id)

    def test_seed_is_idempotent_on_rerun(self) -> None:
        # Stable request_ids mean a re-run never double-posts.
        agent = self._agent("ops")
        agent.seed()
        agent.seed()
        n_seeds = len(self.personas["ops"]["seed_messages"])
        self.assertEqual(len(agent.chat.fetch_messages(None)), n_seeds)


class StaffAnswerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.personas = load_personas(_SCENARIOS)
        self.llm = MockLLM()

    def _seeded_agent(self, persona_id: str) -> StaffAgent:
        chat = FakeChatClient(me=f"users/staff-{persona_id}")
        agent = StaffAgent(self.llm, chat, self.personas[persona_id])
        agent.seed()
        return agent

    def test_answer_returns_nonempty_reply_in_thread(self) -> None:
        agent = self._seeded_agent("ops")
        reply = agent.answer_question(
            agent.seed_thread_id, "Who will own this and drive it to resolution?"
        )
        self.assertIsInstance(reply, Message)
        self.assertTrue(reply.text.strip(), "reply text must be non-empty")
        # Reply is posted into the bot's thread and is fetchable.
        self.assertEqual(reply.thread_id, agent.seed_thread_id)
        self.assertIn(reply, agent.chat.fetch_messages(None))

    def test_targeted_questions_reveal_matching_facts(self) -> None:
        # A question targeting a held fact surfaces that fact's literal text.
        facts = self.personas["ops"]["facts"]

        agent = self._seeded_agent("ops")
        owner = agent.answer_question(agent.seed_thread_id, "Who will own this?")
        self.assertEqual(owner.text, facts["owner"])

        agent = self._seeded_agent("ops")
        deadline = agent.answer_question(
            agent.seed_thread_id, "When is the firm deadline or target date?"
        )
        self.assertEqual(deadline.text, facts["deadline"])

        agent = self._seeded_agent("ops")
        numbers = agent.answer_question(
            agent.seed_thread_id, "How many are stuck and what rate of failures?"
        )
        self.assertEqual(numbers.text, facts["numbers"])

    def test_progressive_disclosure_no_repeats(self) -> None:
        # Each generic question reveals a new, distinct held fact.
        agent = self._seeded_agent("ops")
        seen: list[str] = []
        for _ in range(3):
            reply = agent.answer_question(agent.seed_thread_id, "Can you tell me more?")
            self.assertIsNotNone(reply)
            self.assertNotIn(reply.text, seen, "fact disclosed twice in one thread")
            seen.append(reply.text)
        # Every disclosed reply is a real held fact value.
        held = set(self.personas["ops"]["facts"].values())
        for text in seen:
            self.assertIn(text, held)

    def test_answer_returns_none_when_exhausted(self) -> None:
        # Once every held fact is disclosed, the persona stays quiet (None).
        agent = self._seeded_agent("promo")
        n_facts = len(self.personas["promo"]["facts"])
        revealed = 0
        for _ in range(n_facts):
            reply = agent.answer_question(agent.seed_thread_id, "Anything else to add?")
            self.assertIsNotNone(reply)
            revealed += 1
        self.assertEqual(revealed, n_facts)
        # Nothing left to disclose -> None, and no extra message posted.
        before = len(agent.chat.fetch_messages(None))
        self.assertIsNone(
            agent.answer_question(agent.seed_thread_id, "Anything else to add?")
        )
        self.assertEqual(len(agent.chat.fetch_messages(None)), before)

    def test_answer_none_when_persona_has_no_facts(self) -> None:
        chat = FakeChatClient(me="users/staff")
        agent = StaffAgent(
            self.llm,
            chat,
            {"role": "r", "facts": {}, "withholding_policy": "", "seed_messages": []},
        )
        self.assertIsNone(agent.answer_question("spaces/FAKE/threads/t1", "Who owns it?"))

    def test_seeded_thread_reaches_clarity_after_revealing_facts(self) -> None:
        # The disclosed facts together supply owner + date + number, so the
        # MockLLM's clarity check would flip to clear over the thread transcript.
        agent = self._seeded_agent("ops")
        for _ in range(len(self.personas["ops"]["facts"])):
            if agent.answer_question(agent.seed_thread_id, "Tell me more?") is None:
                break
        transcript = " ".join(m.text for m in agent.chat.fetch_messages(None)).lower()
        from gchat_agent.llm.mock import _DATE_PATTERN, _NUMBER_PATTERN, _OWNER_HINTS

        self.assertTrue(any(h in transcript for h in _OWNER_HINTS))
        self.assertTrue(_DATE_PATTERN.search(transcript))
        self.assertTrue(_NUMBER_PATTERN.search(transcript))

    def test_persona_system_prompt_includes_role_and_facts(self) -> None:
        agent = self._seeded_agent("ops")
        prompt = agent.persona_system_prompt()
        self.assertIn(agent.role, prompt)
        # Holds at least one of its facts and the withholding policy.
        self.assertTrue(any(v in prompt for v in agent.facts.values()))
        self.assertIn(agent.withholding_policy, prompt)


class DedupeRepeatTest(unittest.TestCase):
    """A verbatim doubled model reply collapses to a single copy (minimax quirk)."""

    def test_collapses_exact_and_separated_doubles(self) -> None:
        d = StaffAgent._dedupe_repeat
        self.assertEqual(d("Owner is Sam.Owner is Sam."), "Owner is Sam.")
        self.assertEqual(d("Owner is Sam. Owner is Sam."), "Owner is Sam.")
        self.assertEqual(d("Just once."), "Just once.")  # not doubled, untouched
        self.assertEqual(d(""), "")


if __name__ == "__main__":
    unittest.main()
