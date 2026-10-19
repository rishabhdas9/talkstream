from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import event, select, text
from test_knowledge_sources_api import FakeEmbeddingClient, FakeVectorStore

from app.knowledge.models import KnowledgeJobStatus, KnowledgeReindexJob
from app.knowledge.service import KnowledgeService
from app.models import User


class SwitchingEmbeddingClient:
    def __init__(self, *, fail_large: bool = False) -> None:
        self.fail_large = fail_large

    async def embed(self, texts, *, model_id: str, expected_dimensions: int):
        if self.fail_large and model_id == "text-embedding-3-large":
            raise RuntimeError("injected provider failure")
        return [[1.0] + [0.0] * (expected_dimensions - 1) for _ in texts]


class RecordingVectorStore:
    def __init__(self) -> None:
        self.points = []
        self.deleted_generations: list[str] = []

    async def upsert(self, *, collection: str, dimensions: int, points) -> None:
        del collection, dimensions
        self.points.extend(points)

    async def search(self, **kwargs):
        del kwargs
        return []

    async def delete_source(
        self,
        *,
        collection: str,
        user_id: str,
        source_id: str,
        generation_id: str,
    ) -> None:
        del collection, user_id, source_id
        self.deleted_generations.append(generation_id)


async def _user(database) -> User:
    async with database.session_factory() as session:
        user = User(email="switch@example.com", is_verified=True)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def test_reindex_switches_only_after_all_chunks_succeed(database, settings) -> None:
    configured = settings.model_copy(
        update={"knowledge_rag_enabled": True, "openai_api_key": "test-key"}
    )
    vectors = RecordingVectorStore()
    service = KnowledgeService(
        database=database,
        settings=configured,
        embedding_client=SwitchingEmbeddingClient(),
        vector_store=vectors,
    )
    user = await _user(database)
    old_profile = await service.save_profile(
        user_id=user.id,
        provider_id="openai",
        model_id="text-embedding-3-small",
    )
    await service.add_text_source(
        user_id=user.id,
        name="Policy",
        text="Refunds arrive after approval.",
    )

    pending = await service.save_profile(
        user_id=user.id,
        provider_id="openai",
        model_id="text-embedding-3-large",
    )
    assert pending.is_active is False
    assert pending.status.value == "pending"
    assert (await service.get_active_profile(user.id)).id == old_profile.id

    await service.run_pending_jobs()

    active = await service.get_active_profile(user.id)
    assert active.model_id == "text-embedding-3-large"
    assert active.generation_id == pending.generation_id
    assert old_profile.generation_id in vectors.deleted_generations
    async with database.session_factory() as session:
        job = (await session.execute(select(KnowledgeReindexJob))).scalar_one()
        assert job.status is KnowledgeJobStatus.COMPLETE
        assert job.processed_chunks == job.total_chunks == 1


async def test_failed_reindex_preserves_previous_active_profile(database, settings) -> None:
    configured = settings.model_copy(
        update={"knowledge_rag_enabled": True, "openai_api_key": "test-key"}
    )
    service = KnowledgeService(
        database=database,
        settings=configured,
        embedding_client=SwitchingEmbeddingClient(fail_large=True),
        vector_store=RecordingVectorStore(),
    )
    user = await _user(database)
    old_profile = await service.save_profile(
        user_id=user.id,
        provider_id="openai",
        model_id="text-embedding-3-small",
    )
    await service.add_text_source(user_id=user.id, name="Policy", text="Refund policy text.")
    await service.save_profile(
        user_id=user.id,
        provider_id="openai",
        model_id="text-embedding-3-large",
    )

    await service.run_pending_jobs()

    active = await service.get_active_profile(user.id)
    assert active.id == old_profile.id
    async with database.session_factory() as session:
        job = (await session.execute(select(KnowledgeReindexJob))).scalar_one()
        assert job.status is KnowledgeJobStatus.FAILED
        assert "provider failure" in (job.error_message or "")


