from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.costs.models import BudgetExceeded, CostScope
from app.costs.rates import PriceAdjustment, RateCard, RateUnavailable, UnitPrice
from app.costs.service import CostService
from app.runtime import RuntimePolicy
from app.voice.admission import CallAdmission, CallLimitExceeded


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 31, 18, 29, 59, tzinfo=UTC)

    def utc_now(self):
        return self.now

    def monotonic(self):
        return 1.0


@pytest.fixture
def admission(database):
    clock = Clock()
    policy = RuntimePolicy(session_micro_inr=5_000_000, user_day_micro_inr=10_000_000)
    rates = RateCard(
        version="fixture-v1",
        captured_at=clock.now,
        valid_until=clock.now + timedelta(days=30),
        source="https://example.com/fixture",
        currency="INR",
        fx_micro_inr=1_000_000,
        qualified=True,
        taxes_and_fees=PriceAdjustment(numerator=1, denominator=1, note="Synthetic test only"),
        unit_prices={
            "sarvam": {
                "saaras:v3": {"audio_ms": UnitPrice(amount_micros=1_000_000, per_units=120_000)}
            }
        },
    )
    costs = CostService(database, clock, policy, rates)
    return CallAdmission(
        database, clock, policy, costs, stt_provider="sarvam", stt_model="saaras:v3"
    )


async def test_global_slots_precede_vendor_connect(admission):
    await admission.admit("a", "s1")
    await admission.admit("b", "s2")
    with pytest.raises(CallLimitExceeded):
        await admission.admit("c", "s3")
    assert await admission.active_count() == 2


async def test_repeated_release_is_safe(admission):
    await admission.admit("a", "s1")
    await admission.release("s1")
    await admission.release("s1")
    await admission.release("never-admitted")
    assert await admission.active_count() == 0
    assert await admission.costs.outstanding_micro_inr(CostScope(user_id="a", session_id="s1")) == 0


async def test_one_active_call_per_user_under_concurrency(admission):
    results = await asyncio.gather(
        *(admission.admit("user", f"s-{i}") for i in range(12)), return_exceptions=True
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert all(
        not isinstance(result, Exception) or isinstance(result, CallLimitExceeded)
        for result in results
    )
    assert await admission.active_count() == 1


async def test_two_global_calls_under_concurrency(admission):
    results = await asyncio.gather(
        *(admission.admit(f"user-{i}", f"s-{i}") for i in range(12)), return_exceptions=True
    )
    assert sum(not isinstance(result, Exception) for result in results) == 2
    assert all(
        not isinstance(result, Exception) or isinstance(result, CallLimitExceeded)
        for result in results
    )


async def test_three_successful_calls_per_kolkata_day(admission):
    for i in range(3):
        await admission.admit("user", f"s-{i}")
        await admission.release(f"s-{i}")
    with pytest.raises(CallLimitExceeded):
        await admission.admit("user", "fourth")
    admission.clock.now += timedelta(seconds=1)
    assert (await admission.admit("user", "next-day")).session_id == "next-day"


async def test_local_mode_skips_only_the_daily_call_count(admission):
    admission.daily_call_limit = False
    for i in range(5):
        await admission.admit("user", f"s-{i}")
        await admission.release(f"s-{i}")
    await admission.admit("user", "active")
    with pytest.raises(CallLimitExceeded):
        await admission.admit("user", "second-active")


async def test_duplicate_session_does_not_consume_another_call_or_extend_expiry(admission):
    original = await admission.admit("user", "session")
    assert await admission.admit("user", "session") == original
    with pytest.raises(ValueError):
        await admission.admit("other-user", "session")
    await admission.release("session")
    with pytest.raises(CallLimitExceeded):
        await admission.admit("user", "session")


async def test_expiry_is_bounded_by_access_deadline_but_does_not_silently_free_slots(admission):
    deadline = admission.clock.now + timedelta(seconds=10)
    lease = await admission.admit("user", "session", access_expires_at=deadline)
    assert lease.expires_at == deadline
    admission.clock.now = deadline
    with pytest.raises(CallLimitExceeded):
        await admission.admit("user", "new-session")
    assert await admission.active_count() == 1
    with pytest.raises(CallLimitExceeded):
        await admission.admit("other", "already-expired", access_expires_at=deadline)


async def test_restart_preserves_slots_and_daily_admissions(admission, database):
    from app.db import Database

    await admission.admit("user", "session")
    restarted = Database(str(database.engine.url))
    try:
        service = CallAdmission(
            restarted,
            admission.clock,
            admission.policy,
            CostService(restarted, admission.clock, admission.policy, admission.costs._rates),
            stt_provider="sarvam",
            stt_model="saaras:v3",
        )
        assert await service.active_count() == 1
        with pytest.raises(CallLimitExceeded):
            await service.admit("user", "new-session")
    finally:
        await restarted.dispose()


async def test_failed_admission_leaves_no_slot_or_envelope(admission, database):
    from sqlalchemy.exc import IntegrityError

    async with database.engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TRIGGER fail_lease BEFORE INSERT ON call_leases "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(IntegrityError):
        await admission.admit("user", "session")
    assert await admission.active_count() == 0
    async with database.engine.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM cost_envelopes"))).scalar_one() == 0
        assert (await conn.execute(text("SELECT COUNT(*) FROM cost_periods"))).scalar_one() == 0


@pytest.mark.parametrize("failure", ["too_expensive", "unqualified", "missing_metric"])
async def test_full_recognition_bound_must_fit_before_admission(admission, failure):
    rates = admission.costs._rates.model_copy(deep=True)
    if failure == "too_expensive":
        rates.unit_prices["sarvam"]["saaras:v3"]["audio_ms"] = UnitPrice(
            amount_micros=6_000_000, per_units=120_000
        )
    elif failure == "unqualified":
        rates = rates.model_copy(update={"qualified": False})
    else:
        rates.unit_prices["sarvam"]["saaras:v3"] = {}
    admission.costs = CostService(admission.database, admission.clock, admission.policy, rates)
    with pytest.raises((BudgetExceeded, RateUnavailable)):
        await admission.admit("user", "session")
    assert await admission.active_count() == 0


def test_application_loads_the_configured_runtime_policy(tmp_path):
    from app.config import Settings
    from app.main import create_app

    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        RuntimePolicy(user_calls_per_day=1, session_seconds=30).model_dump_json()
    )
    app = create_app(Settings(_env_file=None, policy_file=policy_file))
    assert app.state.runtime_policy.user_calls_per_day == 1
    assert app.state.runtime_policy.session_seconds == 30
    assert app.state.call_admission is None


