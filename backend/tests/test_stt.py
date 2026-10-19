from __future__ import annotations

import asyncio

import pytest

from app.voice.stt import SarvamSttClient, SttConfig


@pytest.mark.parametrize("transport", ["legacy", "realtime"])
@pytest.mark.parametrize(
    ("owner", "cap"),
    [
        ("session", 1),
        ("other-job", 1),
        ("other-job", None),
        ("live-eval:smoke", None),
        ("live-eval:smoke", 0),
        ("live-eval:smoke", -1),
        ("live-eval:smoke", True),
        ("live-eval:smoke", 1.5),
    ],
)
def test_recognition_evaluation_requires_explicit_job_and_integer_cap(
    metered_stt, transport, owner, cap
):
    from app.costs.models import CostScope
    from app.voice.providers.sarvam_realtime import SarvamRealtimeClient

    original, costs, _, _ = metered_stt
    scope = (
        CostScope(user_id="eval-owner", session_id="session")
        if owner == "session"
        else CostScope(user_id="eval-owner", job_id=owner)
    )
    client_type = SarvamSttClient if transport == "legacy" else SarvamRealtimeClient
    with pytest.raises(ValueError, match="scope|allowance"):
        client_type(
            original._config,
            costs=costs,
            scope=scope,
            expires_at=original._expires_at,
            evaluation_cap_micro_inr=cap,
        )


async def test_recognition_evaluation_cap_covers_reconnect(metered_stt, monkeypatch, database):
    from sqlalchemy import text

    from app.costs.models import BudgetExceeded, CostScope

    original, costs, _, _ = metered_stt
    client = SarvamSttClient(
        original._config,
        costs=costs,
        scope=CostScope(user_id="eval-owner", job_id="live-eval:baseline"),
        expires_at=original._expires_at,
        evaluation_cap_micro_inr=1_000_000,
    )
    opened = []

    async def open_connection():
        opened.append(True)
        return StreamConnection()

    monkeypatch.setattr(client, "_open_connection", open_connection)
    try:
        await client.connect()
        with pytest.raises(BudgetExceeded, match="evaluation allowance exhausted"):
            await client.reconnect()
        assert opened == [True]
        async with database.engine.connect() as conn:
            states = (await conn.execute(text("SELECT state FROM cost_attempts"))).scalars().all()
        assert states == ["settled"]  # The silent first connection settles at the minimum.
    finally:
        await client.close()


async def test_recognition_evaluation_cap_survives_profiles_and_restart(metered_stt, monkeypatch):
    from app.costs.models import BudgetExceeded, CostScope
    from app.costs.service import CostService
    from app.db import Database

    original, costs, _, clock = metered_stt
    opened = []

    async def open_connection():
        opened.append(True)
        return StreamConnection()

    async def attempt(service, name):
        client = SarvamSttClient(
            original._config,
            costs=service,
            scope=CostScope(user_id="eval-owner", job_id=f"live-eval:{name}"),
            expires_at=original._expires_at,
            evaluation_cap_micro_inr=2_000_000,
        )
        monkeypatch.setattr(client, "_open_connection", open_connection)
        try:
            await client.connect()
            await client.send_audio(b"\0" * 32)  # Unreported audio keeps the full hold.
        finally:
            await client.close()

    await attempt(costs, "baseline")
    restarted = Database(str(costs.database.engine.url))
    try:
        resumed_costs = CostService(restarted, clock, costs.policy, costs.rate_card)
        await attempt(resumed_costs, "candidate")
        with pytest.raises(BudgetExceeded, match="evaluation allowance exhausted"):
            await attempt(resumed_costs, "retry")
        assert opened == [True, True]
    finally:
        await restarted.dispose()


