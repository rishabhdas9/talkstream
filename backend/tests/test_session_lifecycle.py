from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.costs.models import CostScope, WorkBound
from app.costs.rates import PriceAdjustment, RateCard, RateUnavailable, UnitPrice
from app.costs.service import CostService
from app.main import create_app
from app.models import UsageSession, User
from app.runtime import RuntimePolicy
from app.voice.admission import CallAdmission
from app.wallet.service import WalletError, WalletService


@pytest.fixture
async def recovery_rig(database):
    clock = SimpleNamespace(now=datetime(2026, 10, 18, tzinfo=UTC))
    clock.utc_now = lambda: clock.now
    clock.monotonic = lambda: 100.0
    policy = RuntimePolicy()
    rates = RateCard(
        version="recovery-fixture",
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
    await admission.admit("owner", "call")
    async with database.session_factory() as session:
        session.add(User(id="owner", email="owner@example.test", is_verified=True))
        await session.commit()
        session.add(
            UsageSession(id="call", user_id="owner", started_at=clock.now - timedelta(seconds=20))
        )
        await session.commit()
    wallet = WalletService(database)
    await wallet.recharge(user_id="owner", amount_paise=500, max_amount_paise=1000)
    return SimpleNamespace(
        database=database,
        clock=clock,
        costs=costs,
        admission=admission,
        wallet=wallet,
        scope=CostScope(user_id="owner", session_id="call"),
        policy=policy,
    )


async def ready_and_tick(rig):
    await rig.wallet.mark_ready(user_id="owner", usage_session_id="call", ready_at=rig.clock.now)
    await rig.wallet.bill_session(
        user_id="owner", usage_session_id="call", billable_seconds=5, target_cost_paise=17
    )


async def session_row(rig):
    async with rig.database.engine.connect() as conn:
        return (
            (await conn.execute(text("SELECT * FROM usage_sessions WHERE id='call'")))
            .mappings()
            .one()
        )


async def test_readiness_anchor_is_owned_aware_and_immutable(recovery_rig):
    r = recovery_rig
    with pytest.raises(WalletError):
        await r.wallet.mark_ready(user_id="other", usage_session_id="call", ready_at=r.clock.now)
    with pytest.raises(ValueError, match="aware"):
        await r.wallet.mark_ready(
            user_id="owner", usage_session_id="call", ready_at=datetime(2026, 10, 18)
        )
    await ready_and_tick(r)
    await r.wallet.mark_ready(user_id="owner", usage_session_id="call", ready_at=r.clock.now)
    with pytest.raises(WalletError):
        await r.wallet.mark_ready(
            user_id="owner", usage_session_id="call", ready_at=r.clock.now + timedelta(seconds=1)
        )
    assert datetime.fromisoformat((await session_row(r))["ready_at"]).replace(
        tzinfo=UTC
    ) == datetime(2026, 10, 18, tzinfo=UTC)


async def test_restart_charges_holds_left_by_the_old_unknown_rule(recovery_rig):
    from app.voice.lifecycle import recover_abandoned_sessions

    r = recovery_rig
    await r.costs.reserve(
        r.scope, "speech", "sarvam", "saaras:v3", WorkBound(units={"audio_ms": 120_000})
    )
    async with r.database.engine.begin() as conn:
        await conn.execute(
            text("UPDATE cost_attempts SET state='unknown',outcome='cancelled' WHERE id='speech'")
        )
    await recover_abandoned_sessions(r.database, r.clock, r.costs)
    async with r.database.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT state,outcome,actual_micro_inr FROM cost_attempts WHERE id='speech'")
            )
        ).one()
        held = (await conn.execute(text("SELECT SUM(held_micro_inr) FROM cost_periods"))).scalar()
    assert tuple(row) == ("unknown", "cancelled", 1_000_000) and held == 0


async def test_restart_ends_at_last_tick_without_charging_downtime(recovery_rig):
    from app.voice.lifecycle import recover_abandoned_sessions

    r = recovery_rig
    await ready_and_tick(r)
    await r.costs.reserve(
        r.scope, "speech", "sarvam", "saaras:v3", WorkBound(units={"audio_ms": 120_000})
    )
    r.clock.now += timedelta(days=2)
    assert await recover_abandoned_sessions(r.database, r.clock, r.costs) == 1
    row = await session_row(r)
    assert datetime.fromisoformat(row["ended_at"]).replace(tzinfo=UTC) == datetime(
        2026, 10, 18, 0, 0, 5, tzinfo=UTC
    )
    assert row["end_reason"] == "process_restart"
    assert row["billed_seconds"] == 5 and row["cost_paise"] == 17
    assert await r.wallet.balance("owner") == 483
    assert await r.admission.active_count() == 0
    assert await r.costs.outstanding_micro_inr(r.scope) == 0
    async with r.database.engine.connect() as conn:
        assert tuple(
            (
                await conn.execute(
                    text("SELECT state,actual_micro_inr FROM cost_attempts WHERE id='speech'")
                )
            ).one()
        ) == ("unknown", 1_000_000)
    assert await recover_abandoned_sessions(r.database, r.clock, r.costs) == 0
    assert await r.wallet.balance("owner") == 483
    with pytest.raises(WalletError, match="ended"):
        await r.wallet.bill_session(
            user_id="owner",
            usage_session_id="call",
            billable_seconds=86400,
            target_cost_paise=10000,
        )


async def test_restart_before_ready_has_no_mock_wallet_charge(recovery_rig):
    from app.voice.lifecycle import recover_abandoned_sessions

    r = recovery_rig
    r.clock.now += timedelta(days=2)
    await recover_abandoned_sessions(r.database, r.clock, r.costs)
    row = await session_row(r)
    assert row["cost_paise"] == row["billed_seconds"] == 0
    assert datetime.fromisoformat(row["ended_at"]).replace(tzinfo=UTC) == datetime(
        2026, 10, 17, 23, 59, 40, tzinfo=UTC
    )
    assert await r.wallet.balance("owner") == 500
    assert await r.costs.outstanding_micro_inr(r.scope) == 0