async def test_new_service_instance_resumes_persisted_pending_job(database, settings) -> None:
    configured = settings.model_copy(
        update={"knowledge_rag_enabled": True, "openai_api_key": "test-key"}
    )
    vectors = RecordingVectorStore()
    first_service = KnowledgeService(
        database=database,
        settings=configured,
        embedding_client=SwitchingEmbeddingClient(),
        vector_store=vectors,
    )
    user = await _user(database)
    await first_service.save_profile(
        user_id=user.id,
        provider_id="openai",
        model_id="text-embedding-3-small",
    )
    await first_service.add_text_source(
        user_id=user.id,
        name="Policy",
        text="Persisted chunks can be rebuilt after restart.",
    )
    pending = await first_service.save_profile(
        user_id=user.id,
        provider_id="openai",
        model_id="text-embedding-3-large",
    )

    restarted_service = KnowledgeService(
        database=database,
        settings=configured,
        embedding_client=SwitchingEmbeddingClient(),
        vector_store=vectors,
    )
    await restarted_service.run_pending_jobs()

    active = await restarted_service.get_active_profile(user.id)
    assert active is not None
    assert active.id == pending.id
    job = await restarted_service.get_reindex_job(pending.id)
    assert job is not None
    assert job.status is KnowledgeJobStatus.COMPLETE
    assert job.processed_chunks == job.total_chunks == 1


SMALL, LARGE = "text-embedding-3-small", "text-embedding-3-large"
DOCUMENT = "refund approved " * 18  # three chunks of at most 100 characters


class Crash(BaseException):
    """Simulated process death: not handled as an ordinary job failure."""


class GatedEmbeddings(FakeEmbeddingClient):
    def __init__(self) -> None:
        self.calls = {SMALL: 0, LARGE: 0}
        self.crash_on_large_call: int | None = None
        self.pause_on_large_call: int | None = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def embed(self, texts, *, model_id: str, expected_dimensions: int):
        self.calls[model_id] += 1
        if model_id == LARGE and self.calls[LARGE] == self.crash_on_large_call:
            raise Crash
        if model_id == LARGE and self.calls[LARGE] == self.pause_on_large_call:
            self.entered.set()
            await self.release.wait()
        return await super().embed(
            texts, model_id=model_id, expected_dimensions=expected_dimensions
        )


@pytest.fixture
async def generations(database, settings):
    configured = settings.model_copy(
        update={
            "openai_api_key": "test-key",
            "knowledge_chunk_characters": 100,
            "knowledge_chunk_overlap": 0,
            "knowledge_embedding_batch_size": 1,
        }
    )
    embeddings, vectors = GatedEmbeddings(), FakeVectorStore()

    def service():
        return KnowledgeService(
            database=database,
            settings=configured,
            embedding_client=embeddings,
            vector_store=vectors,
        )

    first = service()
    user = await _user(database)
    old = await first.save_profile(user_id=user.id, provider_id="openai", model_id=SMALL)
    source = await first.add_text_source(user_id=user.id, name="Policy", text=DOCUMENT)
    assert source.chunk_count == 3
    return first, service, user.id, old, embeddings, vectors


def generation_points(vectors, generation_id):
    return [
        stored["point"]
        for stored in vectors.points.values()
        if stored["point"].payload["generation_id"] == generation_id
    ]


async def test_generation_point_ids_are_distinct_per_generation(generations):
    service, _, user, old, _, vectors = generations
    pending = await service.save_profile(user_id=user, provider_id="openai", model_id=LARGE)
    await service.run_pending_jobs()
    old_points = {
        p.payload["chunk_id"]: p.id for p in generation_points(vectors, old.generation_id)
    }
    new_points = {
        p.payload["chunk_id"]: p.id for p in generation_points(vectors, pending.generation_id)
    }
    assert set(new_points) == set(old_points) or not old_points
    for chunk_id, point_id in new_points.items():
        assert point_id == str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{pending.generation_id}:{chunk_id}")
        )
        assert point_id != chunk_id


async def test_reindex_keeps_old_generation_live_until_complete(generations):
    service, _, user, old, embeddings, vectors = generations
    embeddings.pause_on_large_call = 2
    pending = await service.save_profile(user_id=user, provider_id="openai", model_id=LARGE)
    job = asyncio.create_task(service.run_pending_jobs())
    await embeddings.entered.wait()
    try:
        assert (await service.get_active_profile(user)).id == old.id
        hits = await service.search_knowledge(user_id=user, query="refund", limit=5)
        assert len(hits) == 3
        assert len(generation_points(vectors, old.generation_id)) == 3
    finally:
        embeddings.release.set()
        await job
    assert (await service.get_active_profile(user)).id == pending.id
    assert generation_points(vectors, old.generation_id) == []
    assert len(generation_points(vectors, pending.generation_id)) == 3


