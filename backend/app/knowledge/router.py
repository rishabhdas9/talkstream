import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

from app.auth.dependencies import CurrentUserDependency
from app.costs.models import BudgetExceeded
from app.costs.rates import RateUnavailable
from app.knowledge.admission import IngestionCapacityError
from app.knowledge.crypto import CredentialCipherError
from app.knowledge.ingestion import (
    ExtractionLimitError,
    ExtractionTimeoutError,
    UnsupportedSourceError,
)
from app.knowledge.mutations import KnowledgeDeletionPendingError
from app.knowledge.providers import PROVIDERS
from app.knowledge.schemas import (
    EmbeddingModelResponse,
    EmbeddingProfileBody,
    EmbeddingProfileResponse,
    EmbeddingProviderResponse,
    KnowledgeSearchBody,
    KnowledgeSearchResponse,
    KnowledgeSourceListResponse,
    KnowledgeSourceResponse,
    ProviderCatalogResponse,
    ProviderKeyBody,
    TestEmbeddingProfileResponse,
    TextSourceBody,
)
from app.knowledge.service import (
    EmbeddingProviderUnavailableError,
    KnowledgeNotConfiguredError,
    KnowledgeReindexInProgressError,
    KnowledgeService,
    KnowledgeSourceLimitError,
    KnowledgeSourceTooLargeError,
    UnknownEmbeddingProfileError,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
logger = logging.getLogger("uvicorn.error")


def get_knowledge_service(request: Request) -> KnowledgeService:
    return request.app.state.knowledge_service


KnowledgeServiceDependency = Annotated[KnowledgeService, Depends(get_knowledge_service)]


def _profile_response(
    profile,
    *,
    retrieval_available: bool | None = None,
    reindex_job=None,
) -> EmbeddingProfileResponse:
    return EmbeddingProfileResponse(
        configured=True,
        provider_id=profile.provider_id,
        model_id=profile.model_id,
        dimensions=profile.dimensions,
        status="reindexing" if profile.status.value == "pending" else profile.status.value,
        active=profile.is_active if retrieval_available is None else retrieval_available,
        generation_id=profile.generation_id,
        reindex_processed_chunks=(
            reindex_job.processed_chunks if reindex_job is not None else None
        ),
        reindex_total_chunks=reindex_job.total_chunks if reindex_job is not None else None,
    )


def _configuration_error(exc: Exception) -> HTTPException:
    if isinstance(exc, UnknownEmbeddingProfileError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _source_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KnowledgeDeletionPendingError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc), headers={"Retry-After": "5"}
        )
    if isinstance(exc, BudgetExceeded):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Embedding spending allowance exhausted",
        )
    if isinstance(exc, (RateUnavailable, CredentialCipherError)):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Embedding configuration is unavailable"
        )
    if isinstance(exc, IngestionCapacityError):
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    if isinstance(exc, ExtractionTimeoutError):
        return HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT, detail="Document extraction timed out"
        )
    if isinstance(exc, (KnowledgeSourceTooLargeError, ExtractionLimitError)):
        return HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc))
    if isinstance(
        exc,
        (KnowledgeNotConfiguredError, KnowledgeReindexInProgressError, KnowledgeSourceLimitError),
    ):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (ValueError, UnsupportedSourceError)):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    logger.exception("knowledge_operation_failed", exc_info=exc)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Knowledge indexing service is unavailable",
    )


def _mask_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return "…" + api_key[-2:]
    return f"{api_key[:4]}…{api_key[-4:]}"


async def _provider_catalog(user_id: str, service: KnowledgeService) -> ProviderCatalogResponse:
    try:
        user_keys = await service.provider_keys(user_id)
    except CredentialCipherError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stored provider credentials are unavailable",
        ) from None
    providers = []
    for provider in PROVIDERS:
        user_key = user_keys.get(provider.id)
        providers.append(
            EmbeddingProviderResponse(
                id=provider.id,
                label=provider.label,
                available=await service.provider_available(user_id, provider.id),
                key_set=user_key is not None,
                key_hint=_mask_key(user_key) if user_key is not None else None,
                models=[
                    EmbeddingModelResponse(
                        id=model.id,
                        label=model.label,
                        dimensions=model.dimensions,
                        default=model.default,
                    )
                    for model in provider.models
                ],
            )
        )
    return ProviderCatalogResponse(providers=providers)


@router.get("/providers", response_model=ProviderCatalogResponse)
async def list_embedding_providers(
    user: CurrentUserDependency,
    service: KnowledgeServiceDependency,
) -> ProviderCatalogResponse:
    return await _provider_catalog(user.id, service)


@router.put("/providers/{provider_id}/key", response_model=ProviderCatalogResponse)
async def set_provider_key(
    provider_id: str,
    body: ProviderKeyBody,
    user: CurrentUserDependency,
    service: KnowledgeServiceDependency,
) -> ProviderCatalogResponse:
    if all(provider.id != provider_id for provider in PROVIDERS):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown embedding provider"
        )
    try:
        await service.set_provider_key(user.id, provider_id, body.api_key.strip())
    except CredentialCipherError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stored provider credentials are unavailable",
        ) from None
    logger.info("knowledge provider key set user_id=%s provider=%s", user.id, provider_id)
    return await _provider_catalog(user.id, service)