async def test_app_startup_recovers_without_current_rate_file(recovery_rig, settings):
    r = recovery_rig
    await ready_and_tick(r)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        row = await session_row(r)
        assert row["end_reason"] == "process_restart"
        assert await r.admission.active_count() == 0
        assert await r.wallet.balance("owner") == 483
        with pytest.raises(RateUnavailable):
            app.state.cost_service.estimate(
                "sarvam", "saaras:v3", WorkBound(units={"audio_ms": 1000})
            )


async def test_database_owner_refuses_another_lifespan_before_recovery(recovery_rig, settings):
    r = recovery_rig
    app = create_app(settings)
    other = create_app(settings)
    async with app.router.lifespan_context(app):
        await r.admission.admit("second", "live-call")
        with pytest.raises(RuntimeError, match="already owned"):
            async with other.router.lifespan_context(other):
                pytest.fail("A second owner entered its serving lifespan")
        assert await r.admission.active_count() == 1
    async with other.router.lifespan_context(other):
        assert await r.admission.active_count() == 0


async def test_recovery_failure_rolls_back_all_state(recovery_rig, monkeypatch):
    from app.voice.lifecycle import recover_abandoned_sessions

    r = recovery_rig
    await ready_and_tick(r)
    await r.costs.reserve(
        r.scope, "speech", "sarvam", "saaras:v3", WorkBound(units={"audio_ms": 120_000})
    )
    release = r.costs.release_envelope

    async def fail(*args, **kwargs):
        await release(*args, **kwargs)
        raise RuntimeError("Injected storage failure")

    monkeypatch.setattr(r.costs, "release_envelope", fail)
    with pytest.raises(RuntimeError, match="Injected"):
        await recover_abandoned_sessions(r.database, r.clock, r.costs)
    assert (await session_row(r))["ended_at"] is None
    assert await r.admission.active_count() == 1
    async with r.database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT state FROM cost_attempts"))
        ).scalar_one() == "reserved"
    assert await r.wallet.balance("owner") == 483


async def test_readiness_is_idempotent_across_utc_offsets(recovery_rig):
    from datetime import timezone

    r = recovery_rig
    await r.wallet.mark_ready(user_id="owner", usage_session_id="call", ready_at=r.clock.now)
    same_instant = r.clock.now.astimezone(timezone(timedelta(hours=5, minutes=30)))
    await r.wallet.mark_ready(user_id="owner", usage_session_id="call", ready_at=same_instant)
    row = await session_row(r)
    assert datetime.fromisoformat(row["ready_at"]).replace(tzinfo=UTC) == datetime(
        2026, 10, 18, tzinfo=UTC
    )


async def test_recovery_refuses_a_foreign_cost_database(recovery_rig, tmp_path):
    from app.db import Database
    from app.voice.lifecycle import recover_abandoned_sessions

    r = recovery_rig
    other = Database(f"sqlite+aiosqlite:///{tmp_path / 'other.db'}")
    try:
        with pytest.raises(ValueError, match="database"):
            await recover_abandoned_sessions(other, r.clock, r.costs)
        assert (await session_row(r))["ended_at"] is None
        assert await r.admission.active_count() == 1
    finally:
        await other.dispose()


async def test_unpriced_service_can_cleanup_but_cannot_admit_paid_work(recovery_rig):
    from app.voice.lifecycle import recover_abandoned_sessions

    r = recovery_rig
    costs = CostService(r.database, r.clock, r.policy, None)
    with pytest.raises(RateUnavailable):
        costs.estimate("sarvam", "saaras:v3", WorkBound(units={"audio_ms": 1000}))
    with pytest.raises(RateUnavailable):
        await costs.reserve(
            r.scope, "unpriced", "sarvam", "saaras:v3", WorkBound(units={"audio_ms": 1000})
        )
    await recover_abandoned_sessions(r.database, r.clock, costs)
    assert await r.admission.active_count() == 0
    assert await costs.outstanding_micro_inr(r.scope) == 0


async def test_failed_startup_releases_database_owner(recovery_rig, settings, monkeypatch):
    from app import main

    original = main.recover_abandoned_sessions

    async def fail(*args):
        raise RuntimeError("Injected startup failure")

    app = create_app(settings)
    monkeypatch.setattr(main, "recover_abandoned_sessions", fail)
    with pytest.raises(RuntimeError, match="Injected"):
        async with app.router.lifespan_context(app):
            pytest.fail("Failed recovery must prevent serving")
    monkeypatch.setattr(main, "recover_abandoned_sessions", original)
    other = create_app(settings)
    async with other.router.lifespan_context(other):
        assert (await session_row(recovery_rig))["end_reason"] == "process_restart"


async def test_repeated_finish_finalizes_once_even_when_first_waiter_is_cancelled(recovery_rig):
    import asyncio

    from app.voice.lifecycle import SessionLifecycle

    r = recovery_rig
    await ready_and_tick(r)
    entered = asyncio.Event()
    proceed = asyncio.Event()
    reasons = []

    async def finalize(reason):
        reasons.append(reason)
        entered.set()
        await proceed.wait()
        await r.wallet.bill_session(
            user_id="owner", usage_session_id="call", billable_seconds=6, target_cost_paise=20
        )
        await r.admission.release("call")

    lifecycle = SessionLifecycle(finalize)
    first = asyncio.create_task(lifecycle.finish("disconnect"))
    await entered.wait()
    second = asyncio.create_task(lifecycle.finish("timeout"))
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    await lifecycle.finish("again")
    assert reasons == ["disconnect"]
    assert await r.wallet.balance("owner") == 480
    assert await r.admission.active_count() == 0


