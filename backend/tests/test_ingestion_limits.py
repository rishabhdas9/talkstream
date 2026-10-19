from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from test_knowledge_sources_api import FakeEmbeddingClient, FakeVectorStore

from app.knowledge.admission import IngestionCapacityError
from app.knowledge.service import KnowledgeService, KnowledgeSourceTooLargeError
from app.models import User
from app.runtime import RuntimePolicy


@pytest.fixture
async def ingestion_rig(database, settings):
    async with database.session_factory() as session:
        session.add(User(id="u", email="u@example.com"))
        await session.commit()
    state = SimpleNamespace(calls=0, block=False, fail=False)
    entered = asyncio.Event()

    class Client(FakeEmbeddingClient):
        async def embed(self, *args, **kwargs):
            state.calls += 1
            entered.set()
            if state.block:
                await asyncio.Event().wait()
            if state.fail:
                raise RuntimeError("untrusted-private-provider-detail")
            return await super().embed(*args, **kwargs)

    service = KnowledgeService(
        database=database,
        settings=settings.model_copy(update={"openai_api_key": "test-key"}),
        embedding_client=Client(),
        vector_store=FakeVectorStore(),
    )
    await service.save_profile(user_id="u", provider_id="openai", model_id="text-embedding-3-small")
    return SimpleNamespace(service=service, state=state, entered=entered, database=database)


async def add(rig, value="Document text"):
    return await rig.service.add_text_source(user_id="u", name="Document", text=value)


async def test_character_ceiling_and_third_upload_reject_before_embedding(ingestion_rig):
    rig = ingestion_rig
    with pytest.raises(KnowledgeSourceTooLargeError):
        await add(rig, "x" * 100001)
    assert rig.state.calls == 0
    await add(rig)
    await add(rig)
    with pytest.raises(IngestionCapacityError, match="daily"):
        await add(rig)
    assert rig.state.calls == 2


async def test_source_keeps_original_hash_and_rebuild_versions(ingestion_rig):
    source = await add(ingestion_rig, "  original source \n")
    assert source.source_sha256 == hashlib.sha256(b"  original source \n").hexdigest()
    assert source.content_sha256 == hashlib.sha256(b"original source").hexdigest()
    assert source.extraction_version == "text:utf8:v1"
    assert source.chunking_version == "chunker-v1:1200:150"


async def test_failed_ingestion_is_not_retrievable_and_error_is_safe(ingestion_rig):
    rig = ingestion_rig
    rig.state.fail = True
    with pytest.raises(RuntimeError):
        await add(rig)
    assert await rig.service.search_knowledge(user_id="u", query="document", limit=4) == []
    sources = await rig.service.list_sources("u")
    assert len(sources) == 1
    assert sources[0].status.value == "failed"
    assert "untrusted-private-provider-detail" not in (sources[0].error_message or "")
    async with rig.database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT state FROM knowledge_ingestions"))
        ).scalar_one() == "failed"


async def test_cancelled_ingestion_keeps_used_capacity_and_no_visible_index(ingestion_rig):
    rig = ingestion_rig
    rig.state.block = True
    work = asyncio.create_task(add(rig))
    await rig.entered.wait()
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    sources = await rig.service.list_sources("u")
    assert sources[0].status.value == "failed"
    assert await rig.service.search_knowledge(user_id="u", query="document", limit=4) == []
    async with rig.database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT state FROM knowledge_ingestions"))
        ).scalar_one() == "failed"


async def test_service_global_capacity_denial_skips_embedding(ingestion_rig):
    rig = ingestion_rig
    rig.service.admission.policy = RuntimePolicy(stored_chunks=1)
    await add(rig)
    with pytest.raises(IngestionCapacityError, match="chunk"):
        await add(rig)
    assert rig.state.calls == 1


async def test_file_entrypoint_persists_original_hash_and_extraction_version(ingestion_rig):
    from test_bounded_extraction import pdf_bytes

    rig = ingestion_rig
    data = pdf_bytes(text=b"Refund policy")
    source = await rig.service.add_file_source(user_id="u", name="policy.pdf", data=data)
    assert source.source_sha256 == hashlib.sha256(data).hexdigest()
    assert source.extraction_version.startswith("pypdf:")
    assert source.extracted_text == "Refund policy"
    async with rig.database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT state FROM knowledge_ingestions"))
        ).scalar_one() == "complete"


async def test_file_extraction_failure_and_cancellation_finish_admission(
    ingestion_rig, monkeypatch
):
    from app.knowledge import service as module
    from app.knowledge.ingestion import ExtractionTimeoutError

    rig = ingestion_rig

    async def timeout(*args, **kwargs):
        raise ExtractionTimeoutError("PDF extraction timed out")

    monkeypatch.setattr(module, "extract_file_bounded", timeout)
    with pytest.raises(ExtractionTimeoutError):
        await rig.service.add_file_source(user_id="u", name="policy.pdf", data=b"pdf")
    started = asyncio.Event()

    async def block(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "extract_file_bounded", block)
    work = asyncio.create_task(
        rig.service.add_file_source(user_id="u", name="policy.pdf", data=b"pdf")
    )
    await started.wait()
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert await rig.service.list_sources("u") == []
    assert rig.state.calls == 0
    async with rig.database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT state FROM knowledge_ingestions"))
        ).scalars().all() == ["failed", "failed"]


async def test_actual_upload_routes_enforce_file_pages_chars_and_daily_limits(ingestion_rig):
    import httpx
    from test_bounded_extraction import pdf_bytes

    from app.auth.dependencies import get_current_user
    from app.main import create_app

    rig = ingestion_rig
    app = create_app(rig.service.settings.model_copy(update={"knowledge_rag_enabled": True}))
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u")
    async with app.router.lifespan_context(app):
        app.state.knowledge_service = rig.service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            oversized = await client.post(
                "/knowledge/sources/file",
                files={"file": ("big.txt", b" " * (5 * 1024 * 1024 + 1), "text/plain")},
            )
            assert oversized.status_code == 413
            long_text = await client.post(
                "/knowledge/sources/text", json={"name": "Long", "text": "x" * 100001}
            )
            assert long_text.status_code == 413
            pages = await client.post(
                "/knowledge/sources/file",
                files={"file": ("many.pdf", pdf_bytes(51), "application/pdf")},
            )
            assert pages.status_code == 413
            assert rig.state.calls == 0
            accepted = await client.post(
                "/knowledge/sources/file",
                files={"file": ("notes.md", b"# Notes\nA safe source.", "text/markdown")},
            )
            assert accepted.status_code == 201
            third = await client.post(
                "/knowledge/sources/text", json={"name": "Third", "text": "Another source"}
            )
            assert third.status_code == 429
            assert rig.state.calls == 1
