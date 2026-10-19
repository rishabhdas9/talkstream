"""Opt-in keyword side of retrieval; callers still recheck every chunk against owned SQL rows."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.models import KnowledgeSourceStatus

RRF_K = 60
MAX_TERMS = 32


def rrf_scores(vector_ids: list[str], keyword_ids: list[str]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranked in (vector_ids, keyword_ids):
        for rank, chunk_id in enumerate(dict.fromkeys(ranked), start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (RRF_K + rank)
    return scores


def merge_ranked(vector_ids: list[str], keyword_ids: list[str], limit: int) -> list[str]:
    scores = rrf_scores(vector_ids, keyword_ids)
    return sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:limit]


def fts_query(query: str) -> str | None:
    # Each quoted term is tokenized by the index's own tokenizer, so FTS5 syntax stays literal.
    terms = [term for term in query.split() if any(c.isalnum() for c in term)][:MAX_TERMS]
    if not terms:
        return None
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


async def keyword_chunk_ids(
    session: AsyncSession, *, user_id: str, query: str, limit: int
) -> list[str]:
    expression = fts_query(query)
    if expression is None:
        return []
    rows = await session.execute(
        text(
            "SELECT c.id FROM knowledge_chunks_fts f"
            " JOIN knowledge_chunks c ON c.rowid = f.rowid"
            " JOIN knowledge_sources s ON s.id = c.source_id"
            " WHERE knowledge_chunks_fts MATCH :expression"
            " AND c.user_id = :user_id AND s.user_id = :user_id AND s.status = :status"
            " ORDER BY bm25(knowledge_chunks_fts), c.id LIMIT :limit"
        ),
        {
            "expression": expression,
            "user_id": user_id,
            "status": KnowledgeSourceStatus.INDEXED.value,
            "limit": limit,
        },
    )
    return list(rows.scalars())