async def test_finish_timeout_does_not_wait_forever_for_swallowed_cancellation():
    import asyncio
    import time

    from app.voice.lifecycle import SessionCleanupError, SessionLifecycle

    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck(reason):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    lifecycle = SessionLifecycle(stuck, timeout_seconds=0.02)
    started = time.monotonic()
    try:
        with pytest.raises(SessionCleanupError):
            await asyncio.wait_for(lifecycle.finish("disconnect"), timeout=0.2)
        assert time.monotonic() - started < 0.15
        await asyncio.wait_for(cancelled.wait(), timeout=0.2)
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.fixture
async def voice_rig(recovery_rig, settings, monkeypatch):
    import asyncio
    import json

    from pydantic import SecretStr

    from app.voice import session as implementation
    from app.voice.lifecycle import SessionLifecycle
    from app.voice.protocol import ProbeOptions
    from app.voice.stt import SarvamSttClient

    r = recovery_rig
    async with r.database.engine.begin() as conn:
        await conn.execute(text("DELETE FROM usage_sessions WHERE id='call'"))
    state = SimpleNamespace(
        ready=asyncio.Event(),
        fail_ready=False,
        hang_connect=False,
        incoming=asyncio.Queue(),
        events=[],
        changed=asyncio.Event(),
        close_count=0,
        closed=asyncio.Event(),
        now=100.0,
    )
    r.clock.monotonic = lambda: state.now
    r.costs.policy = RuntimePolicy(initialization_seconds=0.05)

    class Connection:
        close_code = 1000

        def __init__(self):
            self.closed = asyncio.Event()

        async def __aiter__(self):
            await self.closed.wait()
            if False:
                yield ""

        async def close(self):
            state.close_count += 1
            self.closed.set()
            state.closed.set()

    async def open_connection(self):
        if state.hang_connect:
            await asyncio.Event().wait()
        return Connection()

    class Socket:
        def __init__(self):
            self.state = SimpleNamespace(
                admission_lease=None,
                cost_service=r.costs,
                call_admission=r.admission,
                voice_grant=SimpleNamespace(session_id="call"),
            )

        async def send_json(self, message):
            if message["type"] == "ready":
                if state.fail_ready:
                    raise RuntimeError("readiness transport failed")
                state.ready.set()
            state.events.append(message)
            state.changed.set()

        async def receive(self):
            return await state.incoming.get()

        async def close(self, **kwargs):
            pass

    socket = Socket()
    socket.state.admission_lease = await r.admission.admit("owner", "call")
    monkeypatch.setattr(SarvamSttClient, "_open_connection", open_connection)

    async def allow_policy(*args, **kwargs):
        return '{"allowed":true,"category":"allowed"}'

    async def reset_context():
        pass

    monkeypatch.setattr(
        implementation,
        "AgentRunner",
        lambda **kwargs: SimpleNamespace(
            classify_policy=allow_policy,
            _system_message=SimpleNamespace(content="Fixture assistant instructions."),
            reconcile_interrupted_history=lambda text: reset_context(),
            policy_evidence=lambda turn_id: [],
        ),
    )
    monkeypatch.setattr(
        implementation.VoiceSession, "_build_gate", lambda self: SimpleNamespace(vad_name="fixture")
    )
    settings.sarvam_api_key = SecretStr("unused-fixture")
    voice = implementation.VoiceSession(
        websocket=socket,
        settings=settings,
        probe_options=ProbeOptions(),
        database=r.database,
        user_id="owner",
        wallet=r.wallet,
        starting_balance_paise=500,
        connected_at=100.0,
    )
    voice.lifecycle = SessionLifecycle(voice._finalize_session, timeout_seconds=0.2)
    close_stt = voice._stt.close
    tasks = []

    async def start():
        task = asyncio.create_task(voice.run())
        tasks.append(task)
        await asyncio.wait_for(state.ready.wait(), timeout=0.5)
        # Allow the persisted ready anchor to complete before terminal signals.
        for _ in range(50):
            if voice._billing_started and len(voice._supervised_tasks) >= 4:
                break
            await asyncio.sleep(0.001)
        assert voice._billing_started and len(voice._supervised_tasks) >= 4
        return task

    async def end():
        await state.incoming.put(
            {"type": "websocket.receive", "text": json.dumps({"type": "end_call"})}
        )

    yield SimpleNamespace(base=r, state=state, voice=voice, start=start, end=end)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await close_stt()
    await voice._exa.close()
    await r.admission.release("call")


async def test_real_session_finalizes_connected_silence_and_repeated_end_once(voice_rig):
    import asyncio

    r = voice_rig
    task = await r.start()
    r.state.now = 105.25
    r.base.clock.now += timedelta(seconds=5.25)
    await r.end()
    await asyncio.wait_for(task, 0.5)
    await r.voice.lifecycle.finish("duplicate_timeout")
    assert await r.base.wallet.balance("owner") == 480
    assert await r.base.admission.active_count() == 0
    row = await session_row(r.base)
    assert row["billed_seconds"] == 6 and row["cost_paise"] == 20
    assert row["ended_at"] is not None and r.state.close_count == 1
    assert sum(e["type"] == "billing" and e.get("final") for e in r.state.events) == 1


@pytest.mark.parametrize("kind", ["ordinary", "budget", "cleanup"])
async def test_failed_call_carries_safe_error_before_client_retirement(
    voice_rig, monkeypatch, kind
):
    import asyncio

    from app.costs.models import BudgetExceeded
    from app.voice.lifecycle import SessionCleanupError

    r = voice_rig
    fail = asyncio.Event()
    error = {"ordinary": RuntimeError, "budget": BudgetExceeded, "cleanup": SessionCleanupError}[
        kind
    ]

    async def browser_failure():
        await fail.wait()
        raise error("Internal details must not be exposed")

    monkeypatch.setattr(r.voice, "_browser_loop", browser_failure)
    task = await r.start()
    fail.set()
    with pytest.raises(error):
        await asyncio.wait_for(task, 0.5)
    terminal = next(event for event in r.state.events if event["type"] == "call_ended")
    expected = {
        "ordinary": "Voice session failed",
        "budget": "Call stopped: the AI spending allowance is unavailable.",
        "cleanup": "Call cleanup could not finish safely.",
    }[kind]
    assert terminal["message"] == expected
    billing = next(
        event for event in r.state.events if event["type"] == "billing" and event["final"]
    )
    assert r.state.events.index(billing) < r.state.events.index(terminal)
    assert await r.base.admission.active_count() == (1 if kind == "cleanup" else 0)


