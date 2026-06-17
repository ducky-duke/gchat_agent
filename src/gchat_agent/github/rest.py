"""Live GitHub issue client over **stdlib `urllib` only** (no PyGithub).

Mirrors `chat.google_rest`: a Bearer-authed JSON `urllib` request with bounded
retries/backoff on rate-limit / 5xx, and a clean `RuntimeError` on a hard
failure. One instance is bound to one `owner/name` repo.

Auth: a token with `repo` scope. `build_github` resolves it from
`config.GITHUB_TOKEN`, falling back to the host `gh auth token` (the demo machine
is already logged in) so no secret has to live in `.env`. When neither is
available the factory returns `None` and the runner simply skips GitHub export —
best-effort, like the voice path degrading to disk.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Final

from ..config import Config

# GitHub requires a User-Agent on every request (a missing one is a hard 403).
_USER_AGENT: Final[str] = "gchat-agent-issue-spotter"
_API_VERSION: Final[str] = "2022-11-28"
_HTTP_TIMEOUT_SECONDS: Final[float] = 15.0
_MAX_RETRIES: Final[int] = 2
# Transient statuses worth a backoff-and-retry (rate limit + the 5xx family).
_RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


class GitHubRestClient:
    """File issues in one `owner/name` repo via the GitHub REST API."""

    def __init__(
        self,
        repo: str,
        token: str,
        api_url: str = "https://api.github.com",
    ) -> None:
        self.repo = repo.strip().strip("/")
        self._token = token
        self.api_url = api_url.rstrip("/")

    # --- ChatClient-style public surface -----------------------------------
    def create_issue(
        self, title: str, body: str, labels: "list[str] | None" = None
    ) -> str:
        """Create an issue and return its `html_url`.

        Unknown-label tolerance: GitHub rejects the whole create with 422 if any
        label doesn't exist in the repo. Rather than lose the issue, a 422 while
        labels were supplied is retried ONCE with no labels — the issue (report +
        transcript) is the thing that must not be lost; the labels are a nicety."""
        payload: dict[str, Any] = {"title": title, "body": body}
        clean = [str(l).strip() for l in (labels or []) if str(l).strip()]
        if clean:
            payload["labels"] = clean
        try:
            data = self._post_issue(payload)
        except _UnprocessableEntity:
            if not clean:
                raise
            payload.pop("labels", None)
            data = self._post_issue(payload)
        return str(data.get("html_url", "") or "")

    # --- transport ---------------------------------------------------------
    def _post_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST `payload` to `repos/{repo}/issues` with retries. Raises
        `_UnprocessableEntity` on 422 (so the caller can retry without labels) and
        `RuntimeError` on any other hard failure."""
        url = f"{self.api_url}/repos/{self.repo}/issues"
        body = json.dumps(payload).encode("utf-8")
        last_status = 0
        last_payload: dict[str, Any] = {}
        for attempt in range(_MAX_RETRIES + 1):
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Authorization", f"Bearer {self._token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", _API_VERSION)
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", _USER_AGENT)
            try:
                with urllib.request.urlopen(
                    req, timeout=_HTTP_TIMEOUT_SECONDS
                ) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    parsed = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    parsed = {"_raw": raw.decode("utf-8", "replace")}
                last_status, last_payload = exc.code, parsed
                if exc.code == 422:
                    raise _UnprocessableEntity(json.dumps(parsed)[:400]) from exc
                if exc.code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise RuntimeError(
                    f"GitHub create_issue failed (HTTP {exc.code}): "
                    f"{json.dumps(parsed)[:600]}"
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < _MAX_RETRIES:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise RuntimeError(f"GitHub API unreachable: {exc}") from exc
        raise RuntimeError(
            f"GitHub create_issue failed after retries "
            f"(HTTP {last_status}): {json.dumps(last_payload)[:600]}"
        )


class _UnprocessableEntity(RuntimeError):
    """A 422 from GitHub (e.g. an unknown label) — caught internally to retry the
    create without labels. Never escapes `create_issue`."""


def _gh_cli_token(account: str = "") -> str:
    """The host `gh` CLI's token (`gh auth token`), or "" if gh is absent / not
    logged in. Lets the demo machine's existing GitHub login back the client with
    no token in `.env`. `account` (when set) pins it to that login via
    `--user <account>`, so the export targets one owner regardless of which gh
    account is currently active."""
    cmd = ["gh", "auth", "token"]
    if account.strip():
        cmd += ["--user", account.strip()]
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def build_github(config: Config) -> "GitHubRestClient | None":
    """Wire a `GitHubRestClient` from `config`, or `None` when GitHub export is
    off or no token is reachable.

    Token precedence: `GITHUB_TOKEN` (explicit), else the host `gh auth token`.
    A missing token is non-fatal — the runner just skips GitHub export (logged
    once), exactly as voice degrades to disk — so a misconfigured demo still runs.
    """
    if not config.GITHUB_ISSUES:
        return None
    token = (config.GITHUB_TOKEN or "").strip() or _gh_cli_token(config.GITHUB_ACCOUNT)
    if not token:
        print(
            "[issue-spotter] GITHUB_ISSUES is on but no token is available "
            "(set GITHUB_TOKEN or run `gh auth login`); skipping GitHub export.",
            file=sys.stderr,
        )
        return None
    return GitHubRestClient(config.GITHUB_REPO, token, api_url=config.GITHUB_API_URL)
