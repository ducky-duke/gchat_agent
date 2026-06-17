"""GitHub issue export — file each resolved issue as a GitHub issue.

`base.GitHubClient` is the Protocol the runner depends on; `rest.GitHubRestClient`
is the live stdlib-`urllib` implementation (mirrors `chat.google_rest`). Tests use
`tests.fakes.FakeGitHubClient`, never the live client.
"""
from .base import GitHubClient

__all__ = ["GitHubClient"]