async def test_real_failed_ready_never_bills_and_releases_lease(voice_rig):
    r = voice_rig
    r.state.fail_ready = True
    with pytest.raises(RuntimeError, match="readiness"):
        await r.voice.run()
    assert await r.base.wallet.balance("owner") == 500
    assert await r.base.admission.active_count() == 0
    row = await session_row(r.base)
    assert row["ready_at"] is None and row["cost_paise"] == 0
    assert row["ended_at"] is not None


async def test_real_initialization_stall_uses_cleanup_without_billing(voice_rig):
    from app.voice.session import VoiceSessionError

    r = voice_rig
    r.state.hang_connect = True
    with pytest.raises(VoiceSessionError, match="initialization"):
        await r.voice.run()
    assert not r.state.ready.is_set()
    assert await r.base.wallet.balance("owner") == 500
    assert await r.base.admission.active_count() == 0
    assert (await session_row(r.base))["ended_at"] is not None


async def test_provider_cleanup_error_does_not_skip_wallet_or_usage_finalization(
    voice_rig, monkeypatch
):
    import asyncio

    from app.voice.lifecycle import SessionCleanupError

    r = voice_rig
    task = await r.start()
    r.state.now = 102.0
    r.base.clock.now += timedelta(seconds=2)

    async def broken_close():
        raise RuntimeError("provider close failed")

    monkeypatch.setattr(r.voice._stt, "close", broken_close)
    await r.end()
    with pytest.raises(SessionCleanupError):
        await asyncio.wait_for(task, 0.5)
    assert await r.base.wallet.balance("owner") == 493
    assert (await session_row(r.base))["ended_at"] is not None
    assert await r.base.admission.active_count() == 1


async def test_successful_readiness_persists_anchor_before_connected_billing(
    recovery_rig, settings, monkeypatch
):
    import asyncio

    from pydantic import SecretStr

    from app.voice import session as implementation
    from app.voice.protocol import ProbeOptions

    r = recovery_rig
    async with r.database.engine.begin() as conn:
        await conn.execute(text("DELETE FROM usage_sessions WHERE id='call'"))

    class Speech:
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            pass

        async def close(self):
            pass

        async def messages(self):
            await asyncio.Event().wait()
            if False:
                yield {}

    lease = await r.admission.admit("owner", "call")

    class Socket:
        state = SimpleNamespace(
            cost_service=r.costs,
            call_admission=r.admission,
            admission_lease=lease,
            voice_grant=SimpleNamespace(session_id="call"),
        )

        async def send_json(self, message):
            pass

        async def receive(self):
            return {"type": "websocket.disconnect"}

    legacy = implementation.RECOGNITION["sarvam_legacy"]
    monkeypatch.setitem(implementation.RECOGNITION, "sarvam_legacy", (Speech, legacy[1]))
    monkeypatch.setattr(implementation, "ExaSearchClient", Speech)
    monkeypatch.setattr(implementation, "AgentRunner", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        implementation.VoiceSession, "_build_gate", lambda self: SimpleNamespace(vad_name="fixture")
    )
    settings.sarvam_api_key = SecretStr("unused-fixture")
    voice = implementation.VoiceSession(
        websocket=Socket(),
        settings=settings,
        probe_options=ProbeOptions(),
        database=r.database,
        user_id="owner",
        wallet=r.wallet,
        starting_balance_paise=500,
        connected_at=100,
    )
    await voice.run()
    row = await session_row(r)
    assert row["ready_at"] is not None
    assert row["billed_seconds"] == row["cost_paise"] == 0


async def test_repeated_cancellation_during_real_cleanup_does_not_bill_teardown(
    voice_rig, monkeypatch
):
    import asyncio

    r = voice_rig
    task = await r.start()
    r.state.now = 102.0
    r.base.clock.now += timedelta(seconds=2)
    entered, release = asyncio.Event(), asyncio.Event()
    close = r.voice._stt.close

    async def delayed_close():
        entered.set()
        await release.wait()
        await close()

    monkeypatch.setattr(r.voice._stt, "close", delayed_close)
    await r.end()
    await asyncio.wait_for(entered.wait(), 0.2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    r.state.now = 1102.0
    r.base.clock.now += timedelta(seconds=1000)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.5)
    assert await r.base.wallet.balance("owner") == 493
    assert await r.base.admission.active_count() == 0
    row = await session_row(r.base)
    assert row["billed_seconds"] == 2
    assert datetime.fromisoformat(row["ended_at"]).replace(tzinfo=UTC) == datetime(
        2026, 10, 18, 0, 0, 2, tzinfo=UTC
    )


async def test_balance_exhaustion_uses_the_shared_finalizer(voice_rig):
    import asyncio

    r = voice_rig
    async with r.base.database.session_factory() as db:
        db.add(UsageSession(id="other", user_id="owner", started_at=r.base.clock.now))
        await db.commit()
    await r.base.wallet.bill_session(
        user_id="owner", usage_session_id="other", billable_seconds=1, target_cost_paise=497
    )
    r.voice._starting_balance_paise = 3
    task = await r.start()
    r.state.now = 101.0
    r.base.clock.now += timedelta(seconds=1)
    await asyncio.wait_for(task, 1.5)
    assert await r.base.wallet.balance("owner") == 0
    row = await session_row(r.base)
    assert row["cost_paise"] == 3 and row["end_reason"] == "balance_exhausted"
    assert await r.base.admission.active_count() == 0
    assert sum(e["type"] == "billing" and e.get("final") for e in r.state.events) == 1


