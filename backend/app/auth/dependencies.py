from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.email import OtpEmailSender
from app.auth.logging import log_auth_rejection
from app.auth.service import AuthService
from app.auth.tokens import (
    AccessTokenError,
    access_token_rejection_reason,
    decode_access_token,
)
from app.config import Settings
from app.db import Database
from app.models import User

bearer_scheme = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_email_sender(request: Request) -> OtpEmailSender:
    return request.app.state.email_sender


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    database: Database = request.app.state.database
    async for session in database.session():
        yield session


SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]
EmailSenderDependency = Annotated[OtpEmailSender, Depends(get_email_sender)]


def get_auth_service(
    request: Request,
    session: SessionDependency,
    settings: SettingsDependency,
    email_sender: EmailSenderDependency,
) -> AuthService:
    return AuthService(
        session=session,
        settings=settings,
        email_sender=email_sender,
        clock=request.app.state.clock,
        policy=request.app.state.runtime_policy,
        client_ip=request.client.host if request.client else "unknown",
    )


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: SessionDependency,
    settings: SettingsDependency,
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        log_auth_rejection(
            surface="http",
            path=request.url.path,
            reason="missing_token",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        raise unauthorized
    try:
        claims = decode_access_token(
            credentials.credentials,
            secret=settings.jwt_secret_value,
            algorithm=settings.jwt_algorithm,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
    except AccessTokenError as exc:
        log_auth_rejection(
            surface="http",
            path=request.url.path,
            reason=access_token_rejection_reason(exc),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        raise unauthorized from exc

    result = await session.execute(select(User).where(User.id == claims.user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_verified or user.email != claims.email:
        log_auth_rejection(
            surface="http",
            path=request.url.path,
            reason="invalid_token",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        raise unauthorized
    request.state.access_token_expires_at = claims.expires_at
    return user


CurrentUserDependency = Annotated[User, Depends(get_current_user)]


def is_administrator(user: User, settings: Settings) -> bool:
    allowed = {
        email.strip().casefold() for email in settings.admin_emails.split(",") if email.strip()
    }
    return user.email.casefold() in allowed


def get_administrator(user: CurrentUserDependency, settings: SettingsDependency) -> User:
    if not is_administrator(user, settings):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required"
        )
    return user


AdministratorDependency = Annotated[User, Depends(get_administrator)]
