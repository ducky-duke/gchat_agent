# chat/ — Google Chat adapters

Ingress/egress to Google Chat over **stdlib `urllib`** (ported from `smoke/`) — no Google
client libraries, no service accounts; personal @gmail.com accounts via user OAuth. REST
reference is bundled at [`docs/google_chat/`](../../../docs/google_chat) — read it before
changing endpoints. Tests use `tests/fakes.FakeChatClient`, never the live client.

- **`base.py`** — `ChatClient` Protocol: `me`, `fetch_messages`, `post_message`,
  `post_reply`, `post_voice`.
- **`google_rest.py`** — live `GoogleChatClient`. Polling list (`orderBy` ASC matters),
  post message/reply, voice attachment (`_upload_attachment`: `media.upload` multipart +
  `attachment` create, both on the bot's `chat.messages` scope), retry/backoff
  (`_should_retry`/`_sleep_backoff`), and self-id resolution via OAuth tokeninfo
  (`_resolve_self_id` — the `sub` claim; makes self-filter work from cycle 1) +
  learn-from-first-post fallback (`_learn_self`). Takes an optional `space=` override
  (default `GOOGLE_SPACE`) so a 2nd instance can bind to the report DM
  (`GOOGLE_CHAT_REPORT_SPACE`) with the same bot token — used by the report-DM
  assistant. `_to_message` carries `annotations` through (e.g. a call's
  `meetSpaceLinkData`/`huddleStatus`) for missed-call detection.
- **`oauth.py`** — minimal stdlib user-OAuth refresh: `get_access_token()`, `_refresh()`,
  `invalidate()`. Testing-mode refresh tokens expire after 7 days (see root MEMORY.md).
- The webhook ingress client is **Phase-2 DEFERRED** (`scripts/run_webhook.py` is a stub);
  only the polling adapter is live.
