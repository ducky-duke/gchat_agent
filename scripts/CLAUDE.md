# scripts/ — entry points

Each script self-adds `src/` to `sys.path`, so run them directly (no install). Run flow
details + `./start_bot.sh` wrapper live in the root [`CLAUDE.md`](../CLAUDE.md).

- **`run_poller.py`** — the issue-spotter bot's polling loop. `--once` for a single cycle.
  Wrapped by `./start_bot.sh` (fresh-start default; `--continue` to resume).
- **`run_staff.py`** — run one LLM staff persona against the live space.
  `--persona ops|promo|apigw|noise --token <tok.json>` (`apigw` = the API-gateway-timeout
  incident scenario in `data/scenarios.json`, used by the live demo; `noise` = the
  CONTROL persona: benign small talk, no `facts`, never answers — proves the bot files
  NO issue for non-issue chatter). On seed it prints machine-readable `SEEDED_THREAD`/
  `SEEDED_MSG <id>` lines so an orchestrator can verify the bot's handling.
- **`verify_precision.py`** — live verifier for the demo's control case: given the noise
  thread + message ids, confirms the bot SAW the noise (ids in its `seen_message_ids`),
  posted NOTHING into the noise thread, and that the noise came from a NON-bot account.
  Prints `KEY value` lines + `VERDICT PASS|REGRESSION|INCONCLUSIVE` (exit 0/2/3).

## Demo accounts (token → Gmail → users/<id>) — NON-secret mapping
The refresh tokens under `secrets/` carry no email; resolve via OAuth `tokeninfo`. The
**bot self-filters ONLY its own `users/<id>`** (no sender-type rule), so a staff/noise
persona MUST post from a non-bot account or its messages are dropped by default:
- `token_bot.json`   → **mikmikb26@gmail.com**     `users/116566195804326411461` = THE BOT
- `token_ops.json`   → trantrongducqt@gmail.com     `users/107160784481317583826`
- `token_promo.json` → **mety25757@gmail.com**       `users/115562244684898458288`

`demo_live.sh` posts the incident as `token_ops` and the noise as `token_promo` (mety25757) —
two distinct non-bot humans. Don't point a persona at `token_bot` (self-filtered → hollow test).
- **`authorize.py`** — one-time-per-account OAuth loopback mint.
  `--client <client.json> --out <tok.json> --account <email>` → a refresh token.
- **`demo_local.py`** — full agent loop end-to-end over the in-memory `FakeChatClient` with
  the live/.env LLM — **no Google needed**. `--persona ops|promo|both`, `--max-rounds N`,
  `--voice`. Reports → `reports/demo/`.
- **`run_webhook.py`** — webhook ingress entrypoint, **Phase-2 DEFERRED** stub.

The repo-root **`demo_live.sh`** wraps `run_poller.py` + `run_staff.py` into a
one-command LIVE end-to-end demo (seed an "API gateway timeout" incident → resolve
→ filed GitHub issue + voice DM); details in the root [`CLAUDE.md`](../CLAUDE.md).
