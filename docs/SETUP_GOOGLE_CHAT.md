# Live demo setup — Google Chat (user OAuth)

End-to-end runbook for the **live** demo: 3 personal Gmail accounts in **one**
Google Chat space, each driven by its own user-OAuth refresh token — 1 bot
(issue-spotter) + 2 staff personas (`ops`, `promo`). **No Workspace, no admin,
no service account, no app auth.** Posts are attributed to the consenting Gmail
user (`sender.type=HUMAN`, text-only).

Validated live on 2026-06-13 with `mikmikb26@gmail.com` on GCP project
`chat-smoke-1781346315` (list/create space, post/list messages — all HTTP 200).

> **You only need the live setup for the live demo.** To exercise the bot fully
> offline (145 tests, MockLLM + in-memory fake chat), set `LLM_PROVIDER=mock` —
> no GCP, no OAuth, no key. See the project README. The steps below are for
> driving a *real* Chat space.

> **Don't have 3 Gmail accounts?** A 2-account fallback (1 bot + 1 staff) is
> fine — run a single `scripts/run_staff.py` (e.g. `--persona ops`) and skip the
> other. Everything else is identical.

All commands are run **from the repo root** with the project's interpreter.
Activate the `igaming` conda env (Python 3.14) first, then `$PY` below is just
`python`:

```bash
PY=python   # the project interpreter (activate the 'igaming' conda env, Python 3.14)
```

The Google Chat + user-OAuth path is **pure stdlib `urllib`** — no `google-auth`
needed (that extra is only for the deferred Phase-2 webhook). `run_poller.py` and
`run_staff.py` self-insert `src/` on `sys.path`, so they run straight from the
checkout without installing the package; `authorize.py` does **not** — run it with
`PYTHONPATH=src` (as the examples below show).

---

## 1. Create / choose a GCP project, enable the Chat API, configure the (dormant) Chat app

1. In the [Cloud console](https://console.cloud.google.com/) create (or pick) a
   **personal** project, e.g. `chat-smoke-1781346315`. Note its **project id** —
   you'll set it as `GOOGLE_QUOTA_PROJECT`.
2. Enable the **Google Chat API**: *APIs & Services → Library → "Google Chat
   API" → Enable*.
3. Configure a **Chat app** (this is mandatory even though we never use app auth
   — see Gotcha 2): *Chat API → Configuration* tab. Fill in **App name**,
   **Avatar URL**, and **Description**. You do **not** need to enable
   interactivity, slash commands, or a webhook — leave the app dormant. Save.

## 2. Create a Desktop OAuth client

Use your **own** OAuth client of type **Desktop app** — gcloud's built-in client
is blocked from Chat scopes (Gotcha 1).

1. *APIs & Services → Credentials → Create credentials → OAuth client ID*.
2. **Application type: Desktop app**. Name it anything. Create.
3. **Download the JSON** (it has an `installed` section with `client_id` /
   `client_secret`). Save it in the repo, e.g. `secrets/oauth_client.json`
   (gitignored). This single client is **shared** by all 3 accounts.

## 3. Consent screen: External + Testing, add the 3 Gmail accounts as test users

1. *APIs & Services → OAuth consent screen*: **User type = External**. Fill in
   the required app name / support email. **Publishing status = Testing** (do not
   publish for the demo).
2. Under **Test users**, add **all 3 Gmail addresses** (bot + ops + promo).
   Only test users can consent while the app is in Testing mode.
3. Scopes need not be pre-added here — `scripts/authorize.py` requests them at
   consent time: `chat.messages`, `chat.messages.readonly`,
   `chat.spaces.create`, and `userinfo.email`.

> Testing-mode refresh tokens **expire after 7 days** (Gotcha 3). Re-run
> step 4 weekly for an ongoing demo, or publish the app.

## 4. Mint a refresh token for EACH account (`scripts/authorize.py`)

Run the loopback consent **once per account**, in a browser signed into **that
account** (use Incognito or `authuser=` — Gotcha 5). Each run writes a
per-account refresh-token JSON (`{"refresh_token", "token_uri", "client_id"}`)
that `chat/oauth.py` later swaps for short-lived bearers.

`scripts/authorize.py` flags (verified against `--help`):

| Flag | Meaning | Default |
| --- | --- | --- |
| `--client` | Desktop OAuth client_secret JSON (from step 2) | from config (`GOOGLE_OAUTH_CLIENT`); falls back to `secrets/oauth_client.json` |
| `--out` | where to write the refresh-token JSON | from config (`GOOGLE_TOKEN_FILE`); falls back to `secrets/token_bot.json` |
| `--account` | `login_hint` — pre-selects this Gmail in the consent screen | (none) |

The `--client`/`--out` defaults are **not** hard-coded paths: `authorize.py` reads
them from `load_config()`, so an existing `.env` (`GOOGLE_OAUTH_CLIENT` /
`GOOGLE_TOKEN_FILE`) changes what they default to. Pass the flags explicitly (as
below) to be unambiguous.

Mint one token per account:

```bash
# Bot account
PYTHONPATH=src $PY scripts/authorize.py \
  --client secrets/oauth_client.json \
  --out    secrets/token_bot.json \
  --account bot@gmail.com

# Ops staff account
PYTHONPATH=src $PY scripts/authorize.py \
  --client secrets/oauth_client.json \
  --out    secrets/token_ops.json \
  --account ops@gmail.com

# Promo staff account
PYTHONPATH=src $PY scripts/authorize.py \
  --client secrets/oauth_client.json \
  --out    secrets/token_promo.json \
  --account promo@gmail.com
```

For each run: a browser opens to the consent URL (or paste the printed URL). At
**"Google hasn't verified this app"** click **Advanced → Continue** (test users
get a warning, not a block). The script captures the redirect on a loopback
port, exchanges the code, confirms the account via `tokeninfo`, and writes the
token JSON (chmod `0600`). It **refuses a `glo.com` account** by design.

> If it reports `No refresh_token returned`, Google already issued one for this
> client+account. Revoke at <https://myaccount.google.com/permissions> and
> re-run (the flow forces `prompt=consent` to get a fresh refresh token).

## 5. Create a threaded SPACE and add all 3 accounts

Create a **named space** (`spaceType=SPACE`) — a created `SPACE` is threaded
(`spaceThreadingState=THREADED_MESSAGES`) by default, which is required so the
bot's in-thread replies (`messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD`)
work.

Easiest path: in the **Google Chat UI** (signed into the bot account) create a
new **Space** (not a group DM), then **invite the other 2 Gmail accounts** as
members. Each must accept so they're members (user OAuth only sees spaces the
user belongs to).

