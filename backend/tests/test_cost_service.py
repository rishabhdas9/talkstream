from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.costs.models import BudgetExceeded, CostScope, Usage, WorkBound
from app.costs.rates import (
    PriceAdjustment,
    RateCard,
    RateUnavailable,
    UnitPrice,
    estimate_micro_inr,
)
from app.costs.service import CostService
from app.runtime import RuntimePolicy


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 31, 18, 29, 59, tzinfo=UTC)

    def utc_now(self):
        return self.now

    def monotonic(self):
        return 1.0


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def rates(clock):
    return RateCard(
        version="fixture-v1",
        captured_at=clock.now,
        source="https://example.com/fixture",
        currency="INR",
        fx_micro_inr=1_000_000,
        qualified=True,
        valid_until=clock.now + timedelta(days=90),
        taxes_and_fees=PriceAdjustment(
            numerator=1, denominator=1, note="Synthetic tax-inclusive rate"
        ),
        unit_prices={"fake": {"model": {"work": UnitPrice(amount_micros=1_000_000, per_units=1)}}},
    )


@pytest.fixture
def scope():
    return CostScope(user_id="user-1", session_id=None, job_id="job-1")


@pytest.fixture
def bound():
    return WorkBound(units={"work": 1})


@pytest.fixture
def costs(database, clock, rates):
    return CostService(
        database,
        clock,
        RuntimePolicy(session_micro_inr=5_000_000, user_day_micro_inr=10_000_000),
        rates,
    )


async def test_unknown_usage_keeps_reservation(costs, scope, bound):
    held = await costs.reserve(scope, "attempt-1", "fake", "model", bound)
    await costs.settle("attempt-1", None, "unknown")
    assert await costs.outstanding_micro_inr(scope) == held.maximum_micro_inr == 1_000_000


async def test_settle_is_idempotent(costs, scope, bound):
    await costs.reserve(scope, "attempt-1", "fake", "model", bound)
    usage = Usage(units={"work": 1}, provider_request_id="request-1")
    await costs.settle("attempt-1", usage, "succeeded")
    before = await costs.spent_micro_inr()
    await costs.settle("attempt-1", usage, "succeeded")
    await costs.settle("attempt-1", None, "unknown")
    assert await costs.spent_micro_inr() == before == 1_000_000
    assert await costs.outstanding_micro_inr(scope) == 0
    with pytest.raises(ValueError):
        await costs.settle(
            "attempt-1", Usage(units={"work": 0}, provider_request_id="request-1"), "succeeded"
        )


async def test_duplicate_reserve_and_mismatched_arguments(costs, scope, bound):
    held = await costs.reserve(scope, "same", "fake", "model", bound)
    duplicates = await asyncio.gather(
        *(costs.reserve(scope, "same", "fake", "model", bound) for _ in range(8))
    )
    assert all(item == held for item in duplicates)
    assert await costs.outstanding_micro_inr(scope) == 1_000_000
    for changed_scope, changed_bound in [
        (scope, WorkBound(units={"work": 2})),
        (CostScope(user_id="user-2", job_id="job-1"), bound),
        (CostScope(user_id="user-1", session_id="session-1"), bound),
    ]:
        with pytest.raises(ValueError):
            await costs.reserve(changed_scope, "same", "fake", "model", changed_bound)


async def test_concurrent_user_reservations_never_exceed_cap(costs, scope, bound):
    results = await asyncio.gather(
        *(costs.reserve(scope, f"a-{i}", "fake", "model", bound) for i in range(20)),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in results) == 10
    assert all(
        not isinstance(item, Exception) or isinstance(item, BudgetExceeded) for item in results
    )
    assert await costs.outstanding_micro_inr(scope) == 10_000_000


