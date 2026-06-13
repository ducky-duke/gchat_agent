"""RAG layer — the `Retriever` protocol, `Passage` model, and rankers (BM25, dense).

The analyzer wires against `build_retriever` (which returns `None` to trigger the
graceful direct-context bypass when there's nothing to index); the rankers and
chunker are exposed for tests/tooling.
"""
from gchat_agent.rag.base import Passage, Retriever
from gchat_agent.rag.bm25 import BM25, tokenize
from gchat_agent.rag.boost import boost_scores
from gchat_agent.rag.chunk import chunk_document, chunk_history
from gchat_agent.rag.fuse import rrf_fuse
from gchat_agent.rag.store import (
    Bm25BoostRetriever,
    FusedRetriever,
    build_passages,
    build_retriever,
)

__all__ = [
    "Passage",
    "Retriever",
    "BM25",
    "tokenize",
    "boost_scores",
    "chunk_document",
    "chunk_history",
    "rrf_fuse",
    "Bm25BoostRetriever",
    "FusedRetriever",
    "build_passages",
    "build_retriever",
]
