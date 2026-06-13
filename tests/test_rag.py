"""Tests for the RAG retrieval stack (§5.5 / §12).

Pure stdlib `unittest`, no network. Covers the offline-deterministic pieces:

- BM25 relevance ordering over a few tiny docs;
- exact-match boosting of iGaming jargon/identifiers (RTP/KYC ranks its doc first);
- recency boosting of `kind="chat"` snippets (freshest outranks a stale near-twin);
- Reciprocal Rank Fusion (`rag.fuse.rrf_fuse`) ordering of two ranked lists;
- `build_retriever('', None) -> None` empty-KB bypass (analyzer takes direct-context).
"""
from __future__ import annotations

import unittest

from gchat_agent.models import Message, SenderType
from gchat_agent.rag.base import Passage, Retriever
from gchat_agent.rag.bm25 import BM25
from gchat_agent.rag.boost import boost_scores
from gchat_agent.rag.fuse import rrf_fuse
from gchat_agent.rag.store import build_retriever


def _kb(text: str, source: str = "doc.md", section: str = "") -> Passage:
    return Passage(text=text, source=source, section=section, kind="kb", create_time="")


def _chat(text: str, source: str, create_time: str) -> Passage:
    return Passage(
        text=text, source=source, section="chat", kind="chat", create_time=create_time
    )


def _ranking(scored: list[tuple[int, float]]) -> list[int]:
    """Just the index ordering from a `(index, score)` rank list."""
    return [idx for idx, _ in scored]


class Bm25OrderingTest(unittest.TestCase):
    def test_orders_by_relevance(self) -> None:
        passages = [
            _kb("the cat sat on the mat", source="a"),
            _kb("a dog chased the cat around the yard", source="b"),
            _kb("financial regulation and compliance reporting", source="c"),
        ]
        bm25 = BM25(passages)

        scored = bm25.score("cat", k=10)
        ranked = _ranking(scored)

        # Both cat docs surface; the one where "cat" is denser ranks first.
        self.assertIn(0, ranked)
        self.assertIn(1, ranked)
        # doc 0 is shorter and "cat" is a larger fraction of it -> higher BM25.
        self.assertEqual(ranked[0], 0)
        # The unrelated compliance doc never matches a "cat" query.
        self.assertNotIn(2, ranked)

    def test_no_match_returns_empty(self) -> None:
        passages = [_kb("alpha beta gamma"), _kb("delta epsilon")]
        bm25 = BM25(passages)
        self.assertEqual(bm25.score("nonexistentterm"), [])
        # Empty query / empty corpus are both inert.
        self.assertEqual(bm25.score(""), [])
        self.assertEqual(BM25([]).score("alpha"), [])

    def test_k_truncates_top_results(self) -> None:
        passages = [
            _kb("apple apple apple"),
            _kb("apple apple"),
            _kb("apple"),
        ]
        bm25 = BM25(passages)
        top1 = bm25.score("apple", k=1)
        self.assertEqual(len(top1), 1)
        # Highest term frequency wins the single slot.
        self.assertEqual(top1[0][0], 0)


class ExactMatchBoostTest(unittest.TestCase):
    def test_rtp_acronym_ranks_its_doc_first(self) -> None:
        # Two docs both mention "rate"; only one carries the RTP acronym. A bare
        # BM25 score on the shared word "rate" should be reordered so the RTP
        # doc wins once the jargon exact-match boost applies.
        passages = [
            _kb(
                "general payout rate guidance for the operations team and finance",
                source="generic",
            ),
            _kb("the RTP rate for this slot dropped below target", source="rtp"),
        ]
        bm25 = BM25(passages)

        query = "RTP rate"
        prelim = bm25.score(query, k=10)
        boosted = boost_scores(query, passages, prelim)
        ranked = _ranking(boosted)

        self.assertEqual(ranked[0], 1, "the doc containing RTP must rank first")
        # Boost only reweights what BM25 matched; it never invents documents.
        self.assertEqual(set(ranked), set(_ranking(prelim)))

    def test_kyc_jargon_promotes_match(self) -> None:
        passages = [
            _kb("onboarding checklist and account verification steps", source="generic"),
            _kb("KYC verification failed for the new account", source="kyc"),
        ]
        bm25 = BM25(passages)
        query = "KYC verification"
        boosted = boost_scores(query, passages, bm25.score(query, k=10))
        self.assertEqual(_ranking(boosted)[0], 1)

    def test_identifier_token_boosted(self) -> None:
        # Identifier-shaped tokens (ticket-123) carry strong signal even though
        # they are not in the jargon list.
        passages = [
            _kb("see the related ticket for context on the deposit problem"),
            _kb("ticket-123 tracks the deposit failure investigation"),
        ]
        bm25 = BM25(passages)
        query = "ticket-123 deposit"
        boosted = boost_scores(query, passages, bm25.score(query, k=10))
        self.assertEqual(_ranking(boosted)[0], 1)

    def test_boost_preserves_empty_input(self) -> None:
        self.assertEqual(boost_scores("rtp", [], []), [])