async def test_concurrent_global_reservations_never_exceed_cap(costs, bound, database):
    results = await asyncio.gather(
        *(
            costs.reserve(
                CostScope(user_id=f"user-{i}", job_id=f"job-{i}"), f"a-{i}", "fake", "model", bound
            )
            for i in range(40)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in results) == 30
    assert all(
        not isinstance(item, Exception) or isinstance(item, BudgetExceeded) for item in results
    )
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(
                text(
                    "SELECT SUM(held_micro_inr+spent_micro_inr) FROM cost_periods"
                    " WHERE kind='global_day'"
                )
            )
        ).scalar_one() == 30_000_000


async def test_envelope_does_not_double_count_and_closes_only_unused_capacity(costs, database):
    call = CostScope(user_id="caller", session_id="session-1")
    envelope = await costs.reserve_envelope(call, "envelope-1")
    assert envelope.maximum_micro_inr == 5_000_000
    await costs.reserve(call, "call-attempt", "fake", "model", WorkBound(units={"work": 3}))
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT SUM(held_micro_inr) FROM cost_periods WHERE kind='global_day'")
            )
        ).scalar_one() == 5_000_000
    await costs.settle(
        "call-attempt", Usage(units={"work": 1}, provider_request_id=None), "succeeded"
    )
    await costs.reserve(call, "uncertain", "fake", "model", WorkBound(units={"work": 2}))
    await costs.settle("uncertain", None, "cancelled")
    await costs.release_envelope("envelope-1")
    await costs.release_envelope("envelope-1")
    # The cancelled attempt's unknown usage is charged at its full bound, not held.
    assert await costs.outstanding_micro_inr(call) == 0
    assert await costs.spent_micro_inr() == 3_000_000
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT SUM(held_micro_inr) FROM cost_periods WHERE kind='global_day'")
            )
        ).scalar_one() == 0
    # Later evidence cannot refund what was already charged.
    await costs.settle("uncertain", Usage(units={"work": 0}, provider_request_id=None), "failed")
    assert await costs.spent_micro_inr() == 3_000_000
    with pytest.raises(BudgetExceeded):
        await costs.reserve(call, "after-close", "fake", "model", WorkBound(units={"work": 1}))


async def test_call_attempt_needs_envelope_and_cannot_exceed_it(costs):
    call = CostScope(user_id="caller", session_id="session-1")
    with pytest.raises(BudgetExceeded):
        await costs.reserve(call, "no-envelope", "fake", "model", WorkBound(units={"work": 1}))
    await costs.reserve_envelope(call, "envelope-1", maximum_micro_inr=2_000_000)
    with pytest.raises(BudgetExceeded):
        await costs.reserve(call, "too-big", "fake", "model", WorkBound(units={"work": 3}))
    await costs.reserve(call, "fits", "fake", "model", WorkBound(units={"work": 2}))
    with pytest.raises(BudgetExceeded):
        await costs.reserve(call, "another", "fake", "model", WorkBound(units={"work": 1}))
    with pytest.raises(ValueError):
        await costs.reserve_envelope(
            CostScope(user_id="other", session_id="session-1"), "different"
        )


async def test_unknown_usage_counts_as_spent_until_the_day_rolls_over(
    costs, database, clock, rates, scope
):
    await costs.reserve(scope, "old", "fake", "model", WorkBound(units={"work": 10}))
    await costs.settle("old", None, "unknown")
    with pytest.raises(BudgetExceeded):
        await costs.reserve(scope, "same-day", "fake", "model", WorkBound(units={"work": 1}))
    clock.now += timedelta(seconds=1)  # Kolkata day and month rollover.
    from app.db import Database

    restarted = Database(str(database.engine.url))
    try:
        service = CostService(restarted, clock, RuntimePolicy(), rates)
        await service.reserve(scope, "new", "fake", "model", WorkBound(units={"work": 1}))
        async with restarted.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT spent_micro_inr,held_micro_inr FROM cost_periods"
                        " WHERE kind='user_day' AND period=:day"
                    ),
                    {"day": "2026-10-31"},
                )
            ).one()
        assert tuple(row) == (10_000_000, 0)
    finally:
        await restarted.dispose()