@pytest.mark.parametrize("kind", ["budget", "rate"])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_budget_failure_uses_shared_finalization(voice_rig, monkeypatch, kind, wrapped):
    import asyncio

    from app.costs.models import BudgetExceeded

    r = voice_rig
    task = await r.start()
    error = BudgetExceeded if kind == "budget" else RateUnavailable

    async def exhausted(**kwargs):
        if wrapped:
            raise error("No allowance") from TimeoutError("Previous provider operation timed out")
        raise error("No allowance")

    monkeypatch.setattr(r.voice, "_process_turn", exhausted)
    await r.voice._response_queue.put((r.voice._new_timer("local_vad"), "hello"))
    with pytest.raises(error):
        await asyncio.wait_for(task, 0.5)
    assert (await session_row(r.base))["end_reason"] == "provider_budget_exhausted"
    assert await r.base.admission.active_count() == 0


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///file:shared?mode=memory&cache=shared&uri=true",
    ],
)
async def test_runtime_refuses_nonpersistent_or_uri_database(settings, url):
    app = create_app(settings.model_copy(update={"database_url": url}))
    with pytest.raises(RuntimeError, match="file-backed|URI"):
        async with app.router.lifespan_context(app):
            pytest.fail("A runtime must not reset durable budget counters on restart")


async def test_stalled_provider_close_keeps_lease_but_finishes_wallet(voice_rig, monkeypatch):
    import asyncio
    import time

    from app.voice.lifecycle import SessionCleanupError, SessionLifecycle

    r = voice_rig
    r.voice.lifecycle = SessionLifecycle(r.voice._finalize_session, timeout_seconds=2)
    task = await r.start()
    r.state.now = 102.0
    r.base.clock.now += timedelta(seconds=2)

    async def stuck_close():
        await asyncio.Event().wait()

    monkeypatch.setattr(r.voice._stt, "close", stuck_close)
    started = time.monotonic()
    await r.end()
    with pytest.raises(SessionCleanupError):
        await asyncio.wait_for(task, 1.8)
    assert time.monotonic() - started < 1.5
    assert await r.base.wallet.balance("owner") == 493
    assert (await session_row(r.base))["ended_at"] is not None
    assert await r.base.admission.active_count() == 1


async def test_initialization_deadline_also_bounds_ready_delivery(voice_rig, monkeypatch):
    import asyncio

    from app.voice.session import VoiceSessionError

    r = voice_rig
    send = r.voice._websocket.send_json

    async def stalled_ready(message):
        if message["type"] == "ready":
            await asyncio.Event().wait()
        await send(message)

    monkeypatch.setattr(r.voice._websocket, "send_json", stalled_ready)
    with pytest.raises(VoiceSessionError, match="initialization"):
        await asyncio.wait_for(r.voice.run(), 0.3)
    assert await r.base.wallet.balance("owner") == 500
    assert (await session_row(r.base))["ready_at"] is None
    assert await r.base.admission.active_count() == 0


async def test_two_failed_response_turns_end_real_session(voice_rig, monkeypatch):
    import asyncio

    from app.voice.session import VoiceSessionError
    from app.voice.tts import SarvamTtsError

    r = voice_rig

    async def fail(**kwargs):
        raise SarvamTtsError("synthetic provider failure")

    monkeypatch.setattr(r.voice, "_process_turn", fail)
    task = await r.start()
    for _ in range(2):
        timer = r.voice._new_timer("local_vad")
        await r.voice._response_queue.put((timer, "hello"))
        await asyncio.wait_for(r.voice._response_queue.join(), 0.2)
    with pytest.raises(VoiceSessionError, match="consecutive"):
        await asyncio.wait_for(task, 0.3)
    assert (await session_row(r.base))["end_reason"] == "provider_failures"
    assert await r.base.admission.active_count() == 0
    assert sum(e.get("code") == "tts_turn_failed" for e in r.state.events) == 2


@pytest.mark.parametrize(
    "mode", ["first_answer", "tts_progress", "turn_work", "blocked_audio_send"]
)
async def test_real_response_deadline_recovers_without_late_output(voice_rig, monkeypatch, mode):
    import asyncio
    import time

    from app.voice.tts import SarvamTtsClient

    r = voice_rig
    stage = "first_answer" if mode == "blocked_audio_send" else mode
    origin = time.monotonic()
    r.base.clock.monotonic = lambda: r.state.now + time.monotonic() - origin
    r.voice._policy = RuntimePolicy(**{f"{stage}_seconds": 0.03})
    prices = r.base.costs._rates.unit_prices
    r.base.costs._rates = r.base.costs._rates.model_copy(
        update={
            "unit_prices": {
                "sarvam": {
                    **prices["sarvam"],
                    "bulbul:v4-flash": {"characters": UnitPrice(amount_micros=1_000, per_units=1)},
                }
            }
        }
    )

    async def reply(*args, **kwargs):
        if mode == "first_answer":
            await asyncio.Event().wait()
        yield "Hello, how can I help you today?"

    async def stream(self, chunks, **kwargs):
        async for _chunk in chunks:
            if mode == "tts_progress":
                await asyncio.Event().wait()
            while True:
                yield b"\0\0" * 100
                await asyncio.sleep(0.005)

    monkeypatch.setattr(r.voice._agent, "stream_reply", reply, raising=False)
    monkeypatch.setattr(SarvamTtsClient, "_stream", stream)
    retained_streams = []
    synthesize = SarvamTtsClient.synthesize

    def retain_stream(self, *args, **kwargs):
        result = synthesize(self, *args, **kwargs)
        retained_streams.append(result)
        return result

    async def send_audio(audio):
        if mode == "blocked_audio_send":
            await asyncio.Event().wait()

    monkeypatch.setattr(SarvamTtsClient, "synthesize", retain_stream)
    monkeypatch.setattr(r.voice._websocket, "send_bytes", send_audio, raising=False)
    task = await r.start()
    timer = r.voice._new_timer("local_vad")
    await r.voice._response_queue.put((timer, "hello"))
    await asyncio.wait_for(r.voice._response_queue.join(), 0.5)
    errors = [e for e in r.state.events if e.get("code") == "turn_deadline_exceeded"]
    assert len(errors) == 1 and errors[0]["stage"] == stage
    assert not task.done() and timer.dimensions["response_error"] == stage
    event_count = len(r.state.events)
    await asyncio.sleep(0.03)
    assert len(r.state.events) == event_count
    await r.end()
    await asyncio.wait_for(task, 0.5)
    assert await r.base.admission.active_count() == 0
    if mode != "first_answer":
        async with r.base.database.engine.connect() as conn:
            state = (
                await conn.execute(
                    text("SELECT state FROM cost_attempts WHERE model='bulbul:v4-flash'")
                )
            ).scalar_one()
        for retained in retained_streams:
            await retained.aclose()
        assert state == "unknown"


