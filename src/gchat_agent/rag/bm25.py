"""Pure-Python BM25 ranking (§5.5) — zero dependencies.

Okapi BM25 over a fixed corpus of `Passage`s. `tokenize` lower-cases and splits
on non-alphanumerics while preserving intra-word `.`/`-`/`_`/`/` so iGaming
identifiers (`spaces/AAA`, `ticket-123`, `kyc-flow`) survive as single tokens
for the exact-match boost layered on top (§boost). Document length, term
frequency and inverse document frequency are precomputed once at construction;
`score` ranks the whole corpus against a query and returns `(index, score)`
pairs sorted high-to-low.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from gchat_agent.rag.base import Passage

# BM25 free parameters (standard Okapi defaults).
_K1 = 1.5
_B = 0.75

# Keep alphanumerics plus the connector chars that hold identifiers together;
# everything else is a separator. Tokens are lower-cased.
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._/\-][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    """Lower-case and tokenize, preserving intra-word `.`/`-`/`_`/`/`.

    Standalone connector characters are dropped; `RTP 91%` -> `["rtp", "91"]`,
    `ticket-123` -> `["ticket-123"]`, `spaces/AAA` -> `["spaces/aaa"]`."""
    return _TOKEN_RE.findall((text or "").lower())


class BM25:
    """Okapi BM25 ranker over a fixed `passages` corpus.

    Construct once; call `score(query, k)` per query. Empty corpora are valid
    (every query returns `[]`)."""

    def __init__(self, passages: list[Passage], *, k1: float = _K1, b: float = _B) -> None:
        self.passages = passages
        self.k1 = k1
        self.b = b
        self._docs: list[list[str]] = [tokenize(p.text) for p in passages]
        self._freqs: list[Counter[str]] = [Counter(d) for d in self._docs]
        self._lengths: list[int] = [len(d) for d in self._docs]
        n = len(self._docs)
        self._avgdl: float = (sum(self._lengths) / n) if n else 0.0
        # Document frequency per term, then smoothed IDF.
        df: Counter[str] = Counter()
        for freq in self._freqs:
            df.update(freq.keys())
        self._idf: dict[str, float] = {
            term: math.log(1.0 + (n - d + 0.5) / (d + 0.5)) for term, d in df.items()
        }

    def score(self, query: str, k: int | None = None) -> list[tuple[int, float]]:
        """Rank every document against `query`; return `(index, score)` sorted
        descending. With `k` set, truncate to the top `k`. Documents scoring
        zero (no query-term overlap) are dropped."""
        q_terms = tokenize(query)
        if not q_terms or not self._docs:
            return []
        scored: list[tuple[int, float]] = []
        for i, freq in enumerate(self._freqs):
            dl = self._lengths[i] or 1
            s = 0.0
            for term in q_terms:
                tf = freq.get(term, 0)
                if not tf:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = tf + self.k1 * (1.0 - self.b + self.b * dl / (self._avgdl or 1.0))
                s += idf * (tf * (self.k1 + 1.0)) / denom
            if s > 0.0:
                scored.append((i, s))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        if k is not None:
            scored = scored[: max(0, k)]
        return scored
