from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.auth.dependencies import (
    CurrentUserDependency,
    SettingsDependency,
    get_auth_service,
    is_administrator,
)
from app.auth.email import OtpDeliveryError
from app.auth.limits import OtpRateLimited
from app.auth.schemas import (
    CurrentUserResponse,
    RequestOtpBody,
    RequestOtpResponse,
    TokenResponse,
    VerifyOtpBody,
    VoiceTicketResponse,
)
from app.auth.service import AuthService, InvalidOtpError
from app.auth.tickets import TicketService

router = APIRouter(prefix="/auth", tags=["auth"])
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


@router.post(
    "/request-otp",
    response_model=RequestOtpResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_otp(
    body: RequestOtpBody,
    service: AuthServiceDependency,
) -> RequestOtpResponse:
    try:
        await service.request_otp(str(body.email))
    except OtpRateLimited as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    except OtpDeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OTP delivery failed",
        ) from exc
    return RequestOtpResponse(
        message="If the email can receive messages, an OTP has been sent.",
        expires_in_seconds=service.otp_ttl_seconds,
    )


@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp(
    body: VerifyOtpBody,
    service: AuthServiceDependency,
) -> TokenResponse:
    try:
        token = await service.verify_otp(str(body.email), body.code)
    except InvalidOtpError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return TokenResponse(
        access_token=token,
        expires_in_seconds=service.access_token_ttl_seconds,
    )


@router.post("/dev-login", response_model=TokenResponse)
async def dev_login(request: Request, service: AuthServiceDependency) -> TokenResponse:
    # The socket peer, never a forwarded header: only this machine may sign in this way.
    peer = request.client.host if request.client else ""
    if not request.app.state.settings.dev_login_enabled or peer not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return TokenResponse(
        access_token=await service.dev_login(),
        expires_in_seconds=service.access_token_ttl_seconds,
    )


@router.get("/me", response_model=CurrentUserResponse)
async def me(user: CurrentUserDependency, settings: SettingsDependency) -> CurrentUserResponse:
    response = CurrentUserResponse.model_validate(user)
    response.is_admin = is_administrator(user, settings)
    return response


@router.post("/voice-ticket", response_model=VoiceTicketResponse)
async def voice_ticket(
    request: Request, response: Response, user: CurrentUserDependency
) -> VoiceTicketResponse:
    state = request.app.state
    service = TicketService(state.database, state.clock, state.runtime_policy)
    deadline = request.state.access_token_expires_at
    try:
        ticket = await service.issue(user, access_expires_at=deadline)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired access token") from exc
    response.headers["Cache-Control"] = "no-store"
    return VoiceTicketResponse(
        ticket=ticket,
        expires_in_seconds=min(
            state.runtime_policy.ticket_ttl_seconds,
            max(0, int((deadline - state.clock.utc_now()).total_seconds())),
        ),
    )
