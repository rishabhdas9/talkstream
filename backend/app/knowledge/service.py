from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from contextlib import nullcontext, suppress
from functools import partial
from typing import Protocol

import anyio
from sqlalchemy import delete, func, select, update

from app.config import Settings
from app.costs.models import CostScope
from app.costs.service import CostService
from app.db import Database
from app.knowledge.admission import IngestionAdmission
from app.knowledge.crypto import CredentialCipher
from app.knowledge.embeddings import (
    EmbeddingClientFactory,
    GeminiEmbeddingClient,
    OpenAIEmbeddingClient,
)
from app.knowledge.hybrid import keyword_chunk_ids, merge_ranked, rrf_scores
from app.knowledge.ingestion import (
    ChunkDraft,
    ExtractedPage,
    chunk_pages,
    extract_file_bounded,
)
from app.knowledge.metering import MeteredEmbeddingClient
from app.knowledge.models import (
    EmbeddingProfile,
    EmbeddingProviderKey,
    KnowledgeChunk,
    KnowledgeDeletionEvent,
    KnowledgeJobStatus,
    KnowledgeProfileStatus,
    KnowledgeReindexJob,
    KnowledgeSource,
    KnowledgeSourceStatus,
)
from app.knowledge.mutations import (
    DELETION_PENDING_MESSAGE,
    KnowledgeDeletionPendingError,
    KnowledgeMutationConflictError,
    ensure_mutation_available,
    user_mutation,
)
from app.knowledge.providers import env_key_for, get_model_definition, provider_is_configured
from app.knowledge.schemas import KnowledgeSearchResult
from app.knowledge.vector_store import QdrantPoint, point_id
from app.models import new_id, utc_now
from app.recovery import RecoveryJournal
from app.runtime import RuntimePolicy, SystemClock
from app.voice.lifecycle import bounded_cleanup


class UnknownEmbeddingProfileError(ValueError):
    pass


class EmbeddingProviderUnavailableError(RuntimeError):
    pass


class KnowledgeNotConfiguredError(RuntimeError):
    pass


class KnowledgeSourceLimitError(ValueError):
    pass


class KnowledgeSourceTooLargeError(ValueError):
    pass


KnowledgeReindexInProgressError = KnowledgeMutationConflictError


class EmbeddingClient(Protocol):
    async def embed(
        self,
        texts: Sequence[str],
        *,
        model_id: str,
        expected_dimensions: int,
    ) -> list[list[float]]: ...


class VectorStore(Protocol):
    async def upsert(
        self, *, collection: str, dimensions: int, points: list[QdrantPoint]
    ) -> None: ...

    async def search(
        self,
        *,
        collection: str,
        vector: list[float],
        user_id: str,
        generation_id: str,
        limit: int,
    ) -> list[dict]: ...

    async def delete_source(
        self,
        *,
        collection: str,
        user_id: str,
        source_id: str,
        generation_id: str,
    ) -> None: ...


def collection_name(provider_id: str, model_id: str, dimensions: int) -> str:
    fingerprint = hashlib.sha256(
        f"{provider_id}:{model_id}:{dimensions}:cosine:chunker-v1".encode()
    ).hexdigest()[:24]
    return f"knowledge_{fingerprint}"


class StaleReindexError(RuntimeError):
    pass


def _active_profile(user_id: str, *columns):
    return (
        select(*columns)
        .where(EmbeddingProfile.user_id == user_id, EmbeddingProfile.is_active.is_(True))
        .order_by(EmbeddingProfile.activated_at.desc())
        .limit(1)
    )


def _indexed_chunks(user_id: str, *columns):
    return (
        select(*columns)
        .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
        .where(
            KnowledgeChunk.user_id == user_id,
            KnowledgeSource.user_id == user_id,
            KnowledgeSource.status == KnowledgeSourceStatus.INDEXED,
        )
        .order_by(KnowledgeChunk.source_id, KnowledgeChunk.ordinal)
    )


def _chunk_snapshot(chunk_ids: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(chunk_ids).encode()).hexdigest()


def _profile_snapshot(profile: EmbeddingProfile, replaces: str | None) -> str:
    return json.dumps(
        {
            "profile_id": profile.id,
            "provider_id": profile.provider_id,
            "model_id": profile.model_id,
            "dimensions": profile.dimensions,
            "generation_id": profile.generation_id,
            "collection_name": profile.collection_name,
            "replaces": replaces,
        },
        sort_keys=True,
    )


