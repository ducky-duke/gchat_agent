"""Test-only in-memory `ChatClient` (§5.4/§12).

`FakeChatClient` satisfies the `chat.base.ChatClient` protocol exactly but keeps
every message in a plain Python list, so the loop / analyzer / staff tests run
fully offline with no network and no Google credentials. It is deterministic:
ids come from a monotonic counter and `create_time` is derived from that counter
(no wall-clock, no randomness), so a test produces the same transcript every run.

Behaviour mirrors `google_rest.GoogleChatClient` where it matters:
- `me()` returns the configurable own id (constructor arg).
- `post_message` / `post_reply` append a `Message` authored by `me`, in a thread
  (the supplied/replied thread, or a freshly minted one).
- Idempotency: a `request_id` that was already used returns the *same* prior
  `Message` instead of appending, so a retry never double-posts.
- `fetch_messages(since)` returns messages with `create_time > since` (all if
  `None`) in chronological order.

`inject()` / `seed()` drop in messages authored by an arbitrary sender (e.g. a
staff `users/<id>`) so tests and the staff driver can populate the space.
"""
from __future__ import annotations

from gchat_agent.models import Message, SenderType

# A fixed epoch so derived timestamps are stable across runs but still sort
# correctly and stay strictly increasing with the monotonic counter.
_EPOCH = "2026-01-01T00:00:00"


class FakeChatClient:
    """In-memory `ChatClient` for offline tests (implements the protocol)."""

    def __init__(
        self,
        me: str | None = "users/bot",
        space: str = "spaces/FAKE",
    ) -> None:
        self._me = me
        self.space = space
        self.messages: list[Message] = []
        # Monotonic counters drive deterministic ids + create_time (no clock).
        self._counter = 0
        self._thread_counter = 0
        # request_id -> already-created Message (idempotency / retry de-dupe).
        self._by_request: dict[str, Message] = {}

    # --- deterministic id / time helpers -----------------------------------
    def _next_seq(self) -> int:
        self._counter += 1
        return self._counter

    def _next_message_id(self, seq: int) -> str:
        return f"{self.space}/messages/m{seq}"

    def _new_thread_id(self) -> str:
        self._thread_counter += 1
        return f"{self.space}/threads/t{self._thread_counter}"

    @staticmethod
    def _create_time(seq: int) -> str:
        """Derive a strictly-increasing RFC-3339 timestamp from the sequence.

        Encodes the monotonic counter into the fractional seconds so each
        message's `create_time` is unique and chronologically ordered, while
        staying entirely independent of the wall clock."""
        return f"{_EPOCH}.{seq:09d}Z"

    # --- ChatClient protocol ----------------------------------------------
    def me(self) -> str | None:
        """Own `users/<id>` resource name (configured at construction)."""
        return self._me

    def fetch_messages(self, since: str | None) -> list[Message]:
        """Messages with `create_time > since` (all if `None`), chronological.

        Stored order is already chronological (append-only with monotonic
        timestamps), so a stable string comparison on the RFC-3339 timestamps
        matches the live adapter's `createTime >` filter semantics."""
        if since is None:
            return list(self.messages)
        return [m for m in self.messages if m.create_time > since]

    def post_message(
        self,
        text: str,
        thread_id: str | None = None,
        request_id: str | None = None,
    ) -> Message:
        """Append a message authored by `me`, optionally into `thread_id`.

        A repeated `request_id` returns the previously-created `Message` without
        appending (idempotency), matching the live `requestId` contract."""
        if request_id is not None and request_id in self._by_request:
            return self._by_request[request_id]

        seq = self._next_seq()
        thread = thread_id or self._new_thread_id()
        message = Message(
            id=self._next_message_id(seq),
            space=self.space,
            thread_id=thread,
            sender=self._me or "",
            sender_type=SenderType.HUMAN,
            text=text,
            create_time=self._create_time(seq),
        )
        self.messages.append(message)
        if request_id is not None:
            self._by_request[request_id] = message
        return message

    def post_reply(
        self,
        message: Message,
        text: str,
        request_id: str | None = None,
    ) -> Message:
        """Reply to `message` in its thread (idempotent on `request_id`)."""
        return self.post_message(
            text, thread_id=message.thread_id, request_id=request_id
        )

    # --- test convenience --------------------------------------------------
    def inject(
        self,
        sender: str,
        text: str,
        thread_id: str | None = None,
        sender_type: SenderType = SenderType.HUMAN,
    ) -> Message:
        """Drop in a message authored by an arbitrary `sender` (e.g. a staff
        `users/<id>`), in `thread_id` or a fresh thread. Deterministic id +
        create_time, appended in order. Returns the created `Message`."""
        seq = self._next_seq()
        thread = thread_id or self._new_thread_id()
        message = Message(
            id=self._next_message_id(seq),
            space=self.space,
            thread_id=thread,
            sender=sender,
            sender_type=sender_type,
            text=text,
            create_time=self._create_time(seq),
        )
        self.messages.append(message)
        return message

    # Alias so tests can read either name.
    def seed(
        self,
        sender: str,
        text: str,
        thread_id: str | None = None,
        sender_type: SenderType = SenderType.HUMAN,
    ) -> Message:
        """Alias for `inject` (seed a message from an arbitrary sender)."""
        return self.inject(sender, text, thread_id=thread_id, sender_type=sender_type)


class StaffChatView:
    """A `ChatClient` view over a *shared* `FakeChatClient` (per-participant
    identity). Mirrors the live demo where each account points at one Space:
    reads/writes delegate to the shared backend so every message is mutually
    visible, but posts are authored as `me` — so the bot sees a distinct human
    and never self-filters them (§5.7/§6). Honors `request_id` idempotency like
    the real adapter so a retry never double-posts.

    Shared by the end-to-end loop test and the local demo script (single source
    of truth — the two previously carried verbatim copies that could drift)."""

    def __init__(self, backend: FakeChatClient, me: str) -> None:
        self._backend = backend
        self._me = me
        self._by_request: dict[str, Message] = {}

    def me(self) -> str | None:
        return self._me

    def fetch_messages(self, since: str | None) -> list[Message]:
        return self._backend.fetch_messages(since)

    def post_message(
        self,
        text: str,
        thread_id: str | None = None,
        request_id: str | None = None,
    ) -> Message:
        if request_id is not None and request_id in self._by_request:
            return self._by_request[request_id]
        message = self._backend.inject(self._me, text, thread_id=thread_id)
        if request_id is not None:
            self._by_request[request_id] = message
        return message

    def post_reply(
        self, message: Message, text: str, request_id: str | None = None
    ) -> Message:
        return self.post_message(text, thread_id=message.thread_id, request_id=request_id)
