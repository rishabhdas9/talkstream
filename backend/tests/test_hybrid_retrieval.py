from __future__ import annotations

import shutil

import pytest
from sqlalchemy import text
from test_knowledge_sources_api import FakeEmbeddingClient, FakeVectorStore

from app import migrations
from app.config import Settings
from app.db import Database
from app.knowledge.hybrid import fts_query, merge_ranked
from app.knowledge.service import KnowledgeService
from app.models import User


def test_rrf_rewards_agreement():
    result = merge_ranked(["a", "b"], ["b", "c"], limit=3)
    assert result[0] == "b"
    assert result == ["b", "a", "c"]


def test_rrf_uses_one_based_ranks_stable_ties_and_limit():
    assert merge_ranked(["b"], ["a"], limit=2) == ["a", "b"]  # equal 1/61 scores: ID order
    assert merge_ranked(["a", "b", "c"], [], limit=2) == ["a", "b"]
    assert merge_ranked([], [], limit=5) == []
    assert merge_ranked(["a", "a"], ["a"], limit=5) == ["a"]


HOSTILE = [
    "NEAR(a b)",
    '"quoted" OR *',
    "ts-guest AND NOT x",
    "किराया ₹18,000?",
    "col:value ^start",
]


@pytest.mark.parametrize("query", HOSTILE)
def test_keyword_query_cannot_inject_fts_syntax(query):
    expression = fts_query(query)
    assert expression is not None
    assert all(part.startswith('"') and part.endswith('"') for part in expression.split(" OR "))


def test_query_without_terms_has_no_keyword_search():
    assert fts_query("?? !! ...") is None


class MissingVectors(FakeVectorStore):
    """A vector side that finds nothing, so any hit must come from the keyword index."""

    async def search(self, **kwargs):
        await super().search(**kwargs)
        return []


@pytest.fixture
async def hybrid_rig(database, settings):
    configured = settings.model_copy(
        update={"openai_api_key": "test-key", "knowledge_retrieval_mode": "hybrid"}
    )
    service = KnowledgeService(
        database=database,
        settings=configured,
        embedding_client=FakeEmbeddingClient(),
        vector_store=MissingVectors(),
    )
    users = []
    async with database.session_factory() as session:
        for email in ("current@example.com", "foreign@example.com"):
            user = User(email=email, is_verified=True)
            session.add(user)
            users.append(user)
        await session.commit()
    for user in users:
        await service.save_profile(
            user_id=user.id, provider_id="openai", model_id="text-embedding-3-small"
        )
    current, foreign = (user.id for user in users)
    await service.add_text_source(user_id=current, name="Policy", text="Refunds take five days.")
    await service.add_text_source(user_id=foreign, name="Secret", text="Vault code ZX-42 zebra")
    own = await service.add_text_source(user_id=current, name="Codes", text="Gate code ZX-42 zebra")
    return service, current, own


async def ids(service, user_id, query="zebra ZX-42"):
    return [
        hit.chunk_id
        for hit in await service.search_knowledge(user_id=user_id, query=query, limit=5)
    ]


async def test_keyword_finds_an_exact_identifier_the_vector_side_missed(hybrid_rig):
    service, current, own = hybrid_rig
    hits = await service.search_knowledge(user_id=current, query="what is ZX-42?", limit=5)
    assert [hit.source_name for hit in hits] == ["Codes"]
    assert hits[0].source_id == own.id and hits[0].excerpt == "Gate code ZX-42 zebra"
    assert hits[0].score == pytest.approx(1 / 61)  # fused rank score, not a missing similarity
    vector_only = KnowledgeService(
        database=service.database,
        settings=service.settings.model_copy(update={"knowledge_retrieval_mode": "vector"}),
        embedding_client=service.embedding_client,
        vector_store=service.vector_store,
    )
    assert await ids(vector_only, current) == []


@pytest.mark.parametrize("status", ["deleting", "failed", "processing"])
async def test_keyword_hits_cannot_bypass_owner_or_availability(hybrid_rig, status):
    service, current, own = hybrid_rig
    async with service.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE knowledge_sources SET status=:status WHERE id=:id"),
            {"status": status, "id": own.id},
        )
    assert await ids(service, current) == [], "foreign or unavailable chunks must stay hidden"


async def test_deletion_removes_keyword_entries(hybrid_rig):
    service, current, own = hybrid_rig
    await service.delete_source(user_id=current, source_id=own.id)
    async with service.database.engine.connect() as connection:
        remaining = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM knowledge_chunks_fts WHERE knowledge_chunks_fts MATCH 'zebra'"
            )
        )
    assert remaining == 1, "only the other user's chunk remains indexed"
    assert await ids(service, current) == []


def test_vector_retrieval_remains_the_default():
    assert Settings(_env_file=None, jwt_secret="x" * 32).knowledge_retrieval_mode == "vector"


async def test_keyword_index_backfills_existing_chunks(tmp_path, monkeypatch):
    current_migrations = migrations.MIGRATION_DIR
    earlier = tmp_path / "sql"
    earlier.mkdir()
    for path in sorted(current_migrations.glob("*.sql")):
        if path.name < "012":
            shutil.copy(path, earlier / path.name)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.db'}")
    try:
        monkeypatch.setattr(migrations, "MIGRATION_DIR", earlier)
        await migrations.apply_migrations(database)
        async with database.engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO users VALUES ('u','u@example.com',CURRENT_TIMESTAMP,1)")
            )
            await connection.execute(
                text(
                    "INSERT INTO knowledge_sources (id,user_id,name,media_type,content_sha256,"
                    "extracted_text,character_count,chunk_count,status,created_at) VALUES "
                    "('s','u','Old','text/plain','h','legacy zebra',12,1,'indexed',"
                    "CURRENT_TIMESTAMP)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO knowledge_chunks (id,source_id,user_id,ordinal,content,created_at)"
                    " VALUES ('c','s','u',0,'legacy zebra',CURRENT_TIMESTAMP)"
                )
            )
        monkeypatch.setattr(migrations, "MIGRATION_DIR", current_migrations)
        await migrations.apply_migrations(database)
        async with database.engine.connect() as connection:
            found = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM knowledge_chunks_fts"
                    " WHERE knowledge_chunks_fts MATCH 'zebra'"
                )
            )
        assert found == 1
    finally:
        await database.dispose()


@pytest.mark.parametrize("query", HOSTILE)
async def test_hostile_queries_run_as_plain_terms(hybrid_rig, query):
    service, current, _ = hybrid_rig
    await service.search_knowledge(user_id=current, query=query, limit=5)  # no FTS5 syntax error