async def test_crash_resumes_from_cursor_without_duplicate_points(generations):
    service, make_service, user, old, embeddings, vectors = generations
    embeddings.crash_on_large_call = 2
    pending = await service.save_profile(user_id=user, provider_id="openai", model_id=LARGE)
    with pytest.raises(Crash):
        await service.run_pending_jobs()
    job = await service.get_reindex_job(pending.id)
    assert job.status is KnowledgeJobStatus.RUNNING and job.processed_chunks == 1
    assert (await service.get_active_profile(user)).id == old.id

    embeddings.crash_on_large_call = None
    await make_service().run_pending_jobs()
    assert embeddings.calls[LARGE] == 4  # one before the crash, the crashed call, two resumed
    assert (await service.get_active_profile(user)).id == pending.id
    assert len(generation_points(vectors, pending.generation_id)) == 3


@pytest.mark.parametrize(
    "tamper",
    [
        "DELETE FROM knowledge_chunks WHERE ordinal=2",
        "UPDATE embedding_profiles SET model_id='text-embedding-3-small' WHERE is_active=0",
        "UPDATE embedding_profiles SET is_active=0",
        "UPDATE knowledge_reindex_jobs SET source_snapshot=NULL, profile_snapshot=NULL",
        "UPDATE knowledge_reindex_jobs SET processed_chunks=7",
    ],
)
async def test_resume_rejects_stale_snapshot(generations, tamper):
    service, make_service, user, old, embeddings, vectors = generations
    embeddings.crash_on_large_call = 2
    pending = await service.save_profile(user_id=user, provider_id="openai", model_id=LARGE)
    with pytest.raises(Crash):
        await service.run_pending_jobs()
    async with service.database.engine.begin() as connection:
        await connection.execute(text(tamper))
    embeddings.crash_on_large_call = None
    calls = embeddings.calls[LARGE]

    await make_service().run_pending_jobs()

    job = await service.get_reindex_job(pending.id)
    assert job.status is KnowledgeJobStatus.FAILED
    assert embeddings.calls[LARGE] == calls
    active = await service.get_active_profile(user)
    assert active is None or active.id == old.id


async def test_interrupted_activation_completes_on_resume_without_reembedding(generations):
    service, make_service, user, _, embeddings, _ = generations
    pending = await service.save_profile(user_id=user, provider_id="openai", model_id=LARGE)

    def crash_on_activation(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE embedding_profiles"):
            raise Crash

    engine = service.database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", crash_on_activation)
    try:
        with pytest.raises(Crash):
            await service.run_pending_jobs()
    finally:
        event.remove(engine, "before_cursor_execute", crash_on_activation)
    job = await service.get_reindex_job(pending.id)
    assert job.status is KnowledgeJobStatus.RUNNING and job.processed_chunks == 3
    assert (await service.get_active_profile(user)).id != pending.id

    calls = embeddings.calls[LARGE]
    await make_service().run_pending_jobs()
    assert embeddings.calls[LARGE] == calls
    assert (await service.get_active_profile(user)).id == pending.id


async def test_deletion_removes_points_from_every_generation(generations):
    service, _, user, old, _, vectors = generations
    await service.save_profile(user_id=user, provider_id="openai", model_id=LARGE)
    original_delete = vectors.delete_source

    async def lost_cleanup(**kwargs):
        raise RuntimeError("old generation cleanup failed")

    vectors.delete_source = lost_cleanup
    await service.run_pending_jobs()
    vectors.delete_source = original_delete
    again = await service.save_profile(user_id=user, provider_id="openai", model_id=SMALL)
    await service.run_pending_jobs()
    assert again.collection_name == old.collection_name
    assert len(generation_points(vectors, old.generation_id)) == 3, "stale points not overwritten"
    assert len(generation_points(vectors, again.generation_id)) == 3

    source = (await service.list_sources(user))[0]
    await service.delete_source(user_id=user, source_id=source.id)
    assert vectors.points == {}
