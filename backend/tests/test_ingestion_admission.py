from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text
from test_cost_service import clock as clock

from app.knowledge.admission import IngestionAdmission, IngestionCapacityError
from app.knowledge.models import KnowledgeChunk, KnowledgeSource, KnowledgeSourceStatus
from app.models import User, new_id
from app.runtime import RuntimePolicy


@pytest.fixture
async def admission(database, clock):
    async with database.session_factory() as session:
        session.add_all([User(id="u", email="u@example.com"), User(id="v", email="v@example.com")])
        await session.commit()
    return IngestionAdmission(
        database=database, clock=clock, policy=RuntimePolicy(), source_limit=10
    )


def source_with_chunks(user, contents="hello", chunks=1):
    source = KnowledgeSource(
        id=new_id(),
        user_id=user,
        name="Document",
        media_type="text/plain",
        content_sha256="a" * 64,
        extracted_text=contents,
        character_count=len(contents),
        chunk_count=chunks,
        status=KnowledgeSourceStatus.PROCESSING,
        source_sha256="b" * 64,
        extraction_version="utf8-v1",
        chunking_version="chunker-v1:1200:150",
    )
    values = [
        KnowledgeChunk(id=new_id(), source_id=source.id, user_id=user, ordinal=i, content="chunk")
        for i in range(chunks)
    ]
    return source, values


async def test_daily_capacity_is_persistent_atomic_and_unused_only_releases(admission):
    async def reserve():
        try:
            return await admission.reserve("u")
        except IngestionCapacityError:
            return None

    results = await asyncio.gather(*(reserve() for _ in range(3)))
    admitted = [value for value in results if value]
    assert len(admitted) == 2
    await admission.finish("u", admitted[0], succeeded=False)
    third = await admission.reserve("u")
    await admission.mark_started("u", third)
    await admission.finish("u", third, succeeded=False)
    with pytest.raises(IngestionCapacityError, match="daily"):
        await admission.reserve("u")
    admission.clock.now += timedelta(days=1)
    assert await admission.reserve("u")


async def test_source_slots_include_active_admissions(admission, database):
    async with database.session_factory() as session:
        for _ in range(9):
            source, _ = source_with_chunks("u", chunks=0)
            session.add(source)
        await session.commit()
    await admission.reserve("u")
    with pytest.raises(IngestionCapacityError, match="source"):
        await admission.reserve("u")
    assert await admission.reserve("v")


async def test_global_chunk_capacity_cannot_be_overbooked(admission, database):
    admission.policy = RuntimePolicy(stored_chunks=3)
    ids = {user: await admission.reserve(user) for user in ("u", "v")}

    async def store(user):
        source, chunks = source_with_chunks(user, chunks=2)
        try:
            await admission.store(user, ids[user], source, chunks)
            return True
        except IngestionCapacityError:
            return False

    results = await asyncio.gather(store("u"), store("v"))
    assert sum(results) == 1
    async with database.engine.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM knowledge_chunks"))).scalar_one() == 2
        assert (
            await conn.execute(text("SELECT COUNT(*) FROM knowledge_sources"))
        ).scalar_one() == 1


async def test_extracted_text_capacity_counts_unicode_bytes_and_keeps_other_user(
    admission, database
):
    admission.policy = RuntimePolicy(extracted_bytes=5)
    first = await admission.reserve("v")
    source, chunks = source_with_chunks("v", contents="ह")
    await admission.store("v", first, source, chunks)
    second = await admission.reserve("u")
    source2, chunks2 = source_with_chunks("u", contents="ह")
    with pytest.raises(IngestionCapacityError, match="storage"):
        await admission.store("u", second, source2, chunks2)
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT user_id FROM knowledge_sources"))
        ).scalar_one() == "v"


async def test_admission_cannot_cross_users(admission, database):
    reserved = await admission.reserve("u")
    source, chunks = source_with_chunks("v")
    with pytest.raises(ValueError):
        await admission.store("v", reserved, source, chunks)
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT COUNT(*) FROM knowledge_sources"))
        ).scalar_one() == 0


async def test_startup_recovery_releases_unused_but_keeps_started_usage(admission, database):
    unused = await admission.reserve("u")
    started = await admission.reserve("u")
    await admission.mark_started("u", started)
    stored = await admission.reserve("v")
    source, chunks = source_with_chunks("v")
    await admission.store("v", stored, source, chunks)
    await admission.recover()
    await admission.recover()
    async with database.engine.connect() as conn:
        rows = dict((await conn.execute(text("SELECT id,state FROM knowledge_ingestions"))).all())
        status = (await conn.execute(text("SELECT status FROM knowledge_sources"))).scalar_one()
    assert rows == {unused: "released", started: "failed", stored: "failed"}
    assert status == "failed"
    assert await admission.reserve("u")
    with pytest.raises(IngestionCapacityError):
        await admission.reserve("u")


async def test_real_2500_chunk_ceiling_survives_concurrent_users(admission, database):
    assert admission.policy.stored_chunks == 2500
    baseline, chunks = source_with_chunks("v", chunks=2499)
    async with database.session_factory() as session:
        session.add(baseline)
        await session.flush()
        session.add_all(chunks)
        await session.commit()
    reservations = {user: await admission.reserve(user) for user in ("u", "v")}

    async def upload(user):
        source, chunks = source_with_chunks(user)
        try:
            await admission.store(user, reservations[user], source, chunks)
            return True
        except IngestionCapacityError:
            return False

    assert sum(await asyncio.gather(upload("u"), upload("v"))) == 1
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT COUNT(*) FROM knowledge_chunks"))
        ).scalar_one() == 2500
        assert (
            await conn.execute(
                text("SELECT COUNT(*) FROM knowledge_sources WHERE id=:id"), {"id": baseline.id}
            )
        ).scalar_one() == 1


async def test_failed_capacity_transition_keeps_reservation_and_no_partial_rows(
    admission, database
):
    admission.policy = RuntimePolicy(stored_chunks=1)
    reservation = await admission.reserve("u")
    source, chunks = source_with_chunks("u", chunks=2)
    with pytest.raises(IngestionCapacityError):
        await admission.store("u", reservation, source, chunks)
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT COUNT(*) FROM knowledge_sources"))
        ).scalar_one() == 0
        assert (
            await conn.execute(text("SELECT state FROM knowledge_ingestions"))
        ).scalar_one() == "reserved"
    await admission.finish("u", reservation, succeeded=False)
    assert await admission.reserve("u")