class FakeSttConnection:
    def __init__(
        self,
        name: str,
        *,
        close_started: asyncio.Event | None = None,
        allow_close: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.close_started = close_started
        self.allow_close = allow_close
        self.closed = False
        self.close_code = 1000
        self.sent_messages: list[str] = []

    async def close(self) -> None:
        if self.close_started is not None:
            self.close_started.set()
        if self.allow_close is not None:
            await self.allow_close.wait()
        self.closed = True

    async def send(self, message: str) -> None:
        if self.closed:
            raise RuntimeError(f"send used stale closed connection: {self.name}")
        self.sent_messages.append(message)


@pytest.mark.asyncio
async def test_audio_waiting_during_reconnect_uses_new_connection(monkeypatch, metered_stt) -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    old_connection = FakeSttConnection(
        "old",
        close_started=close_started,
        allow_close=allow_close,
    )
    new_connection = FakeSttConnection("new")
    client, _, _, _ = metered_stt

    async def open_old_connection():
        return old_connection

    monkeypatch.setattr(client, "_open_connection", open_old_connection)
    await client.connect()

    async def open_new_connection() -> FakeSttConnection:
        return new_connection

    monkeypatch.setattr(client, "_open_connection", open_new_connection)

    reconnect = asyncio.create_task(client.reconnect())
    await close_started.wait()
    send = asyncio.create_task(client.send_audio(b"\0\0"))
    await asyncio.sleep(0)
    allow_close.set()

    await reconnect
    await send

    assert old_connection.sent_messages == []
    assert len(new_connection.sent_messages) == 1


@pytest.fixture
async def metered_stt(database):
    import time
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from app.costs.models import CostScope
    from app.costs.rates import PriceAdjustment, RateCard, UnitPrice
    from app.costs.service import CostService
    from app.runtime import RuntimePolicy
    from app.voice.admission import CallAdmission

    clock = SimpleNamespace(now=datetime.now(UTC))
    clock.utc_now = lambda: clock.now
    clock.monotonic = time.monotonic
    policy = RuntimePolicy(
        initialization_seconds=0.05, session_micro_inr=5_000_000, user_day_micro_inr=10_000_000
    )
    rates = RateCard(
        version="stt-fixture",
        captured_at=clock.now,
        valid_until=clock.now + timedelta(days=1),
        source="https://example.com/fixture",
        currency="INR",
        fx_micro_inr=1_000_000,
        qualified=True,
        taxes_and_fees=PriceAdjustment(numerator=1, denominator=1, note="Synthetic fixture"),
        unit_prices={
            "sarvam": {
                "saaras:v3": {"audio_ms": UnitPrice(amount_micros=1_000_000, per_units=120_000)}
            }
        },
    )
    costs = CostService(database, clock, policy, rates)
    admission = CallAdmission(
        database, clock, policy, costs, stt_provider="sarvam", stt_model="saaras:v3"
    )
    lease = await admission.admit("user", "session")
    client = SarvamSttClient(
        SttConfig(
            api_key="unused",
            model="saaras:v3",
            language_code="unknown",
            sample_rate_hz=16000,
            input_audio_codec="pcm_s16le",
            audio_encoding="audio/pcm",
        ),
        costs=costs,
        scope=CostScope(user_id="user", session_id="session"),
        expires_at=lease.expires_at,
    )
    try:
        yield client, costs, admission, clock
    finally:
        await client.close()
        await admission.release("session")


class StreamConnection(FakeSttConnection):
    def __init__(self, name="fake", messages=()):
        super().__init__(name)
        self.messages = messages

    async def __aiter__(self):
        import json

        for item in self.messages:
            yield json.dumps(item)


async def test_stt_reserves_full_duration_before_transport(metered_stt, monkeypatch, database):
    from sqlalchemy import text

    client, costs, _, _ = metered_stt
    calls = []

    async def open_connection():
        async with database.engine.connect() as conn:
            row = (await conn.execute(text("SELECT maximum_micro_inr FROM cost_attempts"))).one()
        assert row.maximum_micro_inr == 1_000_000
        calls.append("open")
        return StreamConnection()

    monkeypatch.setattr(client, "_open_connection", open_connection)
    await client.connect()
    assert calls == ["open"]


async def test_stt_budget_failure_never_opens_transport(metered_stt, monkeypatch):
    from app.costs.models import BudgetExceeded, WorkBound

    client, costs, _, _ = metered_stt
    await costs.reserve(
        client._scope, "other", "sarvam", "saaras:v3", WorkBound(units={"audio_ms": 600_000})
    )
    calls = []

    async def open_connection():
        calls.append("open")
        return StreamConnection()

    monkeypatch.setattr(client, "_open_connection", open_connection)
    with pytest.raises(BudgetExceeded):
        await client.connect()
    assert calls == []


async def test_clean_close_settles_covered_provider_duration(metered_stt, monkeypatch, database):
    from sqlalchemy import text

    client, costs, _, _ = metered_stt
    connection = StreamConnection(
        messages=[
            {
                "type": "data",
                "data": {
                    "request_id": "r1",
                    "metrics": {"audio_duration": 1.1},
                    "transcript": "hello",
                },
            }
        ]
    )

    async def open_connection():
        return connection

    monkeypatch.setattr(client, "_open_connection", open_connection)
    await client.connect()
    await client.send_audio(b"\0" * 32_000)
    assert len([event async for event in client.messages()]) == 1
    await client.close()
    await client.close()
    async with database.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT state,maximum_micro_inr,actual_micro_inr,usage_json FROM cost_attempts"
                )
            )
        ).one()
    assert row.state == "settled" and row.actual_micro_inr == 16_667
    assert "2000" in row.usage_json and "r1" in row.usage_json