Note the space resource name — it looks like `spaces/AAQAxxxxxxx`. That's your
`GOOGLE_SPACE`. (You can also create it programmatically via `spaces.create`
with the `chat.spaces.create` scope. Adding members programmatically with
`spaces.members.create` additionally needs the `chat.memberships` scope, which
`scripts/authorize.py` does **not** request — so use the Chat UI invite above,
the documented default, unless you add that scope to the script.)

## 6. Configure `.env`

Copy `.env.example` to `.env` and set the live keys (every key is documented in
`.env.example`):

```bash
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...                 # required for the live demo
OPENROUTER_MODEL=deepseek/deepseek-v4-flash  # or your model of choice

GOOGLE_SPACE=spaces/AAQAxxxxxxx              # from step 5
GOOGLE_OAUTH_CLIENT=secrets/oauth_client.json
GOOGLE_TOKEN_FILE=secrets/token_bot.json     # the BOT's token (default account)
GOOGLE_QUOTA_PROJECT=chat-smoke-1781346315   # project id with Chat API enabled (Gotcha 4)
```

Notes:
- `GOOGLE_QUOTA_PROJECT` is sent as the `x-goog-user-project` header — set it to
  the project id where you enabled the Chat API.
- `GOOGLE_TOKEN_FILE` here is the **bot's** token (`run_poller.py` uses it).
  Staff processes override it via `--token` (step 7), so each staff posts as its
  own account.
- `OPENROUTER_API_KEY` is **required** for the live demo: `build_llm` raises a
  clear `RuntimeError` if `LLM_PROVIDER=openrouter` and no key is set. (To run
  offline, set `LLM_PROVIDER=mock`.)

## 7. Run the bot and the staff personas

Open 3 terminals (or run with `--once` to step through manually). Each process
prints a startup banner (space, provider/model, token, poll interval).

**Bot** — the issue-spotter poller (uses `GOOGLE_TOKEN_FILE` from `.env`):

