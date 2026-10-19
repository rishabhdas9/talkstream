from __future__ import annotations

import asyncio
import hashlib
import importlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.auth.tokens import encode_access_token
from app.models import User


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def utc_now(self):
        return self.now

    def monotonic(self):
        return 1.0


async def logged_in(api_client):
    client, sender = api_client
    app = client._transport.app
    await client.post("/auth/request-otp", json={"email": "person@example.com"})
    response = await client.post(
        "/auth/verify-otp",
        json={
            "email": "person@example.com",
            "code": sender.deliveries[-1]["code"],
        },
    )
    return client, app, response.json()["access_token"]


async def test_ticket_endpoint_requires_valid_bearer_identity(api_client):
    client, app, token = await logged_in(api_client)
    assert (await client.post("/auth/voice-ticket")).status_code == 401
    assert (
        await client.post(
            "/auth/voice-ticket",
            headers={
                "Authorization": "Bearer invalid",
            },
        )
    ).status_code == 401
    settings = app.state.settings
    from app.auth.tokens import decode_access_token

    claims = decode_access_token(
        token,
        secret=settings.jwt_secret_value,
        algorithm=settings.jwt_algorithm,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    expired = encode_access_token(
        user_id=claims.user_id,
        email=claims.email,
        secret=settings.jwt_secret_value,
        algorithm=settings.jwt_algorithm,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        ttl_seconds=1,
        now=datetime.now(UTC) - timedelta(minutes=1),
    )
    assert (
        await client.post(
            "/auth/voice-ticket",
            headers={
                "Authorization": f"Bearer {expired}",
            },
        )
    ).status_code == 401


async def test_ticket_is_opaque_hashed_short_lived_and_preserves_access_deadline(api_client):
    client, app, token = await logged_in(api_client)
    response = await client.post("/auth/voice-ticket", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    ticket = response.json()["ticket"]
    assert 32 <= len(ticket) <= 128 and "." not in ticket and ticket != token
    assert response.json()["expires_in_seconds"] <= 30
    assert response.headers["cache-control"] == "no-store"
    async with app.state.database.session_factory() as session:
        row = (await session.execute(text("SELECT * FROM voice_tickets"))).mappings().one()
    assert ticket not in str(dict(row))
    assert row["ticket_hash"] == hashlib.sha256(ticket.encode()).hexdigest()
    module = importlib.import_module("app.auth.tickets")
    service = module.TicketService(app.state.database, app.state.clock, app.state.runtime_policy)
    grant = await service.consume(ticket)
    import jwt

    assert (
        grant.access_expires_at.timestamp()
        == jwt.decode(token, options={"verify_signature": False})["exp"]
    )
    assert grant.session_id and grant.user_id
    assert await service.consume(ticket) is None


@pytest.mark.parametrize("failure", ["ticket_expiry", "access_expiry", "revoked", "changed_email"])
async def test_ticket_rejects_expiry_and_changed_identity(api_client, failure):
    client, app, token = await logged_in(api_client)
    clock = Clock()
    app.state.clock = clock
    response = await client.post("/auth/voice-ticket", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    module = importlib.import_module("app.auth.tickets")
    service = module.TicketService(app.state.database, clock, app.state.runtime_policy)
    ticket = response.json()["ticket"]
    if failure == "ticket_expiry":
        clock.now += timedelta(seconds=30)
    elif failure == "access_expiry":
        async with app.state.database.session_factory() as session:
            await session.execute(text("DELETE FROM voice_tickets"))
            user = await session.get(
                User, (await session.execute(text("SELECT id FROM users"))).scalar_one()
            )
            await session.commit()
        ticket = await service.issue(user, access_expires_at=clock.now + timedelta(seconds=1))
        clock.now += timedelta(seconds=1)
    else:
        sql = (
            "UPDATE users SET is_verified=0"
            if failure == "revoked"
            else "UPDATE users SET email='changed@example.com'"
        )
        async with app.state.database.session_factory() as session:
            await session.execute(text(sql))
            await session.commit()
    assert await service.consume(ticket) is None


async def test_ticket_consumption_has_one_concurrent_winner_and_survives_restart(api_client):
    client, app, token = await logged_in(api_client)
    response = await client.post("/auth/voice-ticket", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    module = importlib.import_module("app.auth.tickets")
    from app.db import Database

    other = Database(app.state.settings.database_url)
    try:
        service = module.TicketService(other, app.state.clock, app.state.runtime_policy)
        grants = await asyncio.gather(
            *(service.consume(response.json()["ticket"]) for _ in range(8))
        )
        assert sum(grant is not None for grant in grants) == 1
    finally:
        await other.dispose()
