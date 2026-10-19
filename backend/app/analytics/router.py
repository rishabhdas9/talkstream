from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.analytics.schemas import AnalyticsResponse, AnalyticsWindow, SessionDetailResponse
from app.analytics.service import AnalyticsService, SessionNotFoundError
from app.auth.dependencies import CurrentUserDependency, SessionDependency

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("", response_model=AnalyticsResponse)
async def get_analytics(
    user: CurrentUserDependency,
    session: SessionDependency,
    window: Annotated[AnalyticsWindow, Query()] = "day",
) -> AnalyticsResponse:
    return await AnalyticsService(session).overview(user.id, window)


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session_detail(
    session_id: str,
    user: CurrentUserDependency,
    session: SessionDependency,
) -> SessionDetailResponse:
    try:
        return await AnalyticsService(session).session_detail(user.id, session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        ) from exc
