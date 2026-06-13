# RAG analysis — the `rag/` layer as built

> Expands PLAN [§3](../PLAN.md#3-why-rag-and-how-it-degrades-gracefully) /
> [§5.5](../PLAN.md#55-rag-layer-rag) and documents the **shipped** code in
> `src/gchat_agent/rag/`. Where this doc and `PLAN.md` disagree, this doc
> reflects the source.

## TL;DR

- **One retrieval-augmented `Analyzer`. No `--no-rag` build, no `USE_RAG` flag.**
  The only lever is the **knowledge base**: an empty/missing `KB_DIR` (and no
  indexed history) makes `build_retriever` return `None`, and the analyzer then
  passes the **full transcript directly** to the model. Populate `KB_DIR` and the
  same analyzer **supplements** the transcript with retrieved passages.
- **Default retriever is pure-Python, zero-dep**: Okapi BM25 + an exact-match
  (jargon/identifier) boost + a chat-recency boost.
- **Optional dense upgrade** (`RAG_DENSE=true`): a cosine-similarity embeddings
  ranker fused with BM25 via **Reciprocal Rank Fusion** (`rag/fuse.py`).
- **No graph RAG** — our queries are *targeted passage lookup*, not global
  sense-making, so a graph's LLM-heavy build cost and dependency weight aren't
  justified.

---

## 1. Why a single RAG analyzer (no separate non-RAG build)

The brief asked for RAG and, if RAG turned out unnecessary, *two* products (one
with, one without). We ship **one** analyzer instead, because the "without RAG"
behavior is not a different product — it is the **same analyzer with an empty
index**. The split would have been two code paths to test and keep in sync for no
behavioral gain.

The seam is the `Retriever | None` argument to `agent/analyzer.py::Analyzer`:

- `retriever is None` → `_retrieve_context()` returns `""`, the prompt builders
  (`agent/prompts.py`) **omit the "Retrieved context" block entirely**, and the
  rendered transcript is the model's only input. This is true direct-context
  reasoning — no ranker sits between the conversation and the LLM, so a BM25 miss
  can never drop a load-bearing message.
- `retriever is not None` → the analyzer pulls top-`k` passages per LLM call and
  appends them as a clearly-labeled **supplementary** block (see §3).

The default retriever is zero-dependency, so committing to RAG-only adds **no
runtime dependency** (the lone core dep is the `openai` SDK for LLM transport,
lazy-imported).

## 2. Where RAG earns its place

Direct-context reasoning is enough for a single active channel: modern OpenRouter
models have large context windows, and issue detection / clarification is
reasoning over the conversation that is already in front of the model. RAG earns
its cost in two situations:

1. **Grounding in an external knowledge base.** Judging whether something is an
   *issue* often needs domain knowledge the chat does not contain — "is RTP 91%
   out of policy?", "does this promo need compliance sign-off?", "what's the
   runbook for a failing payout webhook?". Retrieval over iGaming
   policies / runbooks / specs / past incidents lets the bot (a) recognize issues
   that require domain knowledge to spot, (b) ask **informed** questions, and
   (c) avoid re-asking what is already documented.
2. **Very long / multi-channel history.** When history outgrows the context
   window — or is too expensive to resend every polling cycle — retrieve only the
   passages relevant to the current issue instead of re-sending the whole
   transcript. `build_passages` caps indexed history at the most recent **200**
   messages (`_HISTORY_LIMIT`) to bound index size.

## 3. Graceful degradation — the empty-`KB_DIR` bypass

The control flow is entirely in `rag/store.py::build_retriever` and how the
analyzer reacts to its return value.

```
build_retriever(kb_dir, history, dense)
        │
        ├─ build_passages(kb_dir, history)  → KB-doc chunks + recent-history chunks
        │
        ├─ passages == []  → return None         # nothing to index
        │        └─► Analyzer.retriever is None
        │            └─► _retrieve_context() == ""  → prompts OMIT the context block
        │                └─► full transcript passed directly  (DIRECT-CONTEXT BYPASS)
        │
        └─ passages != []  → Bm25BoostRetriever (default)
                              or FusedRetriever  (dense=True)
                 └─► Analyzer retrieves top-k, renders a "Retrieved context" block
                     that SUPPLEMENTS the transcript
```

Key facts, verified against the source:

- **What counts as "nothing to index":** `build_passages` returns `[]` when
  `kb_dir` is empty/missing *and* `history` is falsy. `build_retriever` maps that
  to `None`. (`test_rag.py::test_empty_kb_and_no_history_returns_none`.)
- **Supplement, never replace.** Even with a populated KB the analyzer still
  renders the full (windowed) transcript; retrieval is appended after it. The
  prompt text is explicit: *"treat the transcript above as the source of truth …
  never cite these as message ids."* (`prompts.py::_render_user`).
- **Retrieval is best-effort.** `Analyzer._retrieve_context` wraps
  `retriever.retrieve(...)` in a `try/except` and returns `""` on any error, so a
  ranker failure degrades to direct-context rather than blocking detection or
  clarity assessment. It also short-circuits to `""` when `top_k <= 0` or the
  query is blank.
- **Per-call query, not a fixed query.** Detection queries with the rendered
  transcript; clarity/question generation query with the issue's own
  title + summary + category + recorded `missing_info` (`Analyzer._issue_query`),
  so the ranker pulls passages about *that* problem.
- **Each injected passage is capped at 600 chars** (`Analyzer._PASSAGE_CHARS`) and
  rendered as `[kind:source › section] text`, keeping the block compact.

### How the live bot wires it

`runner.py::build_runner` calls:

```python
retriever = build_retriever(config.KB_DIR, history=None, dense=config.RAG_DENSE)
analyzer  = Analyzer(llm, retriever, config.RAG_TOP_K)
```

Note `history=None` at build time — the live bot's retriever indexes **KB docs
only**, so in the demo a **populated `KB_DIR` is what activates retrieval**.
(History-only indexing is a supported path of `build_retriever` and is exercised
by tests, but the runner does not currently feed live history into the index.)
`KB_DIR` defaults to `data/knowledge_base/`, which **ships 4 sample docs**
(`rtp_policy.md`, `payments_runbook.md`, `promo_checklist.md`,
`kyc_compliance.md`) — so **out of the box the demo runs with retrieval ON**.
Clearing `KB_DIR` (or pointing it at an empty directory) drops the bot back into
the **direct-context bypass** (which is also the offline/test condition).

## 4. The default retriever — pure-Python BM25 + boosting (zero deps)

`Bm25BoostRetriever` (`rag/store.py`) is the default. Per query it:

1. Over-fetches from BM25 (`max(k*4, k+10)` candidates) so the boost can promote a
   strong exact match BM25 ranked just outside the top-`k`.
2. Re-weights with `boost_scores`.
3. Materializes the top-`k` `Passage`s with the final score copied in.

### 4.1 Chunking — `rag/chunk.py`

Two entry points, both stdlib:

- `chunk_document(text, source)` — splits a KB doc into `kind="kb"` passages.
  Markdown headings (`#`..`######`) become the passage `section`; content before
  the first heading is section `""`. Long sections are windowed into overlapping
  word-windows (**180 words, 40-word overlap**) so an oversized section never
  becomes one giant passage and a fact straddling a boundary stays retrievable
  from either side. `create_time` is `""` for KB docs.
- `chunk_history(messages)` — turns chat `Message`s into `kind="chat"` snippets.
  Consecutive short messages are **packed** together (up to **120 words**) so each
  snippet has enough lexical signal for BM25. Each snippet line is
  `#<id> <sender>: <text>`; the snippet `source` is the **first** contained
  message id and `create_time` is the **latest** contained timestamp (so the
  recency boost ranks the freshest snippet highest). Empty-text messages are
  skipped.

### 4.2 BM25 — `rag/bm25.py`

Okapi BM25 (`k1=1.5`, `b=0.75`) over a fixed `Passage` corpus. Document length,
term frequency, and smoothed IDF are precomputed once at construction; `score`
ranks the whole corpus and returns `(index, score)` pairs sorted high-to-low,
**dropping zero-overlap docs**. Empty query or empty corpus → `[]`.

The custom `tokenize` lower-cases and splits on non-alphanumerics **but preserves
intra-word `.` `-` `_` `/`**, so iGaming identifiers survive as single tokens:

| Input | Tokens |
|---|---|
| `RTP 91%` | `["rtp", "91"]` |
| `ticket-123` | `["ticket-123"]` |
| `spaces/AAA` | `["spaces/aaa"]` |

This is what makes the exact-match boost below possible — identifiers stay whole
instead of being shredded into low-signal fragments.

### 4.3 Boosting — `rag/boost.py`

`boost_scores(query, passages, scored)` takes BM25's `(index, score)` list and
returns a **re-ranked** list of the same items — it never invents documents, only
reweights what BM25 already matched. Two multiplicative signals:

- **Exact-match boost.** Query tokens that are recognized **jargon** (a frozenset:
  `rtp`, `kyc`, `aml`, `ggr`, `ngr`, `sla`, `api`, `psp`, `payout`, `chargeback`,
  `wagering`, `webhook`, `withdrawal`, `promo`, `jackpot`, … ) **or
  identifier-shaped** (contain a connector like `ticket-123` / `spaces/AAA`, or
  numeric-ish like `91%`) get a verbatim-presence bonus in the passage:
  `+0.6` per distinct hit, capped at a `×3.0` multiplier
  (`min(1.0 + _EXACT_CAP, 1.0 + 0.6·hits)` with `_EXACT_CAP = 2.0`). This is why
  `"RTP rate"` ranks the doc containing `RTP` above a generic doc that only shares
  the word `rate` (`test_rtp_acronym_ranks_its_doc_first`), and an unlisted
  `ticket-123` still gets boosted (`test_identifier_token_boosted`).
- **Recency boost.** `kind="chat"` snippets are nudged up by how recent their
  `create_time` is (RFC-3339 sorts lexicographically): newest gets weight `1.0`,
  scaling a bump of up to **+25%**. KB passages and timestamp-less snippets get no
  bump. This breaks ties between near-duplicate chat snippets toward the fresher
  one (`test_freshest_chat_snippet_outranks_stale_twin`) and never reorders KB
  docs (`test_kb_passages_get_no_recency_bump`).

Final score = `bm25 * exact_multiplier * (1 + recency_bump)`.

## 5. RRF fusion — `rag/fuse.py`

`rrf_fuse(*ranked_lists, k=60, top_k=None)` combines heterogeneous rankers
**without needing their scores to be comparable**. Each list is treated purely as
an ordering; an item at 0-based `rank` contributes `1 / (k + rank + 1)`, summed
across lists by index. The float scores in the input lists are used **only** for
ordering within a list, not for the fused weight — so an item ranked #1 in both
lists wins regardless of raw magnitudes (`test_scores_used_only_for_within_list_order`),
and a consensus item across both lists rises to the top
(`test_consensus_item_ranks_first`). `k=60` is the standard RRF damping constant
(Cormack et al. 2009).

This is the canonical glue for hybrid lexical + dense retrieval; it is used only
on the dense path.

## 6. Optional dense backend — `rag/dense.py` (`RAG_DENSE=true`)

The credible "advanced RAG" upgrade, kept **off the default dependency-free path**:

- **Not imported unless `RAG_DENSE=true`.** `store.py::build_retriever` only
  `from gchat_agent.rag.dense import build_dense` when `dense=True`, returning a
  `FusedRetriever` instead of `Bm25BoostRetriever`.
- `FusedRetriever` over-fetches from **both** boosted-BM25 and the dense ranker,
  then RRF-fuses the two `(index, score)` lists (`rrf_fuse`) before materializing
  the top-`k`.
- `DenseRetriever` embeds passages and the query, ranks by **cosine similarity**,
  and exposes both `retrieve` (the `Retriever` protocol) and `rank` (the
  `(index, score)` shape `store.py` fuses). It is lazy in two senses: importing
  the module pulls in **no** embeddings backend, and the backend + passage
  embeddings are only computed on first use (or via `warm()`).

> **Reality check on the dependency.** The shipped `dense.py` lazy-imports
> **`sentence_transformers`** (default model
> `sentence-transformers/all-MiniLM-L6-v2`) and raises a clear `RuntimeError`
> naming the missing extra if it can't. `pyproject.toml` declares
> `embeddings = ["sentence-transformers"]`, so `pip install -e ".[embeddings]"`
> is what `RAG_DENSE=true` needs — but it pulls **torch**, which may lag
> Python 3.14, so a compatible build may not yet exist on this env. **Treat the
> dense backend as experimental**; the default BM25 + boosting path is the
> supported one and needs none of this.

## 7. Why no graph RAG

Our query shape is **targeted passage lookup** ("is RTP 91% out of policy?",
"runbook for a failing payout webhook?"), not **global sense-making** over a
corpus (the case GraphRAG addresses). A graph would require LLM entity/relation
extraction over the whole corpus at build time and drag in heavy graph/embedding
dependencies — cost and dependency weight that buy nothing for this corpus and
break the lean-deps posture. Dense/vector RAG is explicitly *not excluded* (it's
the optional `RAG_DENSE` path above); graph RAG is out of scope.

## 8. The `Passage` / `Retriever` contract — `rag/base.py`

Everything above implements one tiny interface so the analyzer never changes when
the backend does:

- `Passage{ text, source, section, kind("kb"|"chat"), create_time, score }`,
  JSON-serializable via `to_dict` / `from_dict`.
- `Retriever` protocol: `retrieve(query, k) -> list[Passage]`. `Bm25BoostRetriever`,
  `FusedRetriever`, and `DenseRetriever` all satisfy it.

## 9. Running the RAG tests

The RAG layer is covered by offline, network-free `unittest` cases (BM25 ordering,
exact-match + recency boosting, RRF fusion, the empty-KB bypass, and a
history-only retriever):

From the repo root, with the `igaming` conda env (Python 3.14) activated:

```bash
PYTHONPATH=src python -m unittest tests.test_rag -v
```

Or run the whole suite (the functional gate):

```bash
PYTHONPATH=src python -m unittest discover -s tests -t . -p "test_*.py"
```

## 10. Configuration knobs

From `.env.example` / `config.py` (defaults shown):

| Key | Default | Effect |
|---|---|---|
| `KB_DIR` | `data/knowledge_base` | Docs to index (`.md` / `.markdown` / `.txt` / `.rst`, walked recursively). Empty ⇒ direct-context bypass. |
| `RAG_TOP_K` | `5` | Passages injected per LLM call (the analyzer's `top_k`). `<= 0` disables retrieval. |
| `RAG_DENSE` | `false` | `true` swaps `Bm25BoostRetriever` for a BM25⊕dense `FusedRetriever` (see §6 caveat). |
