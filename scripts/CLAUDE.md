# scripts/ — entry points

Each script self-adds `src/` to `sys.path`, so run them directly (no install). Run flow
details + `./start_bot.sh` wrapper live in the root [`CLAUDE.md`](../CLAUDE.md).

- **`run_poller.py`** — the issue-spotter bot's polling loop. `--once` for a single cycle.
  Wrapped by `./start_bot.sh` (fresh-start default; `--continue` to resume).
- **`run_staff.py`** — run one LLM staff persona against the live space.
  `--persona ops|promo|apigw --token <tok.json>` (`apigw` = the API-gateway-timeout
  incident scenario in `data/scenarios.json`, used by the live demo).
- **`authorize.py`** — one-time-per-account OAuth loopback mint.
  `--client <client.json> --out <tok.json> --account <email>` → a refresh token.
- **`demo_local.py`** — full agent loop end-to-end over the in-memory `FakeChatClient` with
  the live/.env LLM — **no Google needed**. `--persona ops|promo|both`, `--max-rounds N`,
  `--voice`. Reports → `reports/demo/`.
- **`run_webhook.py`** — webhook ingress entrypoint, **Phase-2 DEFERRED** stub.

The repo-root **`demo_live.sh`** wraps `run_poller.py` + `run_staff.py` into a
one-command LIVE end-to-end demo (seed an "API gateway timeout" incident → resolve
→ filed GitHub issue + voice DM); details in the root [`CLAUDE.md`](../CLAUDE.md).