@pytest.mark.parametrize(("reported_s", "state"), [(1.0, "settled"), (0.99, "unknown")])
async def test_coverage_allows_only_the_providers_hundredth_second_rounding(
    metered_stt, monkeypatch, database, reported_s, state
):
    from sqlalchemy import text

    client, _, _, _ = metered_stt
    message = {"request_id": "r1", "metrics": {"audio_duration": reported_s}, "transcript": "hi"}
    connection = StreamConnection(messages=[{"type": "data", "data": message}])

    async def open_connection():
        return connection

    monkeypatch.setattr(client, "_open_connection", open_connection)
    await client.connect()
    await client.send_audio(b"\0" * 32_064)  # 1002 ms, reported as 1.00 s by the provider
    _ = [event async for event in client.messages()]
    await client.close()
    async with database.engine.connect() as conn:
        row = (await conn.execute(text("SELECT state,usage_json FROM cost_attempts"))).one()
    assert row.state == state
    if state == "settled":
        assert '"audio_ms":2000' in row.usage_json.replace(" ", "")  # billed on what was sent


@pytest.mark.parametrize("failure", ["no-final", "partial", "failed-close", "abnormal-close"])
async def test_unknown_stream_charges_only_the_audio_sent(
    metered_stt, monkeypatch, database, failure
):
    from sqlalchemy import text

    client, costs, _, _ = metered_stt
    connection = StreamConnection(
        messages=[]
        if failure == "no-final"
        else [
            {
                "type": "data",
                "data": {
                    "request_id": "r1",
                    "metrics": {"audio_duration": 1 if failure == "abnormal-close" else 0.5},
                    "transcript": "partial",
                },
            }
        ]
    )
    if failure == "abnormal-close":
        connection.close_code = 1006
    if failure == "failed-close":

        async def broken_close():
            raise OSError("ambiguous close")

        connection.close = broken_close

    async def open_connection():
        return connection

    monkeypatch.setattr(client, "_open_connection", open_connection)
    await client.connect()
    await client.send_audio(b"\0" * 32_000)
    _ = [event async for event in client.messages()]
    await client.close()
    async with database.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT state,actual_micro_inr,maximum_micro_inr FROM cost_attempts")
            )
        ).one()
    # 1 s of audio was sent: charged as 2 s (one second of rounding slack), not the whole hold.
    assert (
        row.state == "unknown"
        and row.actual_micro_inr == 16_667
        and row.maximum_micro_inr == 1_000_000
    )


async def test_connect_timeout_or_cancellation_preserves_hold(metered_stt, monkeypatch, database):
    from sqlalchemy import text

    client, _, _, _ = metered_stt

    async def hang():
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_open_connection", hang)
    with pytest.raises(TimeoutError):
        await client.connect()
    await client.close()
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT state FROM cost_attempts"))
        ).scalar_one() == "unknown"