async def test_local_mode_skips_only_the_user_day_cap(costs, scope):
    costs.user_day_limit = False
    for i in range(3):
        await costs.reserve(scope, f"a-{i}", "fake", "model", WorkBound(units={"work": 10}))
    with pytest.raises(BudgetExceeded):  # The global day cap still applies.
        await costs.reserve(scope, "a-3", "fake", "model", WorkBound(units={"work": 1}))


async def test_month_cap_uses_persistent_counters(costs, database, scope, bound):
    async with database.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO cost_periods VALUES ('global_month','*','2026-10',999500000,0)")
        )
    with pytest.raises(BudgetExceeded):
        await costs.reserve(scope, "monthly", "fake", "model", bound)


async def test_settlement_uses_reserved_rate_not_changed_current_rate(
    costs, database, clock, rates, scope, bound
):
    await costs.reserve(scope, "original", "fake", "model", bound)
    changed = rates.model_copy(
        update={
            "unit_prices": {
                "fake": {"model": {"work": UnitPrice(amount_micros=9_000_000, per_units=1)}}
            }
        }
    )
    current = CostService(database, clock, RuntimePolicy(), changed)
    clock.now += timedelta(days=100)
    await current.settle(
        "original", Usage(units={"work": 1}, provider_request_id=None), "succeeded"
    )
    assert await current.spent_micro_inr() == 1_000_000
    with pytest.raises(RateUnavailable):
        await current.reserve(scope, "new", "fake", "model", bound)


@pytest.mark.parametrize("units", [{"missing": 1}, {}, {"work": 2}])
async def test_invalid_or_over_bound_usage_never_releases_hold(costs, scope, bound, units):
    await costs.reserve(scope, "original", "fake", "model", bound)
    with pytest.raises(ValueError):
        await costs.settle("original", Usage(units=units, provider_request_id=None), "succeeded")
    assert await costs.outstanding_micro_inr(scope) == 1_000_000


def test_rates_use_exact_fx_checkout_fee_and_conservative_rounding(clock):
    card = RateCard(
        version="fixture-usd",
        captured_at=clock.now,
        source="https://example.com/fixture",
        currency="USD",
        fx_micro_inr=90_000_000,
        qualified=True,
        valid_until=clock.now + timedelta(days=1),
        taxes_and_fees=PriceAdjustment(
            numerator=580, denominator=500, note="$5 credited; $5.80 paid, synthetic quote"
        ),
        unit_prices={"fake": {"model": {"work": UnitPrice(amount_micros=1, per_units=3)}}},
    )
    assert estimate_micro_inr("fake", "model", WorkBound(units={"work": 1}), card) == 35
    assert estimate_micro_inr("fake", "model", WorkBound(units={"work": 3}), card) == 105
    assert estimate_micro_inr("fake", "model", WorkBound(units={"work": 0}), card) == 0


@pytest.mark.parametrize(
    "change",
    [{"qualified": False}, {"valid_until": datetime(2026, 1, 1, tzinfo=UTC)}, {"unit_prices": {}}],
)
async def test_missing_stale_unqualified_rates_refuse_new_work(
    database, clock, rates, scope, bound, change
):
    service = CostService(database, clock, RuntimePolicy(), rates.model_copy(update=change))
    with pytest.raises((RateUnavailable, ValueError)):
        await service.reserve(scope, "new", "fake", "model", bound)


@pytest.mark.parametrize(
    "data",
    [
        {"user_id": "u"},
        {"user_id": "u", "session_id": "s", "job_id": "j"},
        {"user_id": "", "job_id": "j"},
    ],
)
def test_scope_cannot_bypass_quotas(data):
    with pytest.raises(ValueError):
        CostScope(**data)


@pytest.mark.parametrize("units", [{"work": -1}, {"work": 1.5}, {"work": True}])
def test_work_units_are_nonnegative_integers(units):
    with pytest.raises(ValueError):
        WorkBound(units=units)


