"""Index builder + concrete `Retriever`s (§5.5).

`build_retriever` is the one entry point the analyzer wires against. It indexes
the KB-directory documents **and** recent chat history into `Passage`s, then
returns a `Retriever`:

- default → `Bm25BoostRetriever` (BM25 ranking + the exact-match/recency boost,
  zero dependencies);
- `dense=True` → `FusedRetriever` (BM25 ⊕ dense, RRF-fused via `fuse.py`); the
  dense backend is lazy-imported only here, only when requested.

It returns **`None`** when there is nothing to index — an empty/missing `KB_DIR`
*and* no history — so the analyzer takes its graceful direct-context bypass (§3):
the full transcript goes straight to the model rather than through a retriever
that could drop a key message.

Pure stdlib on the default path (`os`, file reads). `dense.py` and its
`[embeddings]` extra load lazily and only when `dense=True`.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from gchat_agent.rag.base import Passage, Retriever
from gchat_agent.rag.bm25 import BM25
from gchat_agent.rag.boost import boost_scores
from gchat_agent.rag.chunk import chunk_document, chunk_history

if TYPE_CHECKING:
    from gchat_agent.models import Message

# KB files we treat as text documents to chunk.
_DOC_SUFFIXES = (".md", ".markdown", ".txt", ".rst")

# Cap on how many recent messages we index as chat passages (bounds index size;
# the analyzer still passes the full transcript directly — retrieval only
# *supplements* it).
_HISTORY_LIMIT = 200


class Bm25BoostRetriever:
    """Default zero-dep `Retriever`: BM25 ranked, then re-weighted by the
    exact-match (jargon/identifier) and chat-recency boosts (§boost)."""

    def __init__(self, passages: list[Passage]) -> None:
        self.passages = passages
        self._bm25 = BM25(passages)

    def retrieve(self, query: str, k: int) -> list[Passage]:
        # Over-fetch from BM25 so the boost can promote a strong exact match that
        # BM25 ranked just outside the top-k.
        prelim = self._bm25.score(query, k=max(k * 4, k + 10) if k > 0 else None)
        boosted = boost_scores(query, self.passages, prelim)
        return _materialize(self.passages, boosted, k)


class FusedRetriever:
    """`RAG_DENSE` path: RRF-fuse boosted-BM25 with the dense ranker (§fuse).

    The dense retriever is injected (built lazily in `build_retriever`) so this
    module imports no embeddings backend itself."""

    def __init__(self, passages: list[Passage], dense) -> None:  # noqa: ANN001 - dense is rag.dense.DenseRetriever
        self.passages = passages
        self._bm25 = BM25(passages)
        self._dense = dense

    def retrieve(self, query: str, k: int) -> list[Passage]:
        from gchat_agent.rag.fuse import rrf_fuse

        over = max(k * 4, k + 10) if k > 0 else None
        bm = boost_scores(query, self.passages, self._bm25.score(query, k=over))
        dn = self._dense.rank(query, k=over)
        fused = rrf_fuse(bm, dn, top_k=None)
        return _materialize(self.passages, fused, k)


def _materialize(
    passages: list[Passage],
    scored: list[tuple[int, float]],
    k: int,
) -> list[Passage]:
    """Build fresh `Passage`s for `scored` `(index, score)` pairs, copying the
    final score in, truncated to `k` (all when `k <= 0`)."""
    out: list[Passage] = []
    limit = scored if k <= 0 else scored[:k]
    for idx, score in limit:
        if idx < 0 or idx >= len(passages):
            continue
        p = passages[idx]
        out.append(
            Passage(
                text=p.text,
                source=p.source,
                section=p.section,
                kind=p.kind,
                create_time=p.create_time,
                score=score,
            )
        )
    return out


def _load_kb_passages(kb_dir: str) -> list[Passage]:
    """Chunk every text document under `kb_dir` (non-recursive top level, then
    one level of subdirs) into `kind="kb"` passages. A missing dir yields `[]`."""
    if not kb_dir or not os.path.isdir(kb_dir):
        return []
    passages: list[Passage] = []
    for root, _dirs, files in os.walk(kb_dir):
        for name in sorted(files):
            if not name.lower().endswith(_DOC_SUFFIXES):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(path, kb_dir)
            passages.extend(chunk_document(text, source=rel))
    return passages


def build_passages(
    kb_dir: str,
    history: list["Message"] | None = None,
) -> list[Passage]:
    """Assemble the full passage corpus: KB docs + the most recent
    `_HISTORY_LIMIT` chat messages. Exposed for tests/tooling."""
    passages = _load_kb_passages(kb_dir)
    if history:
        recent = history[-_HISTORY_LIMIT:]
        passages.extend(chunk_history(recent))
    return passages


def build_retriever(
    kb_dir: str,
    history: list["Message"] | None = None,
    dense: bool = False,
) -> Retriever | None:
    """Build the analyzer's `Retriever`, or `None` to trigger direct-context.

    Indexes `kb_dir` documents + recent `history`. Returns `None` when there is
    nothing to index (empty/missing `kb_dir` and no history) so the analyzer
    bypasses retrieval and passes the full transcript directly (§3). Otherwise
    returns `Bm25BoostRetriever` (default) or a BM25⊕dense `FusedRetriever`
    (`dense=True`, embeddings backend lazy-imported here only)."""
    passages = build_passages(kb_dir, history)
    if not passages:
        return None
    if dense:
        from gchat_agent.rag.dense import build_dense

        return FusedRetriever(passages, build_dense(passages))
    return Bm25BoostRetriever(passages)