```bash
PYTHONPATH=src $PY scripts/run_poller.py            # loop forever (single-runner lock)
# or one cycle:
PYTHONPATH=src $PY scripts/run_poller.py --once
```

**Ops staff** — seeds the flaky-payout-webhook scenario, then answers the bot:

```bash
PYTHONPATH=src $PY scripts/run_staff.py --persona ops --token secrets/token_ops.json
```

**Promo staff** — seeds the vague-launch-request scenario, then answers the bot:

```bash
PYTHONPATH=src $PY scripts/run_staff.py --persona promo --token secrets/token_promo.json
```

`scripts/run_staff.py` requires both `--persona {ops,promo}` (selects the entry
in `data/scenarios.json`) and `--token <path>` (overrides `GOOGLE_TOKEN_FILE` so
the process posts as that persona's Gmail). Add `--once` to seed and run a single
answer pass.

**Run order matters — start the bot first.** On its true first run with no
`POLL_BACKFILL_SINCE`, the bot pins its cursor to *now* (no history backfill), so
it will **not** see messages posted *before* it started. So **start the bot
first** (its cursor pins to now), **then** run the staff personas — their seeds
arrive afterward and are fetched on the next poll. If you would rather start the
staff first, you **must** set `POLL_BACKFILL_SINCE` (RFC-3339) to a timestamp
just before the demo so the bot backfills the already-seeded threads on its first
poll; otherwise it misses them.

### What to watch for

- In the **Chat space UI**: each staff persona posts 1–2 seed messages, the bot
  detects an issue and replies **in-thread** with a clarifying question, the
  staff persona answers one held fact per reply, and the loop continues until the
  bot has enough (up to `MAX_CLARIFY_ROUNDS`, default 3).
- On **resolution**, the bot writes a Markdown report to
  `reports/issue-<id>.md` and posts a **one-line confirmation** into the thread.
- **Terminal banners**: confirm `space:` is your `GOOGLE_SPACE` and `provider:`
  shows your OpenRouter model (not `(unset GOOGLE_SPACE)`).
- The bot drops only **its own** messages (its `users/<id>`), so it processes
  both staff personas' threads. State persists in `STATE_FILE`
  (`.state/issues.json`) — delete it to reset between demo runs.
- `scripts/run_webhook.py` is a **Phase-2 stub** that just prints a deferral
  notice; the webhook ingress is not built in v1. Use `run_poller.py` instead.

---

## Gotchas (hard-won — read before debugging)

1. **Use your OWN OAuth client of type "Desktop app" in a personal GCP project.**
   gcloud's built-in client is **blocked** from Chat scopes ("This app is
   blocked"). Create your own (step 2); as a test user you'll only get the
   "unverified app" warning (Advanced → Continue), not a block.

2. **The GCP project MUST have a Chat app configured** (Cloud console → *Chat API
   → Configuration*: app name + avatar + description). Without it, **every** Chat
   API call — even a user-auth read — returns `404 "Google Chat app not found"`.
   The app config is **dormant** (we never use app auth or interactivity).

3. **OAuth consent screen = External + Testing**; add all 3 Gmail accounts as
   **test users**. Testing-mode refresh tokens **expire after 7 days** →
   re-consent weekly via `scripts/authorize.py` (or publish the app).

4. **Quota project.** Set `GOOGLE_QUOTA_PROJECT` to the project id with the Chat
   API enabled; it is sent as the `x-goog-user-project` header on every call.

5. **Browser account ≠ gcloud account.** Do the console work and the OAuth
   consent in an **Incognito** window signed into the **target Gmail**, or append
   `authuser=<email>` to console URLs — otherwise you'll consent as the wrong
   account.

### Reference facts (from the live validation)

- Validated live on **2026-06-13** with `mikmikb26@gmail.com` on project
  `chat-smoke-1781346315` (list/create space, post/list messages — all HTTP
  200). Smoke tooling lives in `smoke/`.
- **Scopes** requested at consent: `chat.messages` (read+write),
  `chat.messages.readonly`, `chat.spaces.create` (plus `userinfo.email` to
  confirm/refuse the consenting account).
- The space must be **`spaceType=SPACE`** with
  **`spaceThreadingState=THREADED_MESSAGES`** (a created `SPACE` is threaded by
  default) so `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` works.
- **2-account fallback** (1 bot + 1 staff) is fine if making 3 Gmail accounts is
  a hassle — run a single `scripts/run_staff.py` and skip the other persona.
