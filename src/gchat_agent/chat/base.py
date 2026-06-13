"""Chat client protocol (§5.4).

One interface, several adapters: the Google REST poller (v1 live path), the
Phase-2 webhook, and a test-only in-memory `FakeChatClient` (lives under tests/).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Message


@runtime_checkable
class ChatClient(Protocol):
    """Read recent messages and post (threaded) replies to a Chat space."""

    def me(self) -> str | None:
        """Return this client's own user resource name (`users/<id>`) so the
        runner can filter out the bot's *own* messages before detection
        (§5.7/§6) — never a blanket `sender_type` rule, which would hide the
        staff personas. `None` if not yet known; the Google adapter learns it
        from its first posted message (or a profile lookup)."""
        ...

    def fetch_messages(self, since: str | None) -> list[Message]:
        """Return messages created after `since` (RFC-3339 timestamp or an
        adapter-specific cursor; `None` ⇒ from the current start point), in
        chronological order."""
        ...

    def post_message(
        self,
        text: str,
        thread_id: str | None = None,
        request_id: str | None = None,
    ) -> Message:
        """Post `text` as a new message, optionally into an existing thread.
        `request_id` is a stable idempotency key (§5.4/§5.7, e.g.
        `client-issue-{id}-r{n}`) so a retry never double-posts. Returns the
        created `Message`."""
        ...

    def post_reply(
        self,
        message: Message,
        text: str,
        request_id: str | None = None,
    ) -> Message:
        """Post `text` as a threaded reply to `message`. `request_id` is the
        stable idempotency key (see `post_message`). Returns the created
        `Message`."""
        ...
