"""Reciprocal Rank Fusion (§5.5) — combine two ranked lists, zero dependencies.

RRF fuses heterogeneous rankers (BM25 ⊕ dense) without needing their scores to
be comparable: each list contributes `1 / (k + rank)` per item, summed across
lists. Robust to score-scale mismatch and the classic choice for hybrid lexical
+ dense retrieval. `rrf_fuse` works on `(index, score)` rank lists (the shape
BM25/dense produce); the float scores are used only for ordering within a list,
not for the fused weight.
"""
from __future__ import annotations

# Standard RRF damping constant (Cormack et al. 2009).
_RRF_K = 60


def rrf_fuse(
    *ranked_lists: list[tuple[int, float]],
    k: int = _RRF_K,
    top_k: int | None = None,
) -> list[tuple[int, float]]:
    """Fuse ranked `(index, score)` lists by Reciprocal Rank Fusion.

    Each list is treated as an ordering (best first); an item at 0-based `rank`
    contributes `1 / (k + rank + 1)`. Contributions sum across lists by index.
    Returns `(index, rrf_score)` sorted descending, truncated to `top_k` when
    given. Empty inputs yield `[]`."""
    fused: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, (idx, _score) in enumerate(ranked):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank + 1)
    out = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)
    if top_k is not None:
        out = out[: max(0, top_k)]
    return out
