# gchat-agent

A Google Chat **issue-spotter** bot demo. Three AI agents share one real Chat
space: two LLM-driven *staff* personas seed problems and answer questions in
character, and one *detector* bot watches the conversation, spots an emerging
issue, asks clarifying questions until the issue is well-understood, then writes
a Markdown resolution report and posts a one-line confirmation back into the
thread.

The demo runs across **three personal Gmail accounts** (1 bot + 2 staff: an
`ops` engineer and a `promo` manager) in **one** Google Chat space, all via user
OAuth. No Google Workspace, no admin, no service account.

## How it works

Each poll cycle the bot fetches new messages since its cursor, drops its own
posts, and asks the LLM to **detect** candidate issues over a recent window. For
every open issue it captures any fresh replies as Q&A, then re-assesses clarity:
if the issue is clear and confident enough it **resolves** (writes the report,
posts a confirmation, tombstones the fingerprint so it isn't re-raised);
otherwise it **asks** the next clarifying question (bounded by
`MAX_CLARIFY_ROUNDS`, gated on a new reply to avoid spam), or lets the issue go
**stale** after enough idle cycles. State persists atomically through an
`IssueStore`, and `run_forever` holds a single-runner lock file. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

## Install

The package uses a `src/` layout. You can either install it editable or just run
straight from the checkout with `PYTHONPATH=src`.

```bash
# Activate the project env first (the 'igaming' conda env, Python 3.14), so
# `python` below is the project interpreter.

# Option A — editable install (optional)
python -m pip install -e .

# Option B — no install; every script and the tests run with PYTHONPATH=src
```

The core LLM dependency is **`google-genai`** (the active Gemini transport;
**`openai`** is kept for the legacy OpenRouter path). Both SDKs are lazy-imported,
so the mock/test path needs neither the package at import time nor a key. Optional
extras:

```bash
pip install -e ".[observability]"   # langfuse — LLM/agent tracing
pip install -e ".[google]"          # google-auth — Phase-2 webhook token check
pip install -e ".[embeddings]"      # required for RAG_DENSE=true (off by default):
                                    # installs sentence-transformers (pulls torch)
```

## Configure

Copy the template and edit as needed:

```bash
cp .env.example .env
```

`config.load_config()` reads `.env` (`KEY=VALUE`, with inline `# comments` and
surrounding quotes stripped) and overlays `os.environ` on top; any key absent
from both falls back to a built-in default. `.env.example` documents every key.

For the **offline / mock path** you only need:

```bash
LLM_PROVIDER=mock
```

`mock` uses a deterministic in-process LLM — no API key, no network. The default
provider is **`gemini`** (the Gemini API via `google-genai`); selecting it without
`GEMINI_API_KEY` set makes `build_llm` raise a clear `RuntimeError`. `GEMINI_API_KEY`
is the **same key** the Gemini Live voice call uses — one Google key for the whole
project. The model is `GEMINI_MODEL` (default `gemini-3.5-flash`); per the Gemini 3.x
guidance the sampling knobs (`temperature`/`top_p`/`top_k`) are intentionally not set,
and reasoning depth is tuned via `GEMINI_THINKING_LEVEL` (`minimal`|`low`|`medium`|
`high`; blank = the model default).

The legacy `openrouter` provider (the `openai` SDK pointed at OpenRouter) is kept for
reference but is no longer the default; it needs `OPENROUTER_API_KEY` and its own
`OPENROUTER_MODEL`/`OPENROUTER_REASONING`/`OPENROUTER_QUANTIZATIONS` knobs.

## Run the tests

The functional gate. 145 tests, fully offline (a mock LLM + a fake chat client),
no network and no API key. Run from the repo root with the `igaming` conda env
(Python 3.14) activated:

```bash
PYTHONPATH=src python -m unittest discover -s tests -t . -p "test_*.py"
```

Ends with `OK` when green. (`ty` is not installed in this env — syntax-check a
file with `python -m py_compile <file>`.)

## Run the live 3-agent demo

The live demo needs a real Google Chat space, three Gmail accounts, and a
per-account OAuth token for each. Full walkthrough (OAuth client, space setup,
minting tokens with `scripts/authorize.py`): see
[docs/SETUP_GOOGLE_CHAT.md](docs/SETUP_GOOGLE_CHAT.md).

Once `.env` points at your space and each account has a token JSON, run the
three agents (one per account) in separate terminals:

These two scripts self-insert `src/` on `sys.path`, but `PYTHONPATH=src` is shown
for consistency with the rest of the docs:

```bash
# the detector bot (uses GOOGLE_TOKEN_FILE from .env)
PYTHONPATH=src python scripts/run_poller.py            # loop forever (single-runner lock)
PYTHONPATH=src python scripts/run_poller.py --once     # one cycle, then exit

# the two staff personas (each posts as its own account's token)
PYTHONPATH=src python scripts/run_staff.py --persona ops   --token secrets/token_ops.json
PYTHONPATH=src python scripts/run_staff.py --persona promo --token secrets/token_promo.json --once
```

`--persona` selects the entry in `data/scenarios.json` (`ops` = a flaky Skrill
payout webhook; `promo` = a vague welcome-bonus launch request); `--token`
overrides `GOOGLE_TOKEN_FILE` so that process posts as the right Gmail account.
Resolution reports land in `reports/issue-<id>.md`.

> **Self-filtering is automatic.** The bot drops its OWN account's messages from
> detection (so it never clarifies itself). It resolves its own `users/<id>` from
> the OAuth `tokeninfo` endpoint on startup — using only the `userinfo.email`
> scope the demo already grants — so this works from the first poll cycle without
> any setup. `GOOGLE_BOT_USER_ID` is an **optional** override: pin the account's
> `users/<id>` to skip the one startup lookup. The banner's `self:` line shows
> whether it's pinned or auto-detecting.

## Voice reports (text-to-speech) — legacy

> **Note:** Spoken delivery is now the **Gemini Live call on resolve**
> (`CALL_ON_RESOLVE`), which relays the clarified incident to a human over a real
> Chat call. The TTS voice report below rode the OpenRouter/`openai` transport and is
> retired: keep `REPORT_DELIVERY=disk`. On the default `gemini` provider `build_tts`
> returns `None` (graceful disk fallback); the table below applies only to the legacy
> `LLM_PROVIDER=openrouter` path, which is kept for reference.

Instead of (or alongside) the on-disk Markdown, a resolved issue can be delivered
as a **spoken audio note** posted to Google Chat. Set `REPORT_DELIVERY` in `.env`:

| `REPORT_DELIVERY` | Behaviour |
|---|---|
| `disk` (default) | Write Markdown to `reports/issue-<id>.md`. |
| `voice` | Synthesize a concise spoken summary (TTS) and post it as an MP3 attachment. Falls back to disk if voice delivery is unavailable or fails, so a report is never lost. |
| `both` | Write the Markdown **and** post the voice attachment. |

The voice goes to `GOOGLE_VOICE_SPACE` — a separate space, or a DM with another
account (the bot must be a member); leave it empty to post the voice into the
issue's own thread instead. TTS runs over OpenRouter's `audio.speech` endpoint
(same key/transport as the LLM): `TTS_MODEL` (default `x-ai/grok-voice-tts-1.0`)
and `TTS_VOICE` (model-specific — change it if a model rejects the default).

Two REST steps on the bot's user OAuth deliver it — `media.upload` (covered by the
`chat.messages` scope) returns an attachment token, then `spaces.messages.create`
attaches it; the audio never touches disk. Try it offline (saves the MP3s under
`reports/demo/` so you can play them):

