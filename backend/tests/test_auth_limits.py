from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.auth.email import OtpDeliveryError


class FrozenClock:
    def __init__(self):
        self.now = datetime(2026, 10, 18, 12, tzinfo=UTC)

    def utc_now(self):
        return self.now

    def monotonic(self):
        return 0.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def auth_rig(api_client):
    client, sender = api_client
    app = client._transport.app
    clock = FrozenClock()
    app.state.clock = clock
    return client, sender, app, clock


async def send(client, email, **kwargs):
    return await client.post("/auth/request-otp", json={"email": email}, **kwargs)


async def test_cooldown_does_not_invalidate_the_existing_challenge(auth_rig):
    client, sender, _, _ = auth_rig
    assert (await send(client, "person@example.com")).status_code == 202
    code = sender.deliveries[-1]["code"]
    denied = await send(client, "PERSON@example.com")
    assert denied.status_code == 429
    assert len(sender.deliveries) == 1
    verified = await client.post(
        "/auth/verify-otp", json={"email": "person@example.com", "code": code}
    )
    assert verified.status_code == 200


async def test_concurrent_send_admits_only_one_challenge(auth_rig):
    client, sender, _, _ = auth_rig
    responses = await asyncio.gather(*(send(client, "person@example.com") for _ in range(8)))
    assert sorted(response.status_code for response in responses) == [202] + [429] * 7
    assert len(sender.deliveries) == 1


async def test_forwarded_headers_cannot_bypass_peer_ip_limit(auth_rig):
    client, sender, _, _ = auth_rig
    responses = [
        await send(client, f"person{n}@example.com", headers={"X-Forwarded-For": f"192.0.2.{n}"})
        for n in range(31)
    ]
    assert [response.status_code for response in responses] == [202] * 30 + [429]
    assert len(sender.deliveries) == 30


async def test_email_limit_is_sliding_and_survives_new_service_instances(auth_rig):
    client, sender, _, clock = auth_rig
    for _ in range(3):
        assert (await send(client, "person@example.com")).status_code == 202
        clock.advance(60)
    assert (await send(client, "person@example.com")).status_code == 429
    clock.advance(3420)
    assert (await send(client, "person@example.com")).status_code == 202
    assert len(sender.deliveries) == 4


async def test_global_day_limit_cannot_be_bypassed_by_new_ips(auth_rig):
    _, sender, app, clock = auth_rig
    responses = []
    for n in range(81):
        transport = httpx.ASGITransport(app=app, client=(f"192.0.2.{n + 1}", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as peer:
            responses.append((await send(peer, f"person{n}@example.com")).status_code)
    assert responses == [202] * 80 + [429]
    assert len(sender.deliveries) == 80
    clock.now = datetime(2026, 10, 18, 18, 30, tzinfo=UTC)
    transport = httpx.ASGITransport(app=app, client=("198.51.100.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as peer:
        assert (await send(peer, "nextday@example.com")).status_code == 202


async def test_failed_delivery_still_consumes_admission_quota(auth_rig):
    client, _, app, _ = auth_rig

    class UnavailableEmail:
        async def send_otp(self, **kwargs):
            raise OtpDeliveryError("upstream unavailable")

    app.state.email_sender = UnavailableEmail()
    assert (await send(client, "person@example.com")).status_code == 502
    assert (await send(client, "person@example.com")).status_code == 429


async def test_limits_survive_application_restart(settings):
    from conftest import RecordingOtpSender

    from app.main import create_app

    sender = RecordingOtpSender()
    clock = FrozenClock()
    app = create_app(settings)
    app.state.clock = clock
    async with app.router.lifespan_context(app):
        app.state.email_sender = sender
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await send(client, "person@example.com")).status_code == 202
    restarted = create_app(settings)
    restarted.state.clock = clock
    async with restarted.router.lifespan_context(restarted):
        restarted.state.email_sender = sender
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as peer:
            assert (await send(peer, "person@example.com")).status_code == 429
    assert len(sender.deliveries) == 1


async def test_counter_records_do_not_store_raw_email_or_ip(auth_rig):
    from sqlalchemy import text

    client, _, app, _ = auth_rig
    await send(client, "private-address@example.com")
    async with app.state.database.session_factory() as session:
        row = (
            await session.execute(text("SELECT email_hash,ip_hash FROM otp_send_attempts"))
        ).one()
        assert len(row[0]) == 64 and len(row[1]) == 64
        assert "private-address" not in row[0] and "127.0.0.1" not in row[1]
