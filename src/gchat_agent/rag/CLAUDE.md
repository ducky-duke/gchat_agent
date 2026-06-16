# rag/ — retrieval stack

Retrieval-augmented context for detection/clarity. **Zero dependencies** except the
optional embeddings backend in `dense.py`. The index is rebuilt in memory on each start.
Deep write-up: [`docs/RAG_ANALYSIS.md`](../../../docs/RAG_ANALYSIS.md).

- **`base.py`** — `Passage` dataclass (JSON round-trip) + `Retriever` Protocol (`retrieve`).
- **`bm25.py`** — pure-Python BM25 (`tokenize`, `BM25.score`).
- **`boost.py`** — lexical-hybrid signals over BM25: `exact_match_multiplier` +
  recency boost (`boost_scores`).
- **`chunk.py`** — `chunk_document()` / `chunk_history()`: KB docs + chat history into
  overlapping word-window `Passage`s.
- **`fuse.py`** — `rrf_fuse()`: Reciprocal Rank Fusion of two ranked lists.
- **`dense.py`** — optional `DenseRetriever` + `build_dense()`; lazy/optional embeddings
  backend (`_import_backend`), cosine ranking. The advanced-RAG upgrade.
- **`store.py`** — index builder + concrete retrievers: `Bm25BoostRetriever` (default,
  sparse) and `FusedRetriever` (dense+sparse via RRF). Entry points `build_passages()` /
  `build_retriever()`; `_load_kb_passages()` reads `data/knowledge_base/`.
