"""Tests for the in-memory `FakeChatClient` test double (§5.4/§12)."""
from __future__ import annotations

import unittest

from gchat_agent.chat.base import ChatClient
from gchat_agent.models import SenderType
from tests.fakes import FakeChatClient


class FakeChatClientTest(unittest.TestCase):
    def test_satisfies_chatclient_protocol(self) -> None:
        self.assertIsInstance(FakeChatClient(), ChatClient)

    def test_me_returns_configured_id(self) -> None:
        self.assertEqual(FakeChatClient(me="users/bot").me(), "users/bot")
        self.assertIsNone(FakeChatClient(me=None).me())

    def test_post_and_reply_fetched_in_order(self) -> None:
        client = FakeChatClient(me="users/bot")
        first = client.post_message("hello")
        reply = client.post_reply(first, "world")

        # Reply lands in the same thread as the message it replies to.
        self.assertEqual(reply.thread_id, first.thread_id)
        self.assertEqual(first.sender, "users/bot")
        self.assertEqual(first.sender_type, SenderType.HUMAN)

        fetched = client.fetch_messages(None)
        self.assertEqual([m.text for m in fetched], ["hello", "world"])
        # Chronological + strictly increasing create_time.
        self.assertLess(fetched[0].create_time, fetched[1].create_time)
        self.assertNotEqual(fetched[0].id, fetched[1].id)

    def test_post_message_new_thread_when_none(self) -> None:
        client = FakeChatClient()
        a = client.post_message("a")
        b = client.post_message("b")
        # Each top-level post gets its own fresh thread.
        self.assertNotEqual(a.thread_id, b.thread_id)
        self.assertTrue(a.thread_id)

    def test_request_id_idempotent_no_double_append(self) -> None:
        client = FakeChatClient()
        m1 = client.post_message("once", request_id="r1")
        m2 = client.post_message("once-again", request_id="r1")
        # Same prior Message returned; nothing new appended.
        self.assertIs(m1, m2)
        self.assertEqual(len(client.fetch_messages(None)), 1)
        # A different request_id does append.
        m3 = client.post_message("twice", request_id="r2")
        self.assertEqual(len(client.fetch_messages(None)), 2)
        self.assertIsNot(m1, m3)

    def test_reply_request_id_idempotent(self) -> None:
        client = FakeChatClient()
        root = client.post_message("root")
        r1 = client.post_reply(root, "ans", request_id="q1")
        r2 = client.post_reply(root, "ans", request_id="q1")
        self.assertIs(r1, r2)
        self.assertEqual(len(client.fetch_messages(None)), 2)  # root + 1 reply

    def test_fetch_since_filters_strictly(self) -> None:
        client = FakeChatClient()
        a = client.post_message("a")
        b = client.post_message("b")
        # `since` == a.create_time is exclusive (createTime > since).
        after_a = client.fetch_messages(a.create_time)
        self.assertEqual([m.text for m in after_a], ["b"])
        self.assertEqual(after_a[0].id, b.id)

    def test_inject_and_seed_arbitrary_sender(self) -> None:
        client = FakeChatClient(me="users/bot")
        injected = client.inject("users/staff-1", "we have an outage", thread_id=None)
        self.assertEqual(injected.sender, "users/staff-1")
        self.assertEqual(injected.sender_type, SenderType.HUMAN)

        # seed() is an alias and can reuse a thread.
        seeded = client.seed(
            "users/staff-2", "more detail", thread_id=injected.thread_id
        )
        self.assertEqual(seeded.thread_id, injected.thread_id)
        self.assertEqual(seeded.sender, "users/staff-2")

        # App-typed injection is supported too.
        app_msg = client.inject(
            "users/other-bot", "ping", sender_type=SenderType.APP
        )
        self.assertEqual(app_msg.sender_type, SenderType.APP)

        texts = [m.text for m in client.fetch_messages(None)]
        self.assertEqual(texts, ["we have an outage", "more detail", "ping"])

    def test_deterministic_ids_and_times(self) -> None:
        a = FakeChatClient(space="spaces/X")
        b = FakeChatClient(space="spaces/X")
        ma = a.post_message("hi")
        mb = b.post_message("hi")
        self.assertEqual(ma.id, mb.id)
        self.assertEqual(ma.create_time, mb.create_time)


if __name__ == "__main__":
    unittest.main()
