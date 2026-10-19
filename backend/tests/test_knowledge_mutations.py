from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from test_knowledge_sources_api import FakeEmbeddingClient, FakeVectorStore

from app.knowledge.models import KnowledgeSource
from app.knowledge.schemas import KnowledgeSourceResponse
from app.knowledge.service import KnowledgeReindexInProgressError, KnowledgeService
from app.models import User


class ControlledEmbeddings(FakeEmbeddingClient):
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def embed(self, *args, **kwargs):
        self.calls += 1
        if self.block:
            self.entered.set()
            await self.release.wait()
        return await super().embed(*args, **kwargs)


class ControlledVectors(FakeVectorStore):
    def __init__(self):
        super().__init__()
        self.fail_delete = False
        self.fail_after_delete = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block_delete = False
        self.block_search = False
        self.search_entered = asyncio.Event()
        self.search_release = asyncio.Event()
        self.forged_hits = None

    async def search(self, **kwargs):
        hits = self.forged_hits if self.forged_hits is not None else await super().search(**kwargs)
        if self.block_search:
            # Hold already-fetched hits while mutations land, as a slow vector response would.
            self.search_entered.set()
            await self.search_release.wait()
        return hits

    async def delete_source(self, **kwargs):
        if self.block_delete:
            self.entered.set()
            await self.release.wait()
        if self.fail_delete:
            raise RuntimeError("injected vector cleanup failure")
        await super().delete_source(**kwargs)
        if self.fail_after_delete:
            raise RuntimeError("injected lost vector acknowledgement")


@pytest.fixture
async def knowledge_rig(database, settings):
    settings = settings.model_copy(update={"openai_api_key": "test-key"})
    embeddings, vectors = ControlledEmbeddings(), ControlledVectors()
    service = KnowledgeService(
        database=database, settings=settings, embedding_client=embeddings, vector_store=vectors
    )
    async with database.session_factory() as session:
        user = User(email="mutations@example.com", is_verified=True)
        session.add(user)
        await session.commit()
    await service.save_profile(
        user_id=user.id, provider_id="openai", model_id="text-embedding-3-small"
    )
    return service, user.id, embeddings, vectors


async def add_source(rig):
    service, user_id, _, _ = rig
    return await service.add_text_source(user_id=user_id, name="Private", text="private phrase")


@pytest.mark.parametrize("after", [False, True])
async def test_failed_deletion_is_unavailable_and_retryable(knowledge_rig, after):
    service, user, _, vectors = knowledge_rig
    source = await add_source(knowledge_rig)
    vectors.fail_delete, vectors.fail_after_delete = not after, after
    with pytest.raises(RuntimeError):
        await service.delete_source(user_id=user, source_id=source.id)
    async with service.database.session_factory() as session:
        pending = await session.get(KnowledgeSource, source.id)
        assert pending is not None, "cleanup failure must retain a durable retry record"
        assert pending.status.value == "deleting"
        public = KnowledgeSourceResponse.model_validate(pending)
        assert public.status == "failed" and "retry" in public.error_message.lower()
    assert await service.search_knowledge(user_id=user, query="private phrase", limit=5) == []
    vectors.fail_delete = vectors.fail_after_delete = False
    restarted = KnowledgeService(
        database=service.database,
        settings=service.settings,
        embedding_client=service.embedding_client,
        vector_store=vectors,
    )
    await restarted.run_pending_jobs()
    assert await restarted.list_sources(user) == []
    assert vectors.points == {}
    async with service.database.engine.connect() as connection:
        event = (
            (await connection.execute(text("SELECT * FROM knowledge_deletion_events")))
            .mappings()
            .one()
        )
        assert event["source_id"] == source.id and event["user_id"] == user
        assert event["completed_at"] is not None
        assert set(event) == {"id", "user_id", "source_id", "created_at", "completed_at"}


async def test_empty_search_skips_embeddings(knowledge_rig):
    service, user, embeddings, _ = knowledge_rig
    assert await service.search_knowledge(user_id=user, query="question", limit=5) == []
    assert embeddings.calls == 0


@pytest.mark.parametrize("mutation", ["upload", "profile", "delete"])
async def test_upload_rejects_incompatible_mutations(knowledge_rig, mutation):
    service, user, embeddings, _ = knowledge_rig
    embeddings.block = True
    task = asyncio.create_task(add_source(knowledge_rig))
    await embeddings.entered.wait()
    try:
        async with asyncio.timeout(0.2):
            with pytest.raises(KnowledgeReindexInProgressError):
                if mutation == "upload":
                    await add_source(knowledge_rig)
                elif mutation == "profile":
                    await service.save_profile(
                        user_id=user, provider_id="openai", model_id="text-embedding-3-large"
                    )
                else:
                    source = (await service.list_sources(user))[0]
                    await service.delete_source(user_id=user, source_id=source.id)
    finally:
        embeddings.release.set()
        await task