async def test_reconnect_reserves_a_distinct_attempt(metered_stt, monkeypatch, database):
    from sqlalchemy import text

    client, _, _, _ = metered_stt
    calls = []

    async def open_connection():
        calls.append("open")
        return StreamConnection()

    monkeypatch.setattr(client, "_open_connection", open_connection)
    await client.connect()
    await client.reconnect()
    await client.close()
    async with database.engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id,state FROM cost_attempts"))).all()
    assert len(rows) == len({row.id for row in rows}) == len(calls) == 2


@pytest.mark.parametrize("close_code", [1000, 1006])
async def test_silent_connection_settles_at_the_minimum_only_after_a_clean_close(
    metered_stt, monkeypatch, database, close_code
):
    from sqlalchemy import text

    client, _, _, _ = metered_stt

    class Silent(StreamConnection):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield

    connection = Silent()
    connection.close_code = close_code

    async def open_connection():
        return connection

    monkeypatch.setattr(client, "_open_connection", open_connection)
    await client.connect()
    receiver = asyncio.create_task(anext(client.messages()))
    await asyncio.sleep(0)
    receiver.cancel()  # Every call teardown cancels the receiver like this.
    await asyncio.gather(receiver, return_exceptions=True)
    await client.close()
    async with database.engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT state,actual_micro_inr,usage_json FROM cost_attempts"))
        ).one()
    if close_code == 1000:
        assert row.state == "settled" and row.actual_micro_inr == 8_334 and "1000" in row.usage_json
    else:
        assert row.state == "unknown" and row.actual_micro_inr == 8_334


async def test_expired_stream_cannot_send_or_reconnect(metered_stt, monkeypatch):
    from datetime import timedelta

    client, _, _, clock = metered_stt
    connection = StreamConnection()
    calls = []

    async def open_connection():
        calls.append("open")
        return connection

    monkeypatch.setattr(client, "_open_connection", open_connection)
    await client.connect()
    clock.now += timedelta(seconds=121)
    with pytest.raises(Exception, match="expired|deadline"):
        await client.send_audio(b"\0\0")
    with pytest.raises(Exception, match="expired|deadline"):
        await client.reconnect()
    assert connection.sent_messages == [] and len(calls) == 1


@pytest.mark.parametrize("mode", ["failure", "hang"])
async def test_initialization_never_emits_ready_or_bills_wallet(
    metered_stt, database, settings, monkeypatch, mode
):
    import time
    from datetime import timedelta
    from types import SimpleNamespace

    from pydantic import SecretStr
    from sqlalchemy import text

    from app.models import User
    from app.voice import session as implementation
    from app.voice.admission import AdmissionLease
    from app.voice.protocol import ProbeOptions
    from app.wallet.service import WalletService

    _, costs, admission, _ = metered_stt
    async with database.session_factory() as db:
        db.add(User(id="user", email="user@example.com", is_verified=True))
        await db.commit()
    wallet = WalletService(database)
    await wallet.recharge(user_id="user", amount_paise=500, max_amount_paise=1000)
    events = []
    constructed = []

    class Speech:
        def __init__(self, config, **kwargs):
            constructed.append(kwargs)

        async def connect(self):
            if mode == "hang":
                await asyncio.Event().wait()
            raise RuntimeError("connection failed")

        async def close(self):
            pass

    class Search:
        def __init__(self, **kwargs):
            pass

        async def close(self):
            pass

    class Socket:
        state = SimpleNamespace(
            voice_grant=SimpleNamespace(session_id="session"),
            admission_lease=AdmissionLease(
                "session",
                admission.clock.now + timedelta(seconds=120),
                "envelope",
            ),
            cost_service=costs,
            call_admission=admission,
        )

        async def send_json(self, value):
            events.append(value)

        async def close(self, **kwargs):
            pass

    legacy = implementation.RECOGNITION["sarvam_legacy"]
    monkeypatch.setitem(implementation.RECOGNITION, "sarvam_legacy", (Speech, legacy[1]))
    monkeypatch.setattr(implementation, "ExaSearchClient", Search)
    monkeypatch.setattr(implementation, "AgentRunner", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        implementation.VoiceSession, "_build_gate", lambda self: SimpleNamespace(vad_name="fixture")
    )
    settings.sarvam_api_key = SecretStr("unused-fixture")
    voice = implementation.VoiceSession(
        websocket=Socket(),
        settings=settings,
        probe_options=ProbeOptions(),
        database=database,
        user_id="user",
        wallet=wallet,
        starting_balance_paise=500,
        connected_at=time.monotonic(),
    )
    with pytest.raises((RuntimeError, TimeoutError)) as failed:
        await asyncio.wait_for(voice.run(), timeout=0.2)
    if mode == "hang":
        assert "initializ" in str(failed.value).lower()
    assert not any(event["type"] == "ready" for event in events)
    assert await wallet.balance("user") == 500
    async with database.engine.connect() as conn:
        assert (await conn.execute(text("SELECT cost_paise FROM usage_sessions"))).scalar_one() == 0
    assert constructed[0]["costs"] is costs and constructed[0]["scope"].session_id == "session"


