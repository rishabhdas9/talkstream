from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from conftest import RecordingOtpSender
from sqlalchemy import select, update

from app.auth.service import AuthService, InvalidOtpError
from app.models import OtpChallenge


async def request_code(
    client: httpx.AsyncClient,
    sender: RecordingOtpSender,
    email: str = "person@example.com",
) -> str:
    response = await client.post("/auth/request-otp", json={"email": email})
    assert response.status_code == 202
    return sender.deliveries[-1]["code"]


async def test_auth_api_happy_path_and_me(api_client) -> None:
    client, sender = api_client
    code = await request_code(client, sender)

    verified = await client.post(
        "/auth/verify-otp",
        json={"email": "PERSON@example.com", "code": code},
    )
    assert verified.status_code == 200
    token = verified.json()["access_token"]

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "person@example.com"


async def test_wrong_code_and_attempt_limit(api_client) -> None:
    client, sender = api_client
    await request_code(client, sender)

    for _ in range(5):
        response = await client.post(
            "/auth/verify-otp",
            json={"email": "person@example.com", "code": "000000"},
        )
        assert response.status_code == 400

    correct_after_limit = await client.post(
        "/auth/verify-otp",
        json={"email": "person@example.com", "code": sender.deliveries[-1]["code"]},
    )
    assert correct_after_limit.status_code == 400


async def test_expired_code_is_rejected_and_consumed(api_client) -> None:
    client, sender = api_client
    code = await request_code(client, sender)
    database = client._transport.app.state.database
    async with database.session_factory() as session:
        await session.execute(
            update(OtpChallenge).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    response = await client.post(
        "/auth/verify-otp",
        json={"email": "person@example.com", "code": code},
    )
    assert response.status_code == 400
    async with database.session_factory() as session:
        challenge = (await session.execute(select(OtpChallenge))).scalar_one()
        assert challenge.consumed


async def test_single_use_is_atomic_under_concurrent_verification(
    database,
    settings,
) -> None:
    sender = RecordingOtpSender()
    async with database.session_factory() as issue_session:
        await AuthService(
            session=issue_session,
            settings=settings,
            email_sender=sender,
        ).request_otp("person@example.com")
    code = sender.deliveries[-1]["code"]

    async def verify_once() -> str | Exception:
        async with database.session_factory() as session:
            try:
                return await AuthService(
                    session=session,
                    settings=settings,
                    email_sender=sender,
                ).verify_otp("person@example.com", code)
            except Exception as exc:
                return exc

    first, second = await asyncio.gather(verify_once(), verify_once())

    assert sum(isinstance(item, str) for item in (first, second)) == 1
    failures = [item for item in (first, second) if isinstance(item, Exception)]
    assert len(failures) == 1 and isinstance(failures[0], InvalidOtpError)


async def test_used_code_cannot_be_replayed(api_client) -> None:
    client, sender = api_client
    code = await request_code(client, sender)
    body = {"email": "person@example.com", "code": code}

    assert (await client.post("/auth/verify-otp", json=body)).status_code == 200
    assert (await client.post("/auth/verify-otp", json=body)).status_code == 400