@pytest.mark.parametrize("field", ["policy_file", "rate_card_file"])
def test_malformed_configured_admission_files_refuse_startup(tmp_path, field):
    from app.config import Settings
    from app.main import create_app

    path = tmp_path / "bad.json"
    path.write_text('{"unknown_field": true}')
    with pytest.raises(ValueError):
        create_app(Settings(_env_file=None, **{field: path}))


def test_policy_file_cannot_silently_change_the_selected_budget_profile(tmp_path):
    from app.config import Settings
    from app.main import create_app

    path = tmp_path / "policy.json"
    path.write_text(RuntimePolicy(profile="limited_vps").model_dump_json())
    with pytest.raises(ValueError, match="profile"):
        create_app(Settings(_env_file=None, policy_file=path, runtime_profile="owner_hosted"))


def test_fractional_recognition_duration_is_rounded_up():
    from app.voice.admission import remaining_audio_ms

    now = datetime(2026, 10, 18, tzinfo=UTC)
    assert remaining_audio_ms(now + timedelta(microseconds=1001), now) == 2
    assert remaining_audio_ms(now - timedelta(microseconds=1), now) == 0


async def test_admission_includes_whole_second_recognition_rounding(admission):
    policy = RuntimePolicy(session_micro_inr=1_500_000)
    rates = admission.costs._rates.model_copy(deep=True)
    rates.unit_prices["sarvam"]["saaras:v3"]["audio_ms"] = UnitPrice(
        amount_micros=1_000_000, per_units=1000
    )
    admission.policy = policy
    admission.costs = CostService(admission.database, admission.clock, policy, rates)
    with pytest.raises(BudgetExceeded):
        await admission.admit(
            "user", "session", access_expires_at=admission.clock.now + timedelta(milliseconds=1001)
        )


async def test_local_no_limits_admits_long_calls_past_spent_caps(database):
    from app.runtime import LocalUnlimitedPolicy

    clock = Clock()
    base = RuntimePolicy()
    policy = LocalUnlimitedPolicy.from_policy(base)
    rates = RateCard(
        version="fixture-v1",
        captured_at=clock.now,
        valid_until=clock.now + timedelta(days=30),
        source="https://example.com/fixture",
        currency="INR",
        fx_micro_inr=1_000_000,
        qualified=True,
        taxes_and_fees=PriceAdjustment(numerator=1, denominator=1, note="Synthetic test only"),
        unit_prices={
            "sarvam": {
                "saaras:v3": {"audio_ms": UnitPrice(amount_micros=1_000_000, per_units=120_000)}
            }
        },
    )
    costs = CostService(database, clock, policy, rates, user_day_limit=False)
    admission = CallAdmission(
        database, clock, policy, costs, stt_provider="sarvam", stt_model="saaras:v3"
    )
    # The normal ₹30 day is already used up.
    async with database.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO cost_periods(kind,owner,period,spent_micro_inr) "
                "VALUES ('global_day','*','2026-10-31',:spent)"
            ),
            {"spent": base.global_day_micro_inr},
        )
    lease = await admission.admit("user", "long-call")
    assert lease.expires_at - clock.now == timedelta(days=1)
    # Public limits are untouched by the local copy.
    assert base.session_seconds == 120 and base.global_day_micro_inr == 30_000_000


def test_no_limits_needs_dev_login(tmp_path):
    from app.config import Settings
    from app.main import create_app
    from app.runtime import LocalUnlimitedPolicy

    def policy(**flags):
        settings = Settings(_env_file=None, local_no_limits=True, **flags)
        return create_app(settings).state.runtime_policy

    assert isinstance(policy(dev_login=True), LocalUnlimitedPolicy)
    assert not isinstance(policy(dev_login=False), LocalUnlimitedPolicy)