async def test_cancelled_connect_keeps_its_reservation(metered_stt, database, monkeypatch):
    from sqlalchemy import text

    client, _, _, _ = metered_stt
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_open_connection", hang)
    task = asyncio.create_task(client.connect())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT state FROM cost_attempts"))
        ).scalar_one() == "unknown"


async def test_expiry_after_reserve_before_transport_is_confirmed_zero(
    metered_stt, database, monkeypatch
):
    from sqlalchemy import text

    client, costs, _, clock = metered_stt
    original = costs.reserve

    async def reserve(*args, **kwargs):
        result = await original(*args, **kwargs)
        clock.now = client._expires_at
        return result

    monkeypatch.setattr(costs, "reserve", reserve)
    calls = []

    async def open_connection():
        calls.append("open")
        return StreamConnection()

    monkeypatch.setattr(client, "_open_connection", open_connection)
    with pytest.raises(Exception, match="deadline"):
        await client.connect()
    assert calls == []
    async with database.engine.connect() as conn:
        row = (await conn.execute(text("SELECT state,actual_micro_inr FROM cost_attempts"))).one()
    assert row.state == "settled" and row.actual_micro_inr == 0


async def test_audio_cannot_exceed_the_reserved_work_bound(metered_stt, monkeypatch):
    client, _, _, _ = metered_stt
    connection = StreamConnection()

    async def open_connection():
        return connection

    monkeypatch.setattr(client, "_open_connection", open_connection)
    await client.connect()
    with pytest.raises(Exception, match="reserved PCM"):
        await client.send_audio(b"\0" * (120 * 32000 + 2))
    assert connection.sent_messages == []


async def test_stream_deadline_closes_even_without_another_message(metered_stt, monkeypatch):
    from datetime import timedelta

    original, costs, _, clock = metered_stt
    client = SarvamSttClient(
        original._config,
        costs=costs,
        scope=original._scope,
        expires_at=clock.now + timedelta(milliseconds=20),
    )
    connection = StreamConnection()

    async def open_connection():
        return connection

    monkeypatch.setattr(client, "_open_connection", open_connection)
    try:
        await client.connect()
        for _ in range(50):
            if connection.closed:
                break
            await asyncio.sleep(0.01)
        assert connection.closed
    finally:
        await client.close()


async def test_cancellation_during_rotation_cannot_open_a_new_transport(
    metered_stt, monkeypatch, database
):
    from sqlalchemy import text

    client, _, _, _ = metered_stt
    close_started = asyncio.Event()
    connection = StreamConnection()

    async def slow_close():
        close_started.set()
        await asyncio.Event().wait()

    connection.close = slow_close
    calls = []

    async def open_connection():
        calls.append("open")
        return connection

    monkeypatch.setattr(client, "_open_connection", open_connection)
    await client.connect()
    rotation = asyncio.create_task(client.reconnect())
    await close_started.wait()
    rotation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await rotation
    assert calls == ["open"]
    async with database.engine.connect() as conn:
        rows = (await conn.execute(text("SELECT state,maximum_micro_inr FROM cost_attempts"))).all()
    assert rows == [("unknown", 1_000_000)]