def _points(user_id: str, profile: EmbeddingProfile, chunks, vectors) -> list[QdrantPoint]:
    return [
        QdrantPoint(
            id=point_id(chunk.id, profile.generation_id),
            vector=vector,
            payload={
                "user_id": user_id,
                "source_id": chunk.source_id,
                "chunk_id": chunk.id,
                "generation_id": profile.generation_id,
            },
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]


def _batch_requests(client: EmbeddingClient, batches, profile: EmbeddingProfile):
    """Fund every batch of the job before its first provider request."""
    texts = [[chunk.content for chunk in batch] for batch in batches]
    if isinstance(client, MeteredEmbeddingClient):
        return client.reserve_batches(
            texts, model_id=profile.model_id, expected_dimensions=profile.dimensions
        )
    return nullcontext(
        [
            partial(
                client.embed,
                batch,
                model_id=profile.model_id,
                expected_dimensions=profile.dimensions,
            )
            for batch in texts
        ]
    )


class KnowledgeService:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        embedding_client: EmbeddingClient,
        vector_store: VectorStore | None = None,
        client_factory: EmbeddingClientFactory | None = None,
        costs: CostService | None = None,
        policy: RuntimePolicy | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.embedding_client = embedding_client
        self.vector_store = vector_store
        self.client_factory = client_factory
        self.costs = costs
        self.policy = policy or (
            costs.policy if costs is not None else RuntimePolicy(profile=settings.runtime_profile)
        )
        self.admission = IngestionAdmission(
            database=database,
            clock=costs.clock if costs is not None else SystemClock(),
            policy=self.policy,
            source_limit=min(settings.knowledge_max_sources_per_user, self.policy.sources_per_user),
        )
        self._job_lock = asyncio.Lock()
        self.journal = (
            RecoveryJournal(settings.recovery_journal_file)
            if settings.recovery_journal_file is not None
            else None
        )
        self._mutation_locks: dict[str, asyncio.Lock] = {}
        self._mutation_owners: dict[str, asyncio.Task] = {}

    async def get_provider_key(self, user_id: str, provider_id: str) -> str | None:
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(EmbeddingProviderKey.key_ciphertext).where(
                    EmbeddingProviderKey.user_id == user_id,
                    EmbeddingProviderKey.provider_id == provider_id,
                )
            )
            ciphertext = result.scalar_one_or_none()
            return self._credential_cipher().decrypt(ciphertext) if ciphertext is not None else None

    def _credential_cipher(self) -> CredentialCipher:
        key = self.settings.credential_encryption_key
        value = key.get_secret_value() if hasattr(key, "get_secret_value") else key
        return CredentialCipher(value)

    async def set_provider_key(self, user_id: str, provider_id: str, api_key: str) -> None:
        ciphertext = self._credential_cipher().encrypt(api_key)
        async with self.database.session_factory() as session:
            existing = (
                await session.execute(
                    select(EmbeddingProviderKey).where(
                        EmbeddingProviderKey.user_id == user_id,
                        EmbeddingProviderKey.provider_id == provider_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    EmbeddingProviderKey(
                        user_id=user_id, provider_id=provider_id, key_ciphertext=ciphertext
                    )
                )
            else:
                existing.key_ciphertext = ciphertext
            await session.commit()

    def _journal_deletion(self, kind: str, user_id: str, record_id: str) -> None:
        # Written before the deletion is acknowledged so a restored backup cannot revive it.
        if self.journal is not None:
            self.journal.record(kind, user_id, record_id, self.admission.clock.utc_now())

    async def delete_provider_key(self, user_id: str, provider_id: str) -> None:
        self._journal_deletion("provider_key", user_id, provider_id)
        async with self.database.session_factory() as session:
            await session.execute(
                delete(EmbeddingProviderKey).where(
                    EmbeddingProviderKey.user_id == user_id,
                    EmbeddingProviderKey.provider_id == provider_id,
                )
            )
            await session.commit()

    async def provider_keys(self, user_id: str) -> dict[str, str]:
        async with self.database.session_factory() as session:
            rows = await session.execute(
                select(EmbeddingProviderKey.provider_id, EmbeddingProviderKey.key_ciphertext).where(
                    EmbeddingProviderKey.user_id == user_id
                )
            )
            return {provider: self._credential_cipher().decrypt(value) for provider, value in rows}

    async def provider_available(self, user_id: str, provider_id: str) -> bool:
        if not provider_is_configured(provider_id, self.settings):
            return False
        if env_key_for(provider_id, self.settings) is not None:
            return True
        return await self.get_provider_key(user_id, provider_id) is not None

    async def _client_for(
        self,
        user_id: str,
        provider_id: str,
        *,
        cost_scope: CostScope | None = None,
        evaluation_cap_micro_inr: int | None = None,
    ) -> EmbeddingClient:
        if cost_scope is not None and cost_scope.user_id != user_id:
            raise ValueError("Embedding scope belongs to a different user")
        if self.client_factory is None:
            client = self.embedding_client
            # Pure in-memory clients are an injection seam for offline service tests.
            if not isinstance(client, (OpenAIEmbeddingClient, GeminiEmbeddingClient)):
                return client
        else:
            if not provider_is_configured(provider_id, self.settings):
                raise EmbeddingProviderUnavailableError("Embedding provider is unavailable")
            api_key = await self.get_provider_key(user_id, provider_id)
            if api_key is None:
                api_key = env_key_for(provider_id, self.settings)
            if api_key is None:
                raise EmbeddingProviderUnavailableError(
                    "Add an API key for this embedding provider first"
                )
            client = self.client_factory.client_for(provider_id, api_key)
        rate_provider_id = (
            f"openrouter:{self.settings.openrouter_embedding_provider}"
            if provider_id == "openrouter"
            else provider_id
        )
        return MeteredEmbeddingClient(
            client=client,
            costs=self.costs,
            scope=cost_scope or CostScope(user_id=user_id, job_id=new_id()),
            provider_id=provider_id,
            rate_provider_id=rate_provider_id,
            evaluation_cap_micro_inr=evaluation_cap_micro_inr,
        )

    async def resolve_model(self, user_id: str, provider_id: str, model_id: str):
        model = get_model_definition(provider_id, model_id)
        if model is None:
            raise UnknownEmbeddingProfileError("Unsupported embedding provider or model")
        if not await self.provider_available(user_id, provider_id):
            raise EmbeddingProviderUnavailableError("Embedding provider is unavailable")
        return model

    async def test_profile(self, user_id: str, provider_id: str, model_id: str) -> int:
        if self.settings.live_evaluation_cap_micro_inr <= 0:
            raise EmbeddingProviderUnavailableError(
                "Live embedding tests require explicit evaluation allowance"
            )
        model = await self.resolve_model(user_id, provider_id, model_id)
        client = await self._client_for(
            user_id,
            provider_id,
            cost_scope=CostScope(user_id=user_id, job_id=f"live-eval:{new_id()}"),
            evaluation_cap_micro_inr=self.settings.live_evaluation_cap_micro_inr,
        )
        vectors = await client.embed(
            ["Talkstream embedding connection test"],
            model_id=model.id,
            expected_dimensions=model.dimensions,
        )
        return len(vectors[0])

    async def get_active_profile(self, user_id: str) -> EmbeddingProfile | None:
        async with self.database.session_factory() as session:
            return await session.scalar(_active_profile(user_id, EmbeddingProfile))

    async def get_latest_profile(self, user_id: str) -> EmbeddingProfile | None:
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(EmbeddingProfile)
                .where(EmbeddingProfile.user_id == user_id)
                .order_by(EmbeddingProfile.created_at.desc(), EmbeddingProfile.id.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_reindex_job(self, profile_id: str) -> KnowledgeReindexJob | None:
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(KnowledgeReindexJob)
                .where(KnowledgeReindexJob.profile_id == profile_id)
                .order_by(
                    KnowledgeReindexJob.created_at.desc(),
                    KnowledgeReindexJob.id.desc(),
                )
                .limit(1)
            )
            return result.scalar_one_or_none()

    @user_mutation
    async def save_profile(
        self,
        *,
        user_id: str,
        provider_id: str,
        model_id: str,
    ) -> EmbeddingProfile:
        model = await self.resolve_model(user_id, provider_id, model_id)
        async with self.admission.transaction() as session:
            await ensure_mutation_available(session, user_id)
            unfinished_job = (
                await session.execute(
                    select(KnowledgeReindexJob)
                    .where(
                        KnowledgeReindexJob.user_id == user_id,
                        KnowledgeReindexJob.status.in_(
                            [KnowledgeJobStatus.PENDING, KnowledgeJobStatus.RUNNING]
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if unfinished_job is not None:
                raise KnowledgeReindexInProgressError("A model reindex is already in progress")
            current = (
                await session.execute(
                    select(EmbeddingProfile)
                    .where(
                        EmbeddingProfile.user_id == user_id,
                        EmbeddingProfile.is_active.is_(True),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if (
                current is not None
                and current.provider_id == provider_id
                and current.model_id == model_id
            ):
                return current

            chunk_ids = list(await session.scalars(_indexed_chunks(user_id, KnowledgeChunk.id)))

            if current is not None and chunk_ids:
                profile = EmbeddingProfile(
                    user_id=user_id,
                    provider_id=provider_id,
                    model_id=model_id,
                    dimensions=model.dimensions,
                    generation_id=new_id(),
                    collection_name=collection_name(provider_id, model_id, model.dimensions),
                    status=KnowledgeProfileStatus.PENDING,
                    is_active=False,
                )
                session.add(profile)
                await session.flush()
                session.add(
                    KnowledgeReindexJob(
                        user_id=user_id,
                        profile_id=profile.id,
                        status=KnowledgeJobStatus.PENDING,
                        total_chunks=len(chunk_ids),
                        generation_id=profile.generation_id,
                        profile_snapshot=_profile_snapshot(profile, current.id),
                        source_snapshot=_chunk_snapshot(chunk_ids),
                    )
                )
                await session.commit()
                await session.refresh(profile)
                return profile

            await session.execute(
                update(EmbeddingProfile)
                .where(
                    EmbeddingProfile.user_id == user_id,
                    EmbeddingProfile.is_active.is_(True),
                )
                .values(is_active=False)
            )
            profile = EmbeddingProfile(
                user_id=user_id,
                provider_id=provider_id,
                model_id=model_id,
                dimensions=model.dimensions,
                generation_id=new_id(),
                collection_name=collection_name(provider_id, model_id, model.dimensions),
                status=KnowledgeProfileStatus.READY,
                is_active=True,
                activated_at=utc_now(),
            )
            session.add(profile)
            await session.commit()
            await session.refresh(profile)
            return profile

    async def run_pending_jobs(self) -> None:
        async with self._job_lock:
            await self._run_pending_jobs_locked()

    async def _run_pending_jobs_locked(self) -> None:
        async with self.database.session_factory() as session:
            deletions = (
                await session.execute(
                    select(KnowledgeSource.user_id, KnowledgeSource.id).where(
                        KnowledgeSource.status == KnowledgeSourceStatus.DELETING
                    )
                )
            ).all()
        for user_id, source_id in deletions:
            with suppress(KnowledgeDeletionPendingError, KnowledgeMutationConflictError):
                await self.delete_source(user_id=user_id, source_id=source_id)
        async with self.database.session_factory() as session:
            job_ids = list(
                (
                    await session.execute(
                        select(KnowledgeReindexJob.id)
                        .where(
                            KnowledgeReindexJob.status.in_(
                                [KnowledgeJobStatus.PENDING, KnowledgeJobStatus.RUNNING]
                            )
                        )
                        .order_by(KnowledgeReindexJob.created_at)
                    )
                ).scalars()
            )
        for job_id in job_ids:
            await self._run_reindex_job(job_id)

    async def _validated_reindex(self, session, job_id: str):
        """Return the runnable job, target profile and chunks, or None when already settled."""
        job = await session.get(KnowledgeReindexJob, job_id)
        if job is None or job.status not in {
            KnowledgeJobStatus.PENDING,
            KnowledgeJobStatus.RUNNING,
        }:
            return None
        profile = await session.get(EmbeddingProfile, job.profile_id)
        active = await session.scalar(_active_profile(job.user_id, EmbeddingProfile.id))
        chunks = list(await session.scalars(_indexed_chunks(job.user_id, KnowledgeChunk)))
        if (
            profile is None
            or profile.user_id != job.user_id
            or profile.is_active
            or job.generation_id != profile.generation_id
            or job.profile_snapshot != _profile_snapshot(profile, active)
            or job.source_snapshot != _chunk_snapshot([chunk.id for chunk in chunks])
            or not 0 <= job.processed_chunks <= job.total_chunks == len(chunks)
        ):
            raise StaleReindexError("Knowledge changed during reindex; retry the model change")
        return job, profile, chunks

    async def _run_reindex_job(self, job_id: str) -> None:
        if self.vector_store is None:
            return
        try:
            async with self.admission.transaction() as session:
                validated = await self._validated_reindex(session, job_id)
                if validated is None:
                    return
                job, profile, chunks = validated
                job.status = KnowledgeJobStatus.RUNNING
                user_id, processed = job.user_id, job.processed_chunks

            reindex_client = await self._client_for(
                user_id,
                profile.provider_id,
                cost_scope=CostScope(user_id=user_id, job_id=job_id),
            )
            size = self.settings.knowledge_embedding_batch_size
            batches = [
                chunks[start : start + size] for start in range(processed, len(chunks), size)
            ]
            async with _batch_requests(reindex_client, batches, profile) as requests:
                for batch, request in zip(batches, requests, strict=True):
                    vectors = await request()
                    await self.vector_store.upsert(
                        collection=profile.collection_name,
                        dimensions=profile.dimensions,
                        points=_points(user_id, profile, batch, vectors),
                    )
                    processed += len(batch)
                    async with self.database.session_factory() as session:
                        await session.execute(
                            update(KnowledgeReindexJob)
                            .where(
                                KnowledgeReindexJob.id == job_id,
                                KnowledgeReindexJob.status == KnowledgeJobStatus.RUNNING,
                            )
                            .values(processed_chunks=processed)
                        )
                        await session.commit()

            async with self.admission.transaction() as session:
                validated = await self._validated_reindex(session, job_id)
                if validated is None:
                    return
                if validated[0].processed_chunks != len(chunks):
                    raise StaleReindexError("Reindex is incomplete; retry the model change")
                old_profiles = list(
                    (
                        await session.execute(
                            select(EmbeddingProfile).where(
                                EmbeddingProfile.user_id == user_id,
                                EmbeddingProfile.is_active.is_(True),
                            )
                        )
                    ).scalars()
                )
                source_ids = list(
                    (
                        await session.execute(
                            select(KnowledgeSource.id).where(KnowledgeSource.user_id == user_id)
                        )
                    ).scalars()
                )
                await session.execute(
                    update(EmbeddingProfile)
                    .where(EmbeddingProfile.user_id == user_id)
                    .values(is_active=False)
                )
                await session.execute(
                    update(EmbeddingProfile)
                    .where(EmbeddingProfile.id == profile.id)
                    .values(
                        is_active=True,
                        status=KnowledgeProfileStatus.READY,
                        activated_at=utc_now(),
                        error_message=None,
                    )
                )
                await session.execute(
                    update(KnowledgeReindexJob)
                    .where(KnowledgeReindexJob.id == job_id)
                    .values(
                        status=KnowledgeJobStatus.COMPLETE,
                        processed_chunks=len(chunks),
                        error_message=None,
                    )
                )

            for old_profile in old_profiles:
                for source_id in source_ids:
                    with suppress(Exception):
                        await self.vector_store.delete_source(
                            collection=old_profile.collection_name,
                            user_id=user_id,
                            source_id=source_id,
                            generation_id=old_profile.generation_id,
                        )
        except Exception as exc:
            async with self.database.session_factory() as session:
                job = await session.get(KnowledgeReindexJob, job_id)
                if job is None:
                    return
                job.status = KnowledgeJobStatus.FAILED
                job.error_message = str(exc)
                profile = await session.get(EmbeddingProfile, job.profile_id)
                if profile is not None:
                    profile.status = KnowledgeProfileStatus.FAILED
                    profile.error_message = str(exc)
                await session.commit()

    @property
    def max_upload_bytes(self) -> int:
        return min(self.policy.upload_bytes, self.settings.knowledge_max_upload_bytes)

    @user_mutation
    async def add_file_source(self, *, user_id: str, name: str, data: bytes) -> KnowledgeSource:
        if len(data) > self.max_upload_bytes:
            raise KnowledgeSourceTooLargeError("Uploaded file exceeds the configured limit")
        if not name.strip() or len(" ".join(name.split())) > 255:
            raise ValueError("Source name must contain between 1 and 255 characters")
        if self.vector_store is None:
            raise RuntimeError("Vector database is unavailable")
        if await self.get_active_profile(user_id) is None:
            raise KnowledgeNotConfiguredError("Choose an embedding model before adding sources")
        await self._ensure_no_reindex(user_id)
        admission_id = await self.admission.reserve(user_id)
        succeeded = False
        try:
            await self.admission.mark_started(user_id, admission_id)
            extracted = await extract_file_bounded(
                name,
                data,
                max_bytes=self.max_upload_bytes,
                max_pages=self.policy.pdf_pages,
                max_characters=min(
                    self.policy.source_characters, self.settings.knowledge_max_source_characters
                ),
            )
            source = await self.add_text_source(
                user_id=user_id,
                name=name,
                text=extracted.text,
                pages=extracted.pages,
                media_type=extracted.media_type,
                source_sha256=extracted.source_sha256,
                extraction_version=extracted.extraction_version,
                admission_id=admission_id,
            )
            succeeded = True
            return source
        finally:
            with anyio.CancelScope(shield=True):
                await bounded_cleanup(
                    self.admission.finish(user_id, admission_id, succeeded=succeeded)
                )

    @user_mutation
    async def add_text_source(
        self,
        *,
        user_id: str,
        name: str,
        text: str,
        media_type: str = "text/plain",
        pages: list[ExtractedPage] | None = None,
        source_sha256: str | None = None,
        extraction_version: str = "text:utf8:v1",
        admission_id: str | None = None,
    ) -> KnowledgeSource:
        normalized_name = " ".join(name.split()).strip()
        normalized_text = text.strip()
        if not normalized_name or not normalized_text or len(normalized_name) > 255:
            raise ValueError(
                "Source name and text are required; name must not exceed 255 characters"
            )
        max_characters = min(
            self.settings.knowledge_max_source_characters, self.policy.source_characters
        )
        if len(normalized_text) > max_characters:
            raise KnowledgeSourceTooLargeError("Source text exceeds the configured limit")
        if self.vector_store is None:
            raise RuntimeError("Vector database is unavailable")
        profile = await self.get_active_profile(user_id)
        if profile is None:
            raise KnowledgeNotConfiguredError("Choose an embedding model before adding sources")
        await self._ensure_no_reindex(user_id)
        extracted_pages = pages or [ExtractedPage(normalized_text)]
        if (
            len(extracted_pages) > self.policy.pdf_pages
            or sum(len(page.text) for page in extracted_pages) + 2 * (len(extracted_pages) - 1)
            > max_characters
        ):
            raise KnowledgeSourceTooLargeError("Source text or pages exceed the configured limit")
        if "\n\n".join(page.text for page in extracted_pages).strip() != normalized_text:
            raise ValueError("Source text differs from extracted pages")
        drafts = chunk_pages(
            extracted_pages,
            chunk_characters=self.settings.knowledge_chunk_characters,
            overlap=self.settings.knowledge_chunk_overlap,
            max_chunks=self.policy.stored_chunks,
        )
        if not drafts:
            raise ValueError("Source does not contain extractable text")
        owned_admission = admission_id is None
        admission_id = admission_id or await self.admission.reserve(user_id)
        succeeded = False
        try:
            if owned_admission:
                await self.admission.mark_started(user_id, admission_id)
            source, chunks = await self._store_source_and_chunks(
                user_id=user_id,
                name=normalized_name,
                media_type=media_type,
                text=normalized_text,
                drafts=drafts,
                admission_id=admission_id,
                source_sha256=source_sha256 or hashlib.sha256(text.encode("utf-8")).hexdigest(),
                extraction_version=extraction_version,
            )
            source_client = await self._client_for(
                user_id,
                profile.provider_id,
                cost_scope=CostScope(user_id=user_id, job_id=source.id),
            )
            size = self.settings.knowledge_embedding_batch_size
            batches = [chunks[start : start + size] for start in range(0, len(chunks), size)]
            async with _batch_requests(source_client, batches, profile) as requests:
                for batch, request in zip(batches, requests, strict=True):
                    vectors = await request()
                    await self.vector_store.upsert(
                        collection=profile.collection_name,
                        dimensions=profile.dimensions,
                        points=_points(user_id, profile, batch, vectors),
                    )
            await self._mark_indexed(source.id)
            source.status = KnowledgeSourceStatus.INDEXED
            succeeded = True
            return source
        finally:
            # File ingestion owns admission across both extraction and indexing.
            if owned_admission:
                with anyio.CancelScope(shield=True):
                    await bounded_cleanup(
                        self.admission.finish(user_id, admission_id, succeeded=succeeded)
                    )

    async def _ensure_no_reindex(self, user_id: str) -> None:
        async with self.database.session_factory() as session:
            unfinished = await session.scalar(
                select(func.count(KnowledgeReindexJob.id)).where(
                    KnowledgeReindexJob.user_id == user_id,
                    KnowledgeReindexJob.status.in_(
                        [KnowledgeJobStatus.PENDING, KnowledgeJobStatus.RUNNING]
                    ),
                )
            )
        if int(unfinished or 0) > 0:
            raise KnowledgeReindexInProgressError(
                "New sources are paused while the embedding model is reindexing"
            )

    async def _store_source_and_chunks(
        self,
        *,
        user_id: str,
        name: str,
        media_type: str,
        text: str,
        drafts: list[ChunkDraft],
        admission_id: str,
        source_sha256: str,
        extraction_version: str,
    ) -> tuple[KnowledgeSource, list[KnowledgeChunk]]:
        source = KnowledgeSource(
            id=new_id(),
            user_id=user_id,
            name=name,
            media_type=media_type,
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            source_sha256=source_sha256,
            extraction_version=extraction_version,
            chunking_version=f"chunker-v1:{self.settings.knowledge_chunk_characters}:{self.settings.knowledge_chunk_overlap}",
            extracted_text=text,
            character_count=len(text),
            chunk_count=len(drafts),
            status=KnowledgeSourceStatus.PROCESSING,
        )
        chunks = [
            KnowledgeChunk(
                id=new_id(),
                source_id=source.id,
                user_id=user_id,
                ordinal=ordinal,
                content=draft.content,
                page_number=draft.page_number,
            )
            for ordinal, draft in enumerate(drafts)
        ]
        await self.admission.store(user_id, admission_id, source, chunks)
        return source, chunks

    async def _mark_indexed(self, source_id: str) -> None:
        async with self.admission.transaction() as session:
            result = await session.execute(
                update(KnowledgeSource)
                .where(
                    KnowledgeSource.id == source_id,
                    KnowledgeSource.status == KnowledgeSourceStatus.PROCESSING,
                )
                .values(status=KnowledgeSourceStatus.INDEXED, error_message=None)
            )
            if result.rowcount != 1:
                raise RuntimeError("Knowledge source changed during indexing")

    async def list_sources(self, user_id: str) -> list[KnowledgeSource]:
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(KnowledgeSource)
                .where(KnowledgeSource.user_id == user_id)
                .order_by(KnowledgeSource.created_at.desc())
            )
            return list(result.scalars())

    async def search_knowledge(
        self,
        *,
        user_id: str,
        query: str,
        limit: int,
        cost_scope: CostScope | None = None,
    ) -> list[KnowledgeSearchResult]:
        normalized_query = " ".join(query.split()).strip()
        if not normalized_query:
            raise ValueError("Search query is required")
        if len(normalized_query) > self.settings.knowledge_max_query_characters:
            raise KnowledgeSourceTooLargeError("Search query exceeds the configured limit")
        if cost_scope is not None and cost_scope.user_id != user_id:
            raise ValueError("Embedding scope belongs to a different user")
        async with self.database.session_factory() as session:
            count = await session.scalar(
                select(func.count(KnowledgeChunk.id))
                .join(KnowledgeSource)
                .where(
                    KnowledgeChunk.user_id == user_id,
                    KnowledgeSource.user_id == user_id,
                    KnowledgeSource.status == KnowledgeSourceStatus.INDEXED,
                )
            )
        if not count:
            return []
        if self.vector_store is None:
            raise RuntimeError("Vector database is unavailable")
        profile = await self.get_active_profile(user_id)
        if profile is None:
            raise KnowledgeNotConfiguredError("Choose an embedding model before searching")
        search_client = await self._client_for(user_id, profile.provider_id, cost_scope=cost_scope)
        vector = (
            await search_client.embed(
                [normalized_query],
                model_id=profile.model_id,
                expected_dimensions=profile.dimensions,
            )
        )[0]
        hits = await self.vector_store.search(
            collection=profile.collection_name,
            vector=vector,
            user_id=user_id,
            generation_id=profile.generation_id,
            limit=limit,
        )
        # Vector hits are untrusted until their payload names this owner and generation.
        matched = []
        for hit in hits:
            payload = hit.get("payload") if isinstance(hit, dict) else None
            if (
                isinstance(payload, dict)
                and payload.get("user_id") == user_id
                and payload.get("generation_id") == profile.generation_id
                and payload.get("chunk_id")
            ):
                matched.append((str(payload["chunk_id"]), hit))
        hybrid = self.settings.knowledge_retrieval_mode == "hybrid"
        if not matched and not hybrid:
            return []
        chunk_ids = [chunk_id for chunk_id, _ in matched]
        scores = {chunk_id: float(hit.get("score", 0.0)) for chunk_id, hit in matched}
        async with self.database.session_factory() as session:
            active = await session.scalar(_active_profile(user_id, EmbeddingProfile.generation_id))
            if active != profile.generation_id:
                return []  # The model switched during the provider/vector awaits.
            if hybrid:
                keyword_ids = await keyword_chunk_ids(
                    session, user_id=user_id, query=normalized_query, limit=limit
                )
                scores = rrf_scores(chunk_ids, keyword_ids)  # fused rank score, not similarity
                chunk_ids = merge_ranked(chunk_ids, keyword_ids, limit)
            rows = await session.execute(
                select(KnowledgeChunk, KnowledgeSource)
                .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
                .where(
                    KnowledgeChunk.id.in_(chunk_ids),
                    KnowledgeChunk.user_id == user_id,
                    KnowledgeSource.user_id == user_id,
                    KnowledgeSource.status == KnowledgeSourceStatus.INDEXED,
                )
            )
            owned = {chunk.id: (chunk, source) for chunk, source in rows}
        results: list[KnowledgeSearchResult] = []
        for chunk_id in chunk_ids:
            record = owned.get(chunk_id)
            if record is None:
                continue
            chunk, source = record
            results.append(
                KnowledgeSearchResult(
                    chunk_id=chunk.id,
                    source_id=source.id,
                    source_name=source.name,
                    excerpt=chunk.content,
                    page_number=chunk.page_number,
                    score=scores[chunk_id],
                )
            )
        return results

    @user_mutation
    async def delete_source(self, *, user_id: str, source_id: str) -> None:
        async with self.admission.transaction() as session:
            source = await session.scalar(
                select(KnowledgeSource).where(
                    KnowledgeSource.id == source_id, KnowledgeSource.user_id == user_id
                )
            )
            if source is None:
                return
            await ensure_mutation_available(session, user_id, deleting_source=source_id)
            if source.status != KnowledgeSourceStatus.DELETING:
                self._journal_deletion("knowledge_source", user_id, source_id)
            source.status = KnowledgeSourceStatus.DELETING
            source.error_message = DELETION_PENDING_MESSAGE
            event = await session.scalar(
                select(KnowledgeDeletionEvent).where(KnowledgeDeletionEvent.source_id == source_id)
            )
            if event is None:
                session.add(KnowledgeDeletionEvent(user_id=user_id, source_id=source_id))
            profiles = list(
                (
                    await session.scalars(
                        select(EmbeddingProfile).where(EmbeddingProfile.user_id == user_id)
                    )
                ).all()
            )
        try:
            if self.vector_store is None:
                raise RuntimeError("Vector database is unavailable")
            for profile in profiles:
                await self.vector_store.delete_source(
                    collection=profile.collection_name,
                    user_id=user_id,
                    source_id=source_id,
                    generation_id=profile.generation_id,
                )
            async with self.admission.transaction() as session:
                await session.execute(
                    delete(KnowledgeSource).where(
                        KnowledgeSource.id == source_id,
                        KnowledgeSource.user_id == user_id,
                        KnowledgeSource.status == KnowledgeSourceStatus.DELETING,
                    )
                )
                await session.execute(
                    update(KnowledgeDeletionEvent)
                    .where(
                        KnowledgeDeletionEvent.source_id == source_id,
                        KnowledgeDeletionEvent.user_id == user_id,
                    )
                    .values(completed_at=utc_now())
                )
        except Exception as exc:
            raise KnowledgeDeletionPendingError(DELETION_PENDING_MESSAGE) from exc