@pytest.mark.parametrize("middle", ["success", "interruption"])
async def test_only_a_successful_response_resets_failure_streak(voice_rig, monkeypatch, middle):
    import asyncio

    from app.voice.session import VoiceSessionError
    from app.voice.tts import SarvamTtsError

    r = voice_rig

    async def response(*, transcript, **kwargs):
        if transcript == "fail":
            raise SarvamTtsError("synthetic rejection")
        if transcript == "interruption":
            raise asyncio.CancelledError

    monkeypatch.setattr(r.voice, "_process_turn", response)
    task = await r.start()
    for utterance in ["fail", middle, "fail"]:
        await r.voice._response_queue.put((r.voice._new_timer("local_vad"), utterance))
        await asyncio.wait_for(r.voice._response_queue.join(), 0.3)
    if middle == "interruption":
        with pytest.raises(VoiceSessionError, match="consecutive"):
            await asyncio.wait_for(task, 0.5)
    else:
        assert r.voice._consecutive_provider_failures == 1 and not task.done()
        await r.end()
        await asyncio.wait_for(task, 0.5)


@pytest.mark.parametrize("strategy", ["local_vad", "sarvam"])
async def test_endpoint_finalization_timeout_recovers_once_then_ends(
    voice_rig, monkeypatch, strategy
):
    import asyncio
    import time

    from app.voice.gate import VadFrame, VadGateDecision, VadGateStats
    from app.voice.lifecycle import TurnDeadline
    from app.voice.session import VoiceSessionError

    r = voice_rig
    r.voice._endpointing_strategy = strategy
    r.voice._stt_deadline = TurnDeadline(RuntimePolicy(stt_final_seconds=0.02), r.base.clock)

    async def flush():
        return {"type": "flush"}

    monkeypatch.setattr(r.voice._stt, "flush", flush)
    task = await r.start()
    r.voice._gate.reset = lambda: None
    for count in [1, 2]:
        timer = r.voice._new_timer(strategy)
        if strategy == "local_vad":
            r.voice._active_local_timer = timer
            r.voice._stt._heard = True  # Saaras signalled speech, then stalled.
            await r.voice._apply_gate_decision(
                VadGateDecision(
                    endpoint_fired=True,
                    last_speech_frame=VadFrame(1, 32, time.monotonic(), b""),
                    completed_stats=VadGateStats(1, 1, 0, 0),
                )
            )
        else:
            r.voice._active_sarvam_timer = timer
            await r.voice._handle_stt_event("speech_end", time.monotonic())
        async with asyncio.timeout(0.5):
            while True:
                r.state.changed.clear()
                if sum(e.get("code") == "turn_deadline_exceeded" for e in r.state.events) >= count:
                    break
                await r.state.changed.wait()
            if count == 1:
                await r.state.closed.wait()
        if count == 1:
            assert not task.done()
    with pytest.raises(VoiceSessionError, match="consecutive"):
        await asyncio.wait_for(task, 0.5)
    assert (await session_row(r.base))["end_reason"] == "provider_failures"
    assert await r.base.admission.active_count() == 0


async def test_endpoint_saaras_never_heard_neither_flushes_nor_strikes(voice_rig, monkeypatch):
    import asyncio
    import time

    from app.voice.gate import VadFrame, VadGateDecision, VadGateStats
    from app.voice.lifecycle import TurnDeadline

    r = voice_rig
    r.voice._endpointing_strategy = "local_vad"
    r.voice._stt_deadline = TurnDeadline(RuntimePolicy(stt_final_seconds=0.02), r.base.clock)
    flushes = []

    async def flush():
        flushes.append(True)
        return {"type": "flush"}

    monkeypatch.setattr(r.voice._stt, "flush", flush)
    task = await r.start()
    for _ in range(3):  # Three short "Hi"s Saaras never detected used to end the call.
        r.voice._active_local_timer = r.voice._new_timer("local_vad")
        await r.voice._apply_gate_decision(
            VadGateDecision(
                endpoint_fired=True,
                last_speech_frame=VadFrame(1, 32, time.monotonic(), b""),
                completed_stats=VadGateStats(1, 1, 0, 0),
            )
        )
    await asyncio.sleep(0.1)
    assert flushes == [] and not r.voice._pending_local_timers
    assert not any(e.get("code") == "turn_deadline_exceeded" for e in r.state.events)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_finished_provider_work_can_wait_for_browser_playback(voice_rig, monkeypatch):
    import asyncio

    from app.voice.tts import SarvamTtsClient

    r = voice_rig
    r.voice._policy = RuntimePolicy(turn_work_seconds=0.02)
    prices = r.base.costs._rates.unit_prices
    r.base.costs._rates = r.base.costs._rates.model_copy(
        update={
            "unit_prices": {
                "sarvam": {
                    **prices["sarvam"],
                    "bulbul:v4-flash": {"characters": UnitPrice(amount_micros=1_000, per_units=1)},
                }
            }
        }
    )

    async def reply(*args, **kwargs):
        yield "Hello."

    async def stream(self, chunks, **kwargs):
        async for _ in chunks:
            yield b"\0\0" * 100
        self._completed = True

    async def send_audio(audio):
        pass

    monkeypatch.setattr(r.voice._agent, "stream_reply", reply, raising=False)
    monkeypatch.setattr(SarvamTtsClient, "_stream", stream)
    monkeypatch.setattr(r.voice._websocket, "send_bytes", send_audio, raising=False)
    task = await r.start()
    timer = r.voice._new_timer("local_vad")
    await r.voice._response_queue.put((timer, "hello"))
    await asyncio.sleep(0.06)
    assert r.voice._active_response_task is not None and not r.voice._active_response_task.done()
    r.voice._playback_started_events[timer.turn_id].set()
    await asyncio.wait_for(r.voice._response_queue.join(), 0.3)
    assert any(e["type"] == "turn_complete" for e in r.state.events)
    assert not any(e.get("code") == "turn_deadline_exceeded" for e in r.state.events)
    await r.end()
    await asyncio.wait_for(task, 0.5)


