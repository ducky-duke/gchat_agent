"""Google Meet REST API integration — mint a meeting and get its join link.

`base.MeetClient` is the Protocol the demo/runner depends on; `rest.MeetRestClient`
is the live stdlib-`urllib` implementation (mirrors `chat.google_rest` /
`github.rest`). Tests use `tests.fakes.FakeMeetClient`, never the live client.

The achievable "AI phone call": an AI can't *speak* on a Meet (the Meet Media API
is receive-only + Developer-Preview-gated — see `docs/google_meet/`), but the REST
API's `spaces.create` mints a real meeting whose join link the bot posts into Chat
so a human joins a live incident call.
"""
from .base import MeetClient, MeetSpace

__all__ = ["MeetClient", "MeetSpace"]