class RecencyBoostTest(unittest.TestCase):
    def test_freshest_chat_snippet_outranks_stale_twin(self) -> None:
        # Two near-identical chat snippets (same lexical content -> same BM25);
        # the recency boost must break the tie toward the newer create_time.
        passages = [
            _chat("payout delays reported by users", "msgs/old", "2026-06-10T08:00:00Z"),
            _chat("payout delays reported by users", "msgs/new", "2026-06-13T08:00:00Z"),
        ]
        bm25 = BM25(passages)
        query = "payout delays"
        prelim = bm25.score(query, k=10)
        # BM25 alone ties them (identical text); recency must decide the order.
        self.assertAlmostEqual(prelim[0][1], prelim[1][1])

        boosted = boost_scores(query, passages, prelim)
        self.assertEqual(_ranking(boosted)[0], 1, "newest chat snippet ranks first")

    def test_kb_passages_get_no_recency_bump(self) -> None:
        # KB docs have no create_time -> recency never reorders them; identical
        # text keeps BM25's (stable) ordering.
        passages = [
            _kb("payout delays reported", source="kb-a"),
            _kb("payout delays reported", source="kb-b"),
        ]
        bm25 = BM25(passages)
        query = "payout delays"
        prelim = bm25.score(query, k=10)
        boosted = boost_scores(query, passages, prelim)
        self.assertAlmostEqual(boosted[0][1], boosted[1][1])


class RrfFuseTest(unittest.TestCase):
    def test_consensus_item_ranks_first(self) -> None:
        # List A: 0 best; List B: 0 also high -> 0 should win the fusion.
        list_a = [(0, 9.0), (1, 5.0), (2, 1.0)]
        list_b = [(0, 8.0), (2, 4.0), (1, 0.5)]
        fused = rrf_fuse(list_a, list_b)
        ranked = _ranking(fused)
        self.assertEqual(ranked[0], 0)
        # All distinct indices from both lists are present exactly once.
        self.assertEqual(sorted(ranked), [0, 1, 2])
        self.assertEqual(len(ranked), len(set(ranked)))

    def test_scores_used_only_for_within_list_order(self) -> None:
        # An item ranked #1 in both lists beats one ranked #1 in only one list,
        # regardless of the raw float magnitudes (RRF uses rank, not score).
        list_a = [(7, 0.01), (8, 0.001)]  # 7 first, 8 second
        list_b = [(7, 1000.0), (9, 999.0)]  # 7 first, 9 second
        fused = rrf_fuse(list_a, list_b)
        # 7 appears first in both lists -> highest fused score.
        self.assertEqual(_ranking(fused)[0], 7)

    def test_top_k_truncation_and_empty(self) -> None:
        fused = rrf_fuse([(0, 1.0), (1, 0.5), (2, 0.1)], [(2, 1.0)], top_k=2)
        self.assertEqual(len(fused), 2)
        self.assertEqual(rrf_fuse(), [])
        self.assertEqual(rrf_fuse([], []), [])


class BuildRetrieverBypassTest(unittest.TestCase):
    def test_empty_kb_and_no_history_returns_none(self) -> None:
        # The empty-KB bypass (§3): nothing to index -> None so the analyzer
        # sends the full transcript directly instead of through a lossy index.
        self.assertIsNone(build_retriever("", None))
        self.assertIsNone(build_retriever("/nonexistent/dir/does-not-exist", []))

    def test_history_only_builds_a_working_retriever(self) -> None:
        # No KB dir, but recent chat history is enough to build an index.
        history = [
            Message(
                id="msgs/1",
                space="spaces/X",
                thread_id="threads/1",
                sender="users/staff-1",
                sender_type=SenderType.HUMAN,
                text="users are reporting KYC verification failures at signup",
                create_time="2026-06-13T08:00:00Z",
            ),
            Message(
                id="msgs/2",
                space="spaces/X",
                thread_id="threads/2",
                sender="users/staff-2",
                sender_type=SenderType.HUMAN,
                text="the lobby is showing stale jackpot totals",
                create_time="2026-06-13T08:05:00Z",
            ),
        ]
        retriever = build_retriever("", history)
        self.assertIsNotNone(retriever)
        assert retriever is not None  # narrow for type-checkers
        self.assertIsInstance(retriever, Retriever)

        hits = retriever.retrieve("KYC verification", k=1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "chat")
        self.assertIn("kyc", hits[0].text.lower())


if __name__ == "__main__":
    unittest.main()
