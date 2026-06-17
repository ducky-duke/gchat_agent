"""The `GitHubClient` Protocol the runner depends on (structural typing).

Kept tiny on purpose: the runner only ever needs to *file* a resolved issue, so
the contract is a single `create_issue`. The live `rest.GitHubRestClient` and the
test `tests.fakes.FakeGitHubClient` both satisfy it — the runner is written
against this Protocol, never a concrete class, so the offline tests stay network-
and credential-free.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class GitHubClient(Protocol):
    """File a GitHub issue and return its web URL."""

    def create_issue(
        self, title: str, body: str, labels: "list[str] | None" = None
    ) -> str:
        """Create an issue with `title` / `body` (Markdown) and optional `labels`,
        returning the created issue's `html_url`. Implementations tolerate unknown
        labels (a label the repo doesn't define must not lose the whole issue)."""
        ...
