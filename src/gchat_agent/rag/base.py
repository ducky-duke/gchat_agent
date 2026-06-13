"""Retriever protocol + the `Passage` model (§5.5).

Any ranker (BM25, dense, RRF-fused) implements `Retriever`, so the analyzer never
changes when the backend does. `Passage.kind` is `"kb"` (knowledge-base doc) or
`"chat"` (chat-history snippet).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass
class Passage:
    """A retrieved chunk with provenance + ranking metadata (§5.5)."""

    text: str
    source: str  # file name / message id the passage came from
    section: str  # heading / sub-section within the source
    kind: Literal["kb", "chat"]  # knowledge-base doc vs chat-history snippet
    create_time: str  # RFC-3339 for chat snippets; may be "" for KB docs
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source": self.source,
            "section": self.section,
            "kind": self.kind,
            "create_time": self.create_time,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Passage":
        return cls(
            text=data.get("text", ""),
            source=data.get("source", ""),
            section=data.get("section", ""),
            kind=data.get("kind", "kb"),
            create_time=data.get("create_time", ""),
            score=float(data.get("score", 0.0)),
        )


@runtime_checkable
class Retriever(Protocol):
    """Return the top-`k` passages most relevant to `query`."""

    def retrieve(self, query: str, k: int) -> list[Passage]:
        ...