async def test_crash_loses_only_the_uncommitted_tick_tail(recovery_rig):
    from app.voice.lifecycle import recover_abandoned_sessions

    r = recovery_rig
    await ready_and_tick(r)
    r.clock.now += timedelta(seconds=5.75)
    balance = await r.wallet.balance("owner")
    await recover_abandoned_sessions(r.database, r.clock, r.costs)
    row = await session_row(r)
    ended = datetime.fromisoformat(row["ended_at"]).replace(tzinfo=UTC)
    assert (r.clock.now - ended).total_seconds() == 0.75
    assert row["billed_seconds"] == 5 and await r.wallet.balance("owner") == balance


async def test_terminal_state_blocks_waiting_text_and_audio(voice_rig):
    import asyncio

    r = voice_rig
    timer = r.voice._new_timer("local_vad")
    await r.voice._send_lock.acquire()
    text_task = asyncio.create_task(r.voice._send_json({"type": "agent_text", "text": "late"}))
    audio_task = asyncio.create_task(
        r.voice._send_turn_audio(timer.turn_id, b"late", generation_id="retired")
    )
    await asyncio.sleep(0)
    r.voice._closing = True
    r.voice._send_lock.release()
    await text_task
    assert await audio_task is False
    assert not r.state.events


async def test_cancellation_resistant_turn_keeps_its_admission_lease(voice_rig, monkeypatch):
    import asyncio

    from app.voice.lifecycle import SessionCleanupError, SessionLifecycle

    r = voice_rig
    r.voice._policy = RuntimePolicy(first_answer_seconds=0.01)
    r.voice.lifecycle = SessionLifecycle(r.voice._finalize_session, timeout_seconds=3)
    release = asyncio.Event()
    stopped = asyncio.Event()

    async def stubborn(**kwargs):
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        finally:
            stopped.set()

    monkeypatch.setattr(r.voice, "_process_turn", stubborn)
    task = await r.start()
    await r.voice._response_queue.put((r.voice._new_timer("local_vad"), "hello"))
    try:
        with pytest.raises(SessionCleanupError):
            await asyncio.wait_for(task, 3)
        assert await r.base.admission.active_count() == 1
    finally:
        release.set()
        await asyncio.wait_for(stopped.wait(), 0.3)


@pytest.mark.parametrize("mode", ["stall", "error"])
async def test_post_ready_initialization_failure_does_not_activate_billing(
    voice_rig, monkeypatch, mode
):
    import asyncio

    from app.voice.session import VoiceSessionError

    r = voice_rig
    send = r.voice._websocket.send_json

    async def stall(message):
        if message["type"] == "billing":
            if mode == "error":
                raise RuntimeError("synthetic initialization delivery failure")
            await asyncio.Event().wait()
        await send(message)

    monkeypatch.setattr(r.voice._websocket, "send_json", stall)
    with pytest.raises((VoiceSessionError, RuntimeError), match="initialization"):
        await r.voice.run()
    assert r.state.ready.is_set()
    assert await r.base.wallet.balance("owner") == 500
    assert (await session_row(r.base))["cost_paise"] == 0


async def test_balance_cutoff_precedes_stalled_browser_notification(voice_rig, monkeypatch):
    import asyncio

    from app.voice.lifecycle import SessionLifecycle

    r = voice_rig
    r.voice.lifecycle = SessionLifecycle(r.voice._finalize_session, timeout_seconds=1)
    async with r.base.database.session_factory() as db:
        db.add(UsageSession(id="other", user_id="owner", started_at=r.base.clock.now))
        await db.commit()
    await r.base.wallet.bill_session(
        user_id="owner", usage_session_id="other", billable_seconds=1, target_cost_paise=497
    )
    r.voice._starting_balance_paise = 3
    task = await r.start()
    send = r.voice._websocket.send_json

    async def stall(message):
        if message["type"] == "billing":
            await asyncio.Event().wait()
        await send(message)

    monkeypatch.setattr(r.voice._websocket, "send_json", stall)
    r.state.now = 101
    r.base.clock.now += timedelta(seconds=1)
    await asyncio.wait_for(task, 1.6)
    assert await r.base.wallet.balance("owner") == 0
    assert (await session_row(r.base))["end_reason"] == "balance_exhausted"
    assert await r.base.admission.active_count() == 0


async def test_endpoint_deadline_is_monitored_during_stt_reconnect(voice_rig, monkeypatch):
    import asyncio
    import time
    from unittest.mock import AsyncMock

    from app.voice.gate import VadFrame, VadGateDecision, VadGateStats
    from app.voice.lifecycle import TurnDeadline

    r = voice_rig
    r.voice._stt_deadline = TurnDeadline(RuntimePolicy(stt_final_seconds=0.02), r.base.clock)
    rotating = asyncio.Event()
    connects = 0
    timer = r.voice._new_timer("local_vad")
    real_reconnect = r.voice._stt.reconnect
    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        if reads == 1:
            return timer
        await asyncio.Event().wait()

    async def rotate():
        nonlocal connects
        connects += 1
        if connects == 1:
            rotating.set()
            await asyncio.Event().wait()
        await real_reconnect()

    monkeypatch.setattr(r.voice, "_receive_stt_final", receive)
    monkeypatch.setattr(r.voice._stt, "reconnect", rotate)
    task = await r.start()
    await asyncio.wait_for(rotating.wait(), 0.2)
    r.voice._active_local_timer = r.voice._new_timer("local_vad")
    r.voice._stt._heard = True  # Saaras signalled this utterance before the socket rotated.
    flush = AsyncMock(
        side_effect=AssertionError("A timed-out utterance must not flush a new socket")
    )
    monkeypatch.setattr(r.voice._stt, "flush", flush)
    endpoint = asyncio.create_task(
        r.voice._apply_gate_decision(
            VadGateDecision(
                endpoint_fired=True,
                last_speech_frame=VadFrame(1, 32, time.monotonic(), b""),
                completed_stats=VadGateStats(1, 1, 0, 0),
            )
        )
    )
    async with asyncio.timeout(0.2):
        while True:
            r.state.changed.clear()
            if any(e.get("stage") == "stt_final" for e in r.state.events):
                break
            await r.state.changed.wait()
    assert not task.done()
    await asyncio.wait_for(endpoint, 0.3)
    flush.assert_not_awaited()
    await r.end()
    await asyncio.wait_for(task, 0.5)