async def test_concurrent_settlement_and_release_are_consistent(costs, database):
    call = CostScope(user_id="caller", session_id="session")
    await costs.reserve_envelope(call, "envelope")
    await costs.reserve(call, "attempt", "fake", "model", WorkBound(units={"work": 3}))
    usage = Usage(units={"work": 2}, provider_request_id="provider-1")
    await asyncio.gather(
        costs.release_envelope("envelope"),
        *(costs.settle("attempt", usage, "succeeded") for _ in range(8)),
    )
    assert await costs.spent_micro_inr() == 2_000_000
    assert await costs.outstanding_micro_inr(call) == 0
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT SUM(held_micro_inr) FROM cost_periods"))
        ).scalar_one() == 0


async def test_write_failure_rolls_back_envelope_and_quota_changes(costs, database):
    from sqlalchemy.exc import IntegrityError

    call = CostScope(user_id="caller", session_id="session")
    await costs.reserve_envelope(call, "envelope")
    async with database.engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TRIGGER fail_attempt BEFORE INSERT ON cost_attempts "
            "BEGIN SELECT RAISE(ABORT,'injected disk/write failure'); END"
        )
    with pytest.raises(IntegrityError):
        await costs.reserve(call, "attempt", "fake", "model", WorkBound(units={"work": 3}))
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT held_micro_inr FROM cost_envelopes"))
        ).scalar_one() == 0
        assert (await conn.execute(text("SELECT COUNT(*) FROM cost_attempts"))).scalar_one() == 0
    assert await costs.outstanding_micro_inr(call) == 5_000_000
    await costs.release_envelope("envelope")
    assert await costs.outstanding_micro_inr(call) == 0


async def test_attempt_after_midnight_uses_current_spend_period_without_double_hold(
    costs, database, clock
):
    call = CostScope(user_id="caller", session_id="session")
    await costs.reserve_envelope(call, "envelope")
    clock.now += timedelta(seconds=1)
    await costs.reserve(call, "new-period", "fake", "model", WorkBound(units={"work": 2}))
    await costs.settle(
        "new-period", Usage(units={"work": 1}, provider_request_id=None), "succeeded"
    )
    async with database.engine.connect() as conn:
        rows = dict(
            (
                await conn.execute(
                    text("SELECT period,spent_micro_inr FROM cost_periods WHERE kind='global_day'")
                )
            ).all()
        )
        assert rows == {"2026-10-31": 0, "2026-11-01": 1_000_000}
    assert await costs.outstanding_micro_inr(call) == 4_000_000
    await costs.release_envelope("envelope")
    assert await costs.outstanding_micro_inr(call) == 0


async def test_rate_card_input_mutation_cannot_change_service_prices(costs, rates, scope, bound):
    rates.unit_prices["fake"]["model"]["work"] = UnitPrice(amount_micros=0, per_units=1)
    assert (
        await costs.reserve(scope, "attempt", "fake", "model", bound)
    ).maximum_micro_inr == 1_000_000


async def test_example_rates_are_deliberately_unqualified(database, clock, scope, bound):
    from pathlib import Path

    example = RateCard.model_validate_json(
        (Path(__file__).parents[2] / "config/rates/example.json").read_text()
    )
    assert example.qualified is False
    with pytest.raises(RateUnavailable):
        await CostService(database, clock, RuntimePolicy(), example).reserve(
            scope, "attempt", "fixture-provider", "fixture-model", bound
        )


async def test_tightened_policy_applies_to_an_existing_call_envelope(costs, database, clock, rates):
    call = CostScope(user_id="caller", session_id="session")
    await costs.reserve_envelope(call, "envelope")
    stricter = CostService(
        database,
        clock,
        RuntimePolicy(session_micro_inr=1_000_000, user_day_micro_inr=1_000_000),
        rates,
    )
    with pytest.raises(BudgetExceeded):
        await stricter.reserve(call, "too-large-now", "fake", "model", WorkBound(units={"work": 3}))


