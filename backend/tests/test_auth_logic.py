from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.auth.otp import OtpSnapshot, OtpStatus, assess_otp, generate_otp, hash_otp
from app.auth.tokens import (
    AccessTokenError,
    ExpiredAccessTokenError,
    decode_access_token,
    encode_access_token,
)

SECRET = "test-secret-that-is-at-least-thirty-two-characters"


def snapshot(*, code: str = "123456", **changes: object) -> OtpSnapshot:
    values = {
        "id": "challenge-1",
        "email": "person@example.com",
        "expires_at": datetime(2030, 1, 1, tzinfo=UTC),
        "attempts": 0,
        "consumed": False,
    }
    values.update(changes)
    values["code_hash"] = hash_otp(
        challenge_id=str(values["id"]),
        email=str(values["email"]),
        code=code,
        pepper=SECRET,
    )
    return OtpSnapshot(**values)  # type: ignore[arg-type]


def test_generated_otp_is_six_digits() -> None:
    code = generate_otp()
    assert len(code) == 6 and code.isdigit()


def test_otp_acceptance_is_single_use_decision() -> None:
    decision = assess_otp(
        snapshot(),
        presented_code="123456",
        pepper=SECRET,
        max_attempts=5,
        now=datetime(2029, 1, 1, tzinfo=UTC),
    )
    assert decision.status is OtpStatus.ACCEPTED
    assert decision.consume


def test_wrong_code_increments_and_final_attempt_consumes() -> None:
    ordinary = assess_otp(
        snapshot(attempts=1),
        presented_code="000000",
        pepper=SECRET,
        max_attempts=3,
        now=datetime(2029, 1, 1, tzinfo=UTC),
    )
    final = assess_otp(
        snapshot(attempts=2),
        presented_code="000000",
        pepper=SECRET,
        max_attempts=3,
        now=datetime(2029, 1, 1, tzinfo=UTC),
    )
    assert ordinary.status is OtpStatus.INVALID and ordinary.next_attempts == 2
    assert final.status is OtpStatus.ATTEMPTS_EXHAUSTED and final.consume


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"consumed": True}, OtpStatus.CONSUMED),
        ({"attempts": 5}, OtpStatus.ATTEMPTS_EXHAUSTED),
        ({"expires_at": datetime(2028, 1, 1, tzinfo=UTC)}, OtpStatus.EXPIRED),
    ],
)
def test_otp_guard_states(changes: dict[str, object], status: OtpStatus) -> None:
    decision = assess_otp(
        snapshot(**changes),
        presented_code="123456",
        pepper=SECRET,
        max_attempts=5,
        now=datetime(2029, 1, 1, tzinfo=UTC),
    )
    assert decision.status is status


def test_jwt_round_trip_and_expiry() -> None:
    now = datetime.now(UTC)
    token = encode_access_token(
        user_id="user-1",
        email="person@example.com",
        secret=SECRET,
        algorithm="HS256",
        ttl_seconds=60,
        issuer="talkstream",
        audience="talkstream-api",
        now=now,
    )

    assert (
        decode_access_token(
            token,
            secret=SECRET,
            algorithm="HS256",
            issuer="talkstream",
            audience="talkstream-api",
        ).user_id
        == "user-1"
    )

    expired = encode_access_token(
        user_id="user-1",
        email="person@example.com",
        secret=SECRET,
        algorithm="HS256",
        ttl_seconds=1,
        issuer="talkstream",
        audience="talkstream-api",
        now=now - timedelta(minutes=1),
    )
    with pytest.raises(ExpiredAccessTokenError):
        decode_access_token(
            expired,
            secret=SECRET,
            algorithm="HS256",
            issuer="talkstream",
            audience="talkstream-api",
        )


def test_jwt_rejects_wrong_secret() -> None:
    token = encode_access_token(
        user_id="user-1",
        email="person@example.com",
        secret=SECRET,
        algorithm="HS256",
        ttl_seconds=60,
        issuer="talkstream",
        audience="talkstream-api",
    )
    with pytest.raises(AccessTokenError):
        decode_access_token(
            token,
            secret="different-secret-that-is-also-long-enough",
            algorithm="HS256",
            issuer="talkstream",
            audience="talkstream-api",
        )
