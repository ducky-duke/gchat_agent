# github/ — GitHub issue export

Optional sink: file each *resolved* issue as a GitHub issue (report Markdown + the
collected thread transcript) in `GITHUB_REPO`, so the bot builds a durable,
searchable backlog of technical issues. Gated by `GITHUB_ISSUES` (off by default,
so the offline/test path needs no GitHub). Mirrors `chat/` — stdlib `urllib`
only, no PyGithub; tests use `tests/fakes.FakeGitHubClient`, never the live client.

- **`base.py`** — `GitHubClient` Protocol: a single `create_issue(title, body,
  labels)` → `html_url`. The runner depends on this Protocol, never the concrete
  class, so the offline tests stay network/credential-free.
- **`rest.py`** — live `GitHubRestClient` (Bearer auth, `User-Agent` required,
  bounded retry/backoff on 429/5xx). `create_issue` is **unknown-label tolerant**:
  a 422 while labels were supplied retries ONCE without labels (the issue must not
  be lost over a missing label). `build_github(config)` → a client or `None`
  (export off / no token). Token precedence: `GITHUB_TOKEN`, else
  `gh auth token --user GITHUB_ACCOUNT` (`_gh_cli_token`) — pins the export to one
  host `gh` login without a secret in `.env` or switching the active account.

Behavior + the off-critical-path wiring (`runner._submit_publish` /
`_publish_issue_bg`, drained by `_drain_background`) and the payload renderers
(`report.render_chat_transcript` / `render_github_issue`) are documented in the
**root [`CLAUDE.md`](../../../CLAUDE.md)** "GitHub issue export". Fixed labels
(`auto-filed`, `severity:<low|med|high>`) are pre-created on the repo so a create
never 422s.
