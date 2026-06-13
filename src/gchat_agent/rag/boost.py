"""Lexical-hybrid boost signals layered on BM25 (§5.5) — zero dependencies.

BM25 stems iGaming jargon and identifiers poorly: `RTP`, `KYC`, ticket ids and
Chat resource names carry the most retrieval signal yet score weakly as ordinary
terms. Two cheap, deterministic boosts correct this:

- **Exact-match boost** — multiply a passage's score when query *jargon tokens*
  (recognized acronyms, or any identifier-shaped token like `ticket-123` /
  `spaces/AAA` / `91%`) appear verbatim in the passage.
- **Recency boost** — nudge `kind="chat"` snippets up by how recent their
  `create_time` is, so the freshest chat context outranks a stale near-duplicate.

`boost_scores` takes BM25's `(index, score)` list and returns a re-ranked list;
it never invents new documents, only reweights the ones BM25 already matched.
"""
from __future__ import annotations

import re

from gchat_agent.rag.base import Passage
from gchat_agent.rag.bm25 import tokenize

# Domain acronyms / jargon BM25 treats as low-signal ordinary words. Lower-case;
# matched against tokenized query + passage text.
_JARGON: frozenset[str] = frozenset({
    "rtp", "kyc", "aml", "ggr", "ngr", "rg", "sla", "api", "psp",
    "payout", "payouts", "chargeback", "bonus", "wagering", "freespins",
    "geoblock", "geofence", "selfexclusion", "self-exclusion", "limit",
    "webhook", "deposit", "withdrawal", "promo", "jackpot", "provider",
})

# Identifier-shaped tokens (contain a connector or a trailing %/digit run) carry
# strong retrieval signal regardless of being in the jargon list: ticket-123,
# spaces/AAA, kyc-flow, users/42, 91%.
_IDENTIFIER_RE = re.compile(r"^[a-z0-9]+[._/\-][a-z0-9].*$")
_NUMERICISH_RE = re.compile(r"^\d+[%a-z]*$|^[a-z]+\d+$")

# Boost magnitudes (multiplicative on the BM25 score).
_EXACT_PER_HIT = 0.6   # added weight per distinct jargon/identifier hit
_EXACT_CAP = 2.0       # cap the multiplier so one term can't dominate
_RECENCY_MAX = 0.25    # max fractional bump for the most-recent chat snippet


def _query_signal_tokens(query: str) -> set[str]:
    """Jargon + identifier-shaped tokens in the query — the terms worth boosting
    an exact passage match on."""
    out: set[str] = set()
    for tok in tokenize(query):
        if tok in _JARGON or _IDENTIFIER_RE.match(tok) or _NUMERICISH_RE.match(tok):
            out.add(tok)
    return out


def exact_match_multiplier(query_signals: set[str], passage: Passage) -> float:
    """Multiplier (>=1.0) for verbatim jargon/identifier hits of `query_signals`
    in `passage.text`. Each distinct hit adds `_EXACT_PER_HIT`, capped at
    `_EXACT_CAP`."""
    if not query_signals:
        return 1.0
    text_tokens = set(tokenize(passage.text))
    hits = len(query_signals & text_tokens)
    if not hits:
        return 1.0
    return min(1.0 + _EXACT_CAP, 1.0 + _EXACT_PER_HIT * hits)


def _recency_rank(passages: list[Passage]) -> dict[int, float]:
    """Map passage-index -> recency weight in [0, 1] for `kind="chat"` snippets,
    by descending `create_time` (RFC-3339 sorts lexicographically). Newest gets
    1.0; ties share a weight; KB passages and timestamp-less snippets get 0.0."""
    timed = [
        (i, p.create_time)
        for i, p in enumerate(passages)
        if p.kind == "chat" and p.create_time
    ]
    if not timed:
        return {}
    times = sorted({t for _, t in timed}, reverse=True)
    rank_of = {t: idx for idx, t in enumerate(times)}
    span = max(1, len(times) - 1)
    return {i: 1.0 - rank_of[t] / span for i, t in timed}


def boost_scores(
    query: str,
    passages: list[Passage],
    scored: list[tuple[int, float]],
) -> list[tuple[int, float]]:
    """Re-rank BM25 `(index, score)` pairs with the exact-match + recency boosts.

    The boosted score is `bm25 * exact_multiplier * (1 + recency_bump)`. Returns
    a new list sorted descending; input is not mutated."""
    if not scored:
        return []
    signals = _query_signal_tokens(query)
    recency = _recency_rank(passages)
    out: list[tuple[int, float]] = []
    for idx, base in scored:
        if idx < 0 or idx >= len(passages):
            continue
        p = passages[idx]
        mult = exact_match_multiplier(signals, p)
        bump = 1.0 + _RECENCY_MAX * recency.get(idx, 0.0)
        out.append((idx, base * mult * bump))
    out.sort(key=lambda pair: pair[1], reverse=True)
    return out