@router.delete("/providers/{provider_id}/key", response_model=ProviderCatalogResponse)
async def delete_provider_key(
    provider_id: str,
    user: CurrentUserDependency,
    service: KnowledgeServiceDependency,
) -> ProviderCatalogResponse:
    await service.delete_provider_key(user.id, provider_id)
    return await _provider_catalog(user.id, service)


@router.get("/profile", response_model=EmbeddingProfileResponse)
async def get_embedding_profile(
    user: CurrentUserDependency,
    service: KnowledgeServiceDependency,
) -> EmbeddingProfileResponse:
    profile = await service.get_latest_profile(user.id)
    if profile is not None:
        active_profile = await service.get_active_profile(user.id)
        reindex_job = await service.get_reindex_job(profile.id)
        return _profile_response(
            profile,
            retrieval_available=active_profile is not None,
            reindex_job=reindex_job,
        )
    default_provider = PROVIDERS[0]
    default_model = next(model for model in default_provider.models if model.default)
    try:
        available = await service.provider_available(user.id, default_provider.id)
    except CredentialCipherError as exc:
        raise _source_error(exc) from None
    return EmbeddingProfileResponse(
        configured=False,
        provider_id=default_provider.id,
        model_id=default_model.id,
        dimensions=default_model.dimensions,
        status="ready" if available else "provider_unavailable",
        active=False,
    )


@router.post("/profile/test", response_model=TestEmbeddingProfileResponse)
async def test_embedding_profile(
    body: EmbeddingProfileBody,
    user: CurrentUserDependency,
    service: KnowledgeServiceDependency,
) -> TestEmbeddingProfileResponse:
    try:
        dimensions = await service.test_profile(user.id, body.provider_id, body.model_id)
    except (UnknownEmbeddingProfileError, EmbeddingProviderUnavailableError) as exc:
        raise _configuration_error(exc) from exc
    except (BudgetExceeded, RateUnavailable, CredentialCipherError) as exc:
        raise _source_error(exc) from None
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embedding provider test failed",
        ) from exc
    return TestEmbeddingProfileResponse(ok=True, dimensions=dimensions)


@router.put("/profile", response_model=EmbeddingProfileResponse)
async def update_embedding_profile(
    body: EmbeddingProfileBody,
    background_tasks: BackgroundTasks,
    user: CurrentUserDependency,
    service: KnowledgeServiceDependency,
) -> EmbeddingProfileResponse:
    try:
        profile = await service.save_profile(
            user_id=user.id,
            provider_id=body.provider_id,
            model_id=body.model_id,
        )
    except (
        UnknownEmbeddingProfileError,
        EmbeddingProviderUnavailableError,
        KnowledgeReindexInProgressError,
    ) as exc:
        raise _configuration_error(exc) from exc
    except CredentialCipherError as exc:
        raise _source_error(exc) from None
    if profile.status.value == "pending":
        background_tasks.add_task(service.run_pending_jobs)
    return _profile_response(profile, reindex_job=await service.get_reindex_job(profile.id))


@router.get("/sources", response_model=KnowledgeSourceListResponse)
async def list_sources(
    user: CurrentUserDependency,
    service: KnowledgeServiceDependency,
) -> KnowledgeSourceListResponse:
    return KnowledgeSourceListResponse(sources=await service.list_sources(user.id))


@router.post(
    "/sources/text",
    response_model=KnowledgeSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_text_source(
    body: TextSourceBody,
    user: CurrentUserDependency,
    service: KnowledgeServiceDependency,
) -> KnowledgeSourceResponse:
    try:
        source = await service.add_text_source(
            user_id=user.id,
            name=body.name,
            text=body.text,
        )
    except Exception as exc:
        raise _source_error(exc) from exc
    return KnowledgeSourceResponse.model_validate(source)


@router.post(
    "/sources/file",
    response_model=KnowledgeSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_file_source(
    user: CurrentUserDependency,
    service: KnowledgeServiceDependency,
    file: UploadFile = File(...),  # noqa: B008 - FastAPI dependency declaration
) -> KnowledgeSourceResponse:
    try:
        data = await file.read(service.max_upload_bytes + 1)
        source = await service.add_file_source(
            user_id=user.id, name=file.filename or "source", data=data
        )
    except Exception as exc:
        raise _source_error(exc) from exc
    finally:
        await file.close()
    return KnowledgeSourceResponse.model_validate(source)


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    body: KnowledgeSearchBody,
    user: CurrentUserDependency,
    service: KnowledgeServiceDependency,
) -> KnowledgeSearchResponse:
    try:
        results = await service.search_knowledge(
            user_id=user.id,
            query=body.query,
            limit=body.limit,
        )
    except Exception as exc:
        raise _source_error(exc) from exc
    return KnowledgeSearchResponse(results=results)


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: str,
    user: CurrentUserDependency,
    service: KnowledgeServiceDependency,
) -> Response:
    try:
        await service.delete_source(user_id=user.id, source_id=source_id)
    except Exception as exc:
        raise _source_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
