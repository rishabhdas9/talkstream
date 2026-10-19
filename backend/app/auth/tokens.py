from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt


class AccessTokenError(ValueError):
    pass


class ExpiredAccessTokenError(AccessTokenError):
    pass


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: str
    email: str
    expires_at: datetime


def encode_access_token(
    *,
    user_id: str,
    email: str,
    secret: str,
    algorithm: str,
    ttl_seconds: int,
    issuer: str,
    audience: str,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(UTC)
    payload = {
        "sub": user_id,
        "email": email,
        "type": "access",
        "iat": issued_at,
        "exp": issued_at + timedelta(seconds=ttl_seconds),
        "iss": issuer,
        "aud": audience,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_access_token(
    token: str,
    *,
    secret: str,
    algorithm: str,
    issuer: str,
    audience: str,
) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            issuer=issuer,
            audience=audience,
            options={"require": ["sub", "email", "type", "iat", "exp", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ExpiredAccessTokenError("expired access token") from exc
    except jwt.PyJWTError as exc:
        raise AccessTokenError("invalid access token") from exc

    if payload.get("type") != "access":
        raise AccessTokenError("invalid token type")
    user_id = payload.get("sub")
    email = payload.get("email")
    if not isinstance(user_id, str) or not isinstance(email, str):
        raise AccessTokenError("invalid access token claims")
    return AccessTokenClaims(
        user_id=user_id, email=email, expires_at=datetime.fromtimestamp(float(payload["exp"]), UTC)
    )


def access_token_rejection_reason(error: AccessTokenError) -> str:
    return "expired_token" if isinstance(error, ExpiredAccessTokenError) else "invalid_token"