async def test_over_bound_usage_durably_freezes_new_admission(
    costs, database, clock, rates, scope, bound
):
    await costs.reserve(scope, "overrun", "fake", "model", bound)
    with pytest.raises(ValueError):
        await costs.settle(
            "overrun", Usage(units={"work": 11}, provider_request_id="bill-1"), "succeeded"
        )
    async with database.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT state,actual_micro_inr,usage_json FROM cost_attempts WHERE id='overrun'"
                )
            )
        ).one()
    assert row.state == "disputed"
    assert row.actual_micro_inr == 11_000_000 and "bill-1" in row.usage_json
    assert await costs.outstanding_micro_inr(scope) == 1_000_000
    from app.db import Database

    restarted = Database(str(database.engine.url))
    try:
        service = CostService(restarted, clock, RuntimePolicy(), rates)
        with pytest.raises(BudgetExceeded):
            await service.reserve(scope, "new", "fake", "model", WorkBound(units={"work": 9}))
        with pytest.raises(BudgetExceeded):
            await service.reserve_envelope(
                CostScope(user_id="another", session_id="another"), "envelope"
            )
        with pytest.raises(ValueError):
            await service.settle(
                "overrun", Usage(units={"work": 0}, provider_request_id="bill-1"), "failed"
            )
    finally:
        await restarted.dispose()


async def test_shared_transaction_rolls_back_lease_and_envelope_together(costs, database):
    async with database.engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE test_leases (id TEXT PRIMARY KEY)")
    call = CostScope(user_id="caller", session_id="session")
    with pytest.raises(RuntimeError, match="injected lease failure"):
        async with costs.transaction() as conn:
            await costs.reserve_envelope(call, "envelope", connection=conn)
            await conn.execute(text("INSERT INTO test_leases VALUES ('session')"))
            raise RuntimeError("injected lease failure")
    async with database.engine.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM test_leases"))).scalar_one() == 0
        assert (await conn.execute(text("SELECT COUNT(*) FROM cost_envelopes"))).scalar_one() == 0
        assert (await conn.execute(text("SELECT COUNT(*) FROM cost_periods"))).scalar_one() == 0


async def test_borrowed_transaction_must_be_owned_by_cost_service(costs, database):
    call = CostScope(user_id="caller", session_id="session")
    async with database.engine.begin() as conn:
        with pytest.raises(ValueError):
            await costs.reserve_envelope(call, "envelope", connection=conn)


async def test_shared_release_transaction_can_roll_back_atomically(costs):
    call = CostScope(user_id="caller", session_id="session")
    await costs.reserve_envelope(call, "envelope")
    with pytest.raises(RuntimeError, match="injected lease update failure"):
        async with costs.transaction() as conn:
            await costs.release_envelope("envelope", connection=conn)
            raise RuntimeError("injected lease update failure")
    assert await costs.outstanding_micro_inr(call) == 5_000_000


async def test_cancelled_caller_cannot_orphan_a_ledger_transaction(costs, database):
    import sqlite3

    from sqlalchemy import event

    caller = None

    def cancel_mid_transaction(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO cost_attempts") and caller is not None:
            caller.cancel()

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", cancel_mid_transaction)
    try:
        caller = asyncio.create_task(
            costs.reserve(
                CostScope(user_id="u", job_id="job"),
                "attempt",
                "fake",
                "model",
                WorkBound(units={"work": 1}),
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await caller
    finally:
        event.remove(engine, "before_cursor_execute", cancel_mid_transaction)
    probe = sqlite3.connect(database.engine.url.database, timeout=0)
    try:
        probe.execute("BEGIN IMMEDIATE")  # no connection may still hold the write lock
        probe.rollback()
    finally:
        probe.close()
    async with database.engine.connect() as conn:
        row = (
            (await conn.execute(text("SELECT state,outcome,actual_micro_inr FROM cost_attempts")))
            .mappings()
            .one()
        )
    assert dict(row) == {"state": "settled", "outcome": "cancelled", "actual_micro_inr": 0}
    assert await costs.outstanding_micro_inr(CostScope(user_id="u", job_id="job")) == 0
