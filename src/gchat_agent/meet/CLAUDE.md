# meet/ — Google Meet link minting

Optional sink: mint a real Google Meet meeting via the Meet **REST** API
`spaces.create` and return its join link, so the bot can post a live-call link to
Chat and a HUMAN jumps onto the incident call. This is the *achievable* "AI phone
call": an AI **cannot speak on a Meet** — the Meet **Media** API is receive-only
(its scopes are `.readonly`, "Capture real-time audio/video") and Developer-
Preview-gated, so the REST `spaces.create` (mint a meeting + return a join URL) is
the only path. Gated by `MEET_LINKS` (off by default, so the offline/test path
needs no Meet). Mirrors `chat/` — stdlib `urllib` only; tests use
`tests/fakes.FakeMeetClient`, never the live client.

- **`base.py`** — `MeetClient` Protocol: a single `create_space()` → `MeetSpace`.
  `MeetSpace` (frozen dataclass): `name`, `meeting_uri` (the
  `https://meet.google.com/abc-mnop-xyz` join link), `meeting_code`, `raw`. The
  caller depends on this Protocol, never the concrete class, so the offline tests
  stay network/credential-free.
- **`rest.py`** — live `MeetRestClient` over stdlib `urllib` (Bearer token via
  `gchat_agent.chat.oauth`, bounded retry/backoff on 429/5xx, a single 401-reauth,
  `RuntimeError` on hard fail). `create_space()` POSTs `{api_url}/spaces`
  (`MEET_API_URL`, default `https://meet.googleapis.com/v2`). `build_meet(config)`
  → a client or `None` (links off). Needs the
  `https://www.googleapis.com/auth/meetings.space.created` OAuth scope (minted by
  `scripts/authorize.py`) or `spaces.create` returns 403.

The Meet REST reference is bundled under
[`docs/google_meet/`](../../../docs/google_meet/) (`api/` = REST, `media-api/` =
the receive-only Media API). Cross-cutting behavior (how the link is shared in
Chat) lives in the **root [`CLAUDE.md`](../../../CLAUDE.md)**.