```bash
PYTHONPATH=src python scripts/demo_local.py --persona both --voice
```

## RAG

The repo ships **4 sample KB docs** in `data/knowledge_base/`, so on a fresh
checkout retrieval is **on by default**: the bot supplements the transcript with
grounded passages. Clearing `KB_DIR` (or pointing it at an empty directory)
triggers the graceful **direct-context bypass** — the bot reasons over the
transcript alone (this is also the offline/test condition). See
[docs/RAG_ANALYSIS.md](docs/RAG_ANALYSIS.md) for the retriever design and the
sparse/dense fusion options (`RAG_DENSE`).

## Project layout

```
gchat_agent/
├── README.md                  # this file
├── PLAN.md                    # original implementation plan (predates some changes)
├── MEMORY.md                  # auth/setup gotchas + smoke-test environment
├── pyproject.toml             # package + optional extras
├── .env.example               # every config key, documented
├── data/
│   ├── scenarios.json         # ops + promo staff personas
│   └── knowledge_base/        # RAG corpus (ships 4 sample docs → retrieval on by default)
├── docs/
│   ├── ARCHITECTURE.md        # full design
│   ├── SETUP_GOOGLE_CHAT.md   # live-demo OAuth + space setup
│   ├── RAG_ANALYSIS.md        # retriever design
│   └── google_chat/           # bundled Google Chat REST reference
├── reports/                   # resolution reports (issue-<id>.md)
├── scripts/
│   ├── run_poller.py          # the issue-spotter bot loop
│   ├── run_staff.py           # an LLM staff persona
│   ├── authorize.py           # mint a per-account OAuth refresh token
│   └── run_webhook.py         # Phase-2 stub (not built in v1)
├── src/gchat_agent/
│   ├── config.py              # .env-driven settings (load_config → Config)
│   ├── models.py              # Conversation, Message, Issue, QAPair, Status, …
│   ├── runner.py              # Runner / build_runner — the orchestration loop
│   ├── observability.py       # optional langfuse tracing (no-op by default)
│   ├── llm/                   # base protocol, mock, gemini (live default), openrouter (legacy), build_llm/tts
│   ├── chat/                  # ChatClient base, google_rest, oauth
│   ├── rag/                   # base (Retriever protocol), store (build_retriever), bm25, boost, chunk, dense, fuse
│   └── agent/                 # analyzer, state (IssueStore), report, staff, prompts
└── tests/                     # offline unittest suite (mock LLM + fake chat client)
```

## What's deferred

The **webhook ingress is Phase 2** and is not built in v1 —
`scripts/run_webhook.py` only prints a deferral notice. v1 live ingress is the
REST poller (`scripts/run_poller.py`).