async def test_sql_admission_excludes_mutations_across_service_instances(knowledge_rig):
    service, user, embeddings, vectors = knowledge_rig
    embeddings.block = True
    task = asyncio.create_task(add_source(knowledge_rig))
    await embeddings.entered.wait()
    other = KnowledgeService(
        database=service.database,
        settings=service.settings,
        embedding_client=embeddings,
        vector_store=vectors,
    )
    try:
        with pytest.raises(KnowledgeReindexInProgressError):
            await other.save_profile(
                user_id=user, provider_id="openai", model_id="text-embedding-3-large"
            )
    finally:
        embeddings.release.set()
        await task


async def test_pending_deletion_excludes_new_upload_and_profile(knowledge_rig):
    service, user, _, vectors = knowledge_rig
    source = await add_source(knowledge_rig)
    vectors.fail_delete = True
    with pytest.raises(RuntimeError):
        await service.delete_source(user_id=user, source_id=source.id)
    with pytest.raises(KnowledgeReindexInProgressError):
        await add_source(knowledge_rig)
    with pytest.raises(KnowledgeReindexInProgressError):
        await service.save_profile(
            user_id=user, provider_id="openai", model_id="text-embedding-3-large"
        )


async def test_unavailable_vectors_do_not_complete_deletion(knowledge_rig):
    service, user, _, _ = knowledge_rig
    source = await add_source(knowledge_rig)
    service.vector_store = None
    with pytest.raises(RuntimeError):
        await service.delete_source(user_id=user, source_id=source.id)
    assert (await service.list_sources(user))[0].status.value == "deleting"


async def test_upload_never_reactivates_a_source_marked_deleting(knowledge_rig):
    service, user, embeddings, _ = knowledge_rig
    embeddings.block = True
    task = asyncio.create_task(add_source(knowledge_rig))
    await embeddings.entered.wait()
    async with service.database.engine.begin() as connection:
        await connection.execute(text("UPDATE knowledge_sources SET status='deleting'"))
    embeddings.release.set()
    with pytest.raises(RuntimeError):
        await task
    assert [source.status.value for source in await service.list_sources(user)] == ["deleting"]
    assert await service.search_knowledge(user_id=user, query="private phrase", limit=5) == []


async def test_search_drops_hits_outside_owner_or_active_generation(knowledge_rig):
    service, user, _, vectors = knowledge_rig
    await add_source(knowledge_rig)
    generation = (await service.get_active_profile(user)).generation_id
    chunk_id = next(iter(vectors.points.values()))["point"].payload["chunk_id"]
    vectors.forged_hits = [
        {"score": 0.9, "payload": {"user_id": "intruder", "generation_id": generation}},
        {"score": 0.9, "payload": {"user_id": "intruder", "chunk_id": chunk_id}},
        {
            "score": 0.8,
            "payload": {"user_id": user, "generation_id": "stale", "chunk_id": chunk_id},
        },
        {"score": 0.7, "payload": None},
    ]
    assert await service.search_knowledge(user_id=user, query="private phrase", limit=5) == []
    vectors.forged_hits = None
    assert len(await service.search_knowledge(user_id=user, query="private phrase", limit=5)) == 1


async def test_generation_switch_during_search_returns_no_stale_content(knowledge_rig):
    service, user, _, vectors = knowledge_rig
    await add_source(knowledge_rig)
    vectors.block_search = True
    search = asyncio.create_task(
        service.search_knowledge(user_id=user, query="private phrase", limit=5)
    )
    await vectors.search_entered.wait()
    try:
        await service.save_profile(
            user_id=user, provider_id="openai", model_id="text-embedding-3-large"
        )
        await service.run_pending_jobs()
        assert (await service.get_active_profile(user)).model_id == "text-embedding-3-large"
    finally:
        vectors.search_release.set()
    assert await search == []


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_deletion_during_search_returns_no_deleted_content(knowledge_rig, cleanup_fails):
    service, user, _, vectors = knowledge_rig
    source = await add_source(knowledge_rig)
    vectors.block_search = True
    search = asyncio.create_task(
        service.search_knowledge(user_id=user, query="private phrase", limit=5)
    )
    await vectors.search_entered.wait()
    vectors.fail_delete = cleanup_fails
    try:
        if cleanup_fails:
            with pytest.raises(RuntimeError):
                await service.delete_source(user_id=user, source_id=source.id)
        else:
            await service.delete_source(user_id=user, source_id=source.id)
    finally:
        vectors.search_release.set()
    assert await search == []
