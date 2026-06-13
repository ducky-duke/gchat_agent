"""Optional dense (embeddings) retriever (§5.5) — the advanced-RAG upgrade.

**Not imported unless `RAG_DENSE=true`**, so the default BM25 + boosting path
stays dependency-free. The embeddings backend (`[embeddings]` extra) is
**lazy-imported inside `_embed`**, never at module top level; importing this
module is cheap and safe even without the extra installed.

`DenseRetriever` implements the `Retriever` protocol over a fixed `Passage`
corpus by cosine similarity of embeddings, and exposes `rank` (the `(index,
score)` shape BM25 produces) so `store.py` can RRF-fuse it with BM25 via
`fuse.py`. If the backend cannot be imported it raises a clear `RuntimeError`
naming the missing extra — the caller (`store.py`) only reaches here when
`RAG_DENSE=true`.
"""
from __future__ import annotations

import math

from gchat_agent.rag.base import Passage

# Default embedding model id; overridable via `build_dense(model=...)`.
_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _import_backend():  # noqa: ANN202 - backend type is dynamic / optional
    """Lazy-import the embeddings backend. Raises a clear error if the optional
    `[embeddings]` extra is not installed."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised only with extra absent
        raise RuntimeError(
            "RAG_DENSE=true requires the optional [embeddings] extra "
            "(sentence-transformers). Install it or set RAG_DENSE=false."
        ) from exc
    return SentenceTransformer


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 for a zero vector."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class DenseRetriever:
    """Embeddings-based `Retriever` over a fixed `passages` corpus.

    Lazy: the backend is imported and passage embeddings are computed on first
    use (or eagerly via `warm()`), so constructing the object imports nothing."""

    def __init__(self, passages: list[Passage], *, model: str = _DEFAULT_MODEL) -> None:
        self.passages = passages
        self.model_name = model
        self._model = None
        self._doc_vecs: list[list[float]] | None = None

    def warm(self) -> None:
        """Force backend import + passage embedding now (else done on first use)."""
        if self._model is None:
            SentenceTransformer = _import_backend()
            self._model = SentenceTransformer(self.model_name)
        if self._doc_vecs is None:
            self._doc_vecs = self._embed([p.text for p in self.passages])

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            self.warm()
        if not texts:
            return []
        vecs = self._model.encode(texts)  # type: ignore[union-attr]
        return [list(map(float, v)) for v in vecs]

    def rank(self, query: str, k: int | None = None) -> list[tuple[int, float]]:
        """Rank every passage against `query` by cosine similarity; return
        `(index, score)` sorted descending, truncated to `k` when given. The
        `(index, score)` shape lets `store.py` RRF-fuse this with BM25."""
        if not self.passages or not (query or "").strip():
            return []
        self.warm()
        q = self._embed([query])[0]
        docs = self._doc_vecs or []
        scored = [(i, _cosine(q, docs[i])) for i in range(len(docs))]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        if k is not None:
            scored = scored[: max(0, k)]
        return scored

    def retrieve(self, query: str, k: int) -> list[Passage]:
        """`Retriever` protocol: top-`k` passages by cosine similarity, each
        carrying its similarity in `Passage.score`."""
        out: list[Passage] = []
        for idx, score in self.rank(query, k):
            p = self.passages[idx]
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


def build_dense(passages: list[Passage], *, model: str = _DEFAULT_MODEL) -> DenseRetriever:
    """Construct a `DenseRetriever` (no import yet — deferred to first use)."""
    return DenseRetriever(passages, model=model)
