from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class OtpStatus(StrEnum):
    ACCEPTED = "accepted"
    INVALID = "invalid"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


@dataclass(frozen=True)
class OtpSnapshot:
    id: str
    email: str
    code_hash: str
    expires_at: datetime
    attempts: int
    consumed: bool


@dataclass(frozen=True)
class OtpDecision:
    status: OtpStatus
    next_attempts: int
    consume: bool


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(*, challenge_id: str, email: str, code: str, pepper: str) -> str:
    message = f"{challenge_id}:{email}:{code}".encode()
    return hmac.new(pepper.encode(), message, hashlib.sha256).hexdigest()


def assess_otp(
    snapshot: OtpSnapshot,
    *,
    presented_code: str,
    pepper: str,
    max_attempts: int,
    now: datetime | None = None,
) -> OtpDecision:
    current_time = now or datetime.now(UTC)
    expires_at = snapshot.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    if snapshot.consumed:
        return OtpDecision(OtpStatus.CONSUMED, snapshot.attempts, True)
    if snapshot.attempts >= max_attempts:
        return OtpDecision(OtpStatus.ATTEMPTS_EXHAUSTED, snapshot.attempts, True)
    if current_time >= expires_at:
        return OtpDecision(OtpStatus.EXPIRED, snapshot.attempts, True)

    expected_hash = hash_otp(
        challenge_id=snapshot.id,
        email=snapshot.email,
        code=presented_code,
        pepper=pepper,
    )
    if hmac.compare_digest(snapshot.code_hash, expected_hash):
        return OtpDecision(OtpStatus.ACCEPTED, snapshot.attempts, True)

    next_attempts = snapshot.attempts + 1
    exhausted = next_attempts >= max_attempts
    status = OtpStatus.ATTEMPTS_EXHAUSTED if exhausted else OtpStatus.INVALID
    return OtpDecision(status, next_attempts, exhausted)
