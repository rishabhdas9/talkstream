from __future__ import annotations

from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from app.db import Database
from app.knowledge.models import KnowledgeChunk, KnowledgeSource
from app.knowledge.mutations import ensure_mutation_available
from app.models import new_id
from app.runtime import Clock, RuntimePolicy


class IngestionCapacityError(ValueError):
    pass


class IngestionAdmission:
    def __init__(
        self, *, database: Database, clock: Clock, policy: RuntimePolicy, source_limit: int
    ):
        self.database = database
        self.clock = clock
        self.policy = policy
        self.source_limit = min(source_limit, policy.sources_per_user)

    @asynccontextmanager
    async def transaction(self):
        async with self.database.session_factory() as session:
            try:
                await session.execute(text("BEGIN IMMEDIATE"))
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def reserve(self, user_id: str) -> str:
        now = self.clock.utc_now()
        day = now.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        async with self.transaction() as session:
            await ensure_mutation_available(session, user_id, include_ingestions=False)
            sources = await session.scalar(
                select(func.count(KnowledgeSource.id)).where(KnowledgeSource.user_id == user_id)
            )
            pending = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM knowledge_ingestions WHERE user_id=:user AND "
                    "state IN ('reserved','started')"
                ),
                {"user": user_id},
            )
            if sources + pending >= self.source_limit:
                raise IngestionCapacityError("Knowledge source capacity is exhausted")
            uploads = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM knowledge_ingestions WHERE user_id=:user AND "
                    "day_key=:day AND state!='released'"
                ),
                {"user": user_id, "day": day},
            )
            if uploads >= self.policy.uploads_per_user_day:
                raise IngestionCapacityError("Knowledge daily upload allowance is exhausted")
            admission_id = new_id()
            await session.execute(
                text(
                    "INSERT INTO knowledge_ingestions(id,user_id,day_key,state,created_at) "
                    "VALUES(:id,:user,:day,'reserved',:now)"
                ),
                {"id": admission_id, "user": user_id, "day": day, "now": now.isoformat()},
            )
        return admission_id

    async def mark_started(self, user_id: str, admission_id: str) -> None:
        async with self.transaction() as session:
            result = await session.execute(
                text(
                    "UPDATE knowledge_ingestions SET state='started' WHERE id=:id AND "
                    "user_id=:user AND state='reserved'"
                ),
                {"id": admission_id, "user": user_id},
            )
            if result.rowcount != 1:
                raise ValueError("Knowledge ingestion admission is unavailable")

    async def store(
        self, user_id: str, admission_id: str, source: KnowledgeSource, chunks: list[KnowledgeChunk]
    ) -> None:
        if source.user_id != user_id or any(
            chunk.user_id != user_id or chunk.source_id != source.id for chunk in chunks
        ):
            raise ValueError("Knowledge ingestion owner differs")
        async with self.transaction() as session:
            state = await session.scalar(
                text("SELECT state FROM knowledge_ingestions WHERE id=:id AND user_id=:user"),
                {"id": admission_id, "user": user_id},
            )
            if state not in {"reserved", "started"}:
                raise ValueError("Knowledge ingestion admission is unavailable")
            stored_chunks = await session.scalar(select(func.count(KnowledgeChunk.id)))
            if stored_chunks + len(chunks) > self.policy.stored_chunks:
                raise IngestionCapacityError("Knowledge global chunk capacity is exhausted")
            stored_bytes = await session.scalar(
                text(
                    "SELECT COALESCE(SUM(LENGTH(CAST(extracted_text AS BLOB))),0) FROM "
                    "knowledge_sources"
                )
            )
            if (
                stored_bytes + len(source.extracted_text.encode("utf-8"))
                > self.policy.extracted_bytes
            ):
                raise IngestionCapacityError("Knowledge global text storage capacity is exhausted")
            session.add(source)
            await session.flush()
            session.add_all(chunks)
            await session.execute(
                text(
                    "UPDATE knowledge_ingestions SET state='stored',source_id=:source WHERE id=:id"
                ),
                {"source": source.id, "id": admission_id},
            )

    async def finish(self, user_id: str, admission_id: str, *, succeeded: bool) -> None:
        async with self.transaction() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT state,source_id FROM knowledge_ingestions WHERE id=:id AND "
                        "user_id=:user"
                    ),
                    {"id": admission_id, "user": user_id},
                )
            ).one_or_none()
            if row is None:
                raise ValueError("Knowledge ingestion admission is unavailable")
            if row.state in {"complete", "failed", "released"}:
                return
            if succeeded and row.state != "stored":
                raise ValueError("Knowledge ingestion source is not stored")
            state = "complete" if succeeded else "released" if row.state == "reserved" else "failed"
            await session.execute(
                text("UPDATE knowledge_ingestions SET state=:state WHERE id=:id"),
                {"state": state, "id": admission_id},
            )
            if not succeeded and row.source_id:
                await session.execute(
                    text(
                        "UPDATE knowledge_sources SET status='failed',error_message='Document "
                        "indexing did not complete' WHERE id=:source AND user_id=:user AND "
                        "status='processing'"
                    ),
                    {"source": row.source_id, "user": user_id},
                )

    async def recover(self) -> None:
        async with self.transaction() as session:
            await session.execute(
                text(
                    "UPDATE knowledge_ingestions SET state=CASE WHEN state='reserved' THEN "
                    "'released' ELSE 'failed' END WHERE state IN "
                    "('reserved','started','stored')"
                )
            )
            await session.execute(
                text(
                    "UPDATE knowledge_sources SET status='failed',error_message='Document "
                    "indexing was interrupted; delete and upload again' WHERE "
                    "status='processing'"
                )
            )