async def test_incomplete_turn_cleanup_cannot_release_a_lease(voice_rig, monkeypatch):
    import asyncio

    from app.voice.lifecycle import SessionCleanupError

    r = voice_rig

    async def unsafe(**kwargs):
        raise SessionCleanupError("Provider close could not be confirmed")

    monkeypatch.setattr(r.voice, "_process_turn", unsafe)
    task = await r.start()
    await r.voice._response_queue.put((r.voice._new_timer("local_vad"), "hello"))
    with pytest.raises(SessionCleanupError):
        await asyncio.wait_for(task, 0.5)
    assert (await session_row(r.base))["ended_at"] is not None
    assert await r.base.admission.active_count() == 1


async def test_expired_initialization_cannot_activate_billing_at_its_return(voice_rig, monkeypatch):
    from app.voice.session import VoiceSessionError

    r = voice_rig
    mark_ready = r.base.wallet.mark_ready

    async def delayed_persistence(**kwargs):
        await mark_ready(**kwargs)
        r.state.now += 1
        r.base.clock.now += timedelta(seconds=1)

        def advancing():
            r.state.now += 0.01
            return r.state.now

        r.base.clock.monotonic = advancing

    monkeypatch.setattr(r.base.wallet, "mark_ready", delayed_persistence)
    with pytest.raises(VoiceSessionError, match="initialization"):
        await r.voice.run()
    assert await r.base.wallet.balance("owner") == 500
    assert (await session_row(r.base))["cost_paise"] == 0


@pytest.mark.parametrize("error_kind", ["ordinary", "budget", "expiry"])
async def test_route_finalizes_even_if_terminal_browser_error_send_stalls(
    voice_rig, settings, monkeypatch, error_kind
):
    import asyncio
    from unittest.mock import AsyncMock

    from starlette.websockets import WebSocketState

    from app.auth.tickets import TicketService
    from app.costs.models import BudgetExceeded
    from app.voice.lifecycle import SessionLifecycle
    from app.voice.router import voice_socket

    r = voice_rig
    socket = r.voice._websocket
    socket.headers = {"origin": settings.cors_origin_list[0]}
    socket.query_params = {"ticket": "synthetic"}
    socket.application_state = WebSocketState.CONNECTING

    async def accept():
        socket.application_state = WebSocketState.CONNECTED

    async def close(**kwargs):
        socket.application_state = WebSocketState.DISCONNECTED

    async def stall(message):
        await asyncio.Event().wait()

    async def finalize(reason):
        await r.base.admission.release("call")

    error = {"ordinary": RuntimeError, "budget": BudgetExceeded, "expiry": TimeoutError}[error_kind]
    session = SimpleNamespace(
        run=AsyncMock(side_effect=error("synthetic failure")), lifecycle=SessionLifecycle(finalize)
    )
    socket.app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            database=r.base.database,
            clock=r.base.clock,
            runtime_policy=r.base.policy,
            wallet_service=r.base.wallet,
            cost_service=r.base.costs,
            call_admission=r.base.admission,
            voice_session_factory=lambda **kwargs: session,
        )
    )
    grant = SimpleNamespace(
        user_id="owner", session_id="call", access_expires_at=r.voice._lease.expires_at
    )
    monkeypatch.setattr(TicketService, "consume", AsyncMock(return_value=grant))
    monkeypatch.setattr(socket, "accept", accept, raising=False)
    monkeypatch.setattr(socket, "close", close)
    monkeypatch.setattr(socket, "send_json", stall)
    await asyncio.wait_for(voice_socket(socket), 1.5)
    assert await r.base.admission.active_count() == 0
    assert socket.application_state is WebSocketState.DISCONNECTED


async def test_agent_turn_limit_fails_only_that_turn(voice_rig, monkeypatch):
    import asyncio

    from app.voice.agent import AgentLimitError

    r = voice_rig

    async def too_long(**kwargs):
        raise AgentLimitError("Response character limit reached")

    monkeypatch.setattr(r.voice, "_process_turn", too_long)
    task = await r.start()
    for _ in range(3):
        timer = r.voice._new_timer("local_vad")
        await r.voice._response_queue.put((timer, "explain at length"))
        await asyncio.wait_for(r.voice._response_queue.join(), 0.2)
    assert not task.done()  # Not counted as a provider failure either.
    assert sum(e.get("code") == "turn_limit_reached" for e in r.state.events) == 3
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_persists_of_one_turn_keep_the_call_alive(voice_rig, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    from sqlalchemy import text

    r = voice_rig
    task = await r.start()
    timer = r.voice._new_timer("local_vad")
    factory = r.voice._database.session_factory

    @asynccontextmanager
    async def slow_lookup_session():
        async with factory() as session:
            execute = session.execute

            async def execute_then_yield(*args, **kwargs):
                result = await execute(*args, **kwargs)
                await asyncio.sleep(0.01)  # Let the other writer reach its own lookup.
                return result

            session.execute = execute_then_yield
            yield session

    monkeypatch.setattr(r.voice._database, "session_factory", slow_lookup_session)
    # Deadline recovery and the interruption ack both persist the same turn.
    await asyncio.gather(r.voice._persist_timer(timer), r.voice._persist_timer(timer))
    async with r.base.database.engine.connect() as conn:
        rows = (await conn.execute(text("SELECT count(*) FROM turn_metrics"))).scalar_one()
    assert rows == 1 and not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
