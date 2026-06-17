"""The `MeetClient` Protocol the demo/runner depends on (structural typing).

Mirrors `github.base`: kept deliberately tiny — the only thing the project needs
from the Google Meet REST API is to MINT a meeting space and get back its join
link, so the contract is a single `create_space`. The live `rest.MeetRestClient`
and the test `tests.fakes.FakeMeetClient` both satisfy it; callers are written
against this Protocol, never a concrete class, so the offline tests stay network-
and credential-free.

Why only this slice: an AI cannot be a *speaking* participant on a Google Meet —
the Meet *Media* API is receive-only and Developer-Preview-gated (see the bundled
reference at `docs/google_meet/`). The REST API's `spaces.create` is the achievable
integration: programmatically create a real meeting and share its join link in
Chat so a human joins a live incident call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class MeetSpace:
    """A created Google Meet space (the subset of the v2 `Space` resource we use).

    - `name` — the server resource name, `spaces/<id>`.
    - `meeting_uri` — the clickable join URL, `https://meet.google.com/abc-mnop-xyz`.
    - `meeting_code` — the typeable 10-char code, `abc-mnop-xyz` (may be empty if
      the API omits it; the URI is the authoritative join link).
    - `raw` — the full API payload, for callers that need fields we don't model.
    """

    name: str
    meeting_uri: str
    meeting_code: str = ""
    raw: "dict | None" = None


@runtime_checkable
class MeetClient(Protocol):
    """Create a Google Meet space and return its join link."""

    def create_space(self) -> MeetSpace:
        """Create a meeting space (`POST .../v2/spaces`) and return a `MeetSpace`
        carrying its `meeting_uri` join link. Raises `RuntimeError` on a hard
        transport / credential failure — callers treat Meet creation as
        best-effort (a missing link degrades gracefully, like voice → disk)."""
        ...
