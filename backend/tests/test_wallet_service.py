from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.models import TransactionKind, UsageSession, User, WalletTransaction
from app.wallet.service import RechargeAmountError, RechargeLimitError, WalletService


async def seed_user(database, email: str = "person@example.com") -> User:
    async with database.session_factory() as session:
        user = User(email=email, is_verified=True)
        session.add(user)
        await session.commit()
        return user


async def test_recharge_updates_ledger_balance_and_recent_transactions(database) -> None:
    user = await seed_user(database)
    wallet = WalletService(database)

    transaction, balance = await wallet.recharge(
        user_id=user.id,
        amount_paise=500,
        max_amount_paise=1000,
    )

    assert transaction.kind is TransactionKind.TOPUP
    assert balance == 500
    assert await wallet.balance(user.id) == 500
    assert [item.amount_paise for item in await wallet.recent_transactions(user.id)] == [500]


async def test_recharge_rejects_amount_above_limit(database) -> None:
    user = await seed_user(database)
    with pytest.raises(RechargeAmountError):
        await WalletService(database).recharge(
            user_id=user.id,
            amount_paise=1001,
            max_amount_paise=1000,
        )


async def test_begin_immediate_serializes_concurrent_recharges(database) -> None:
    user = await seed_user(database)
    wallet = WalletService(database)

    results = await asyncio.gather(
        wallet.recharge(user_id=user.id, amount_paise=500, max_amount_paise=1000),
        wallet.recharge(user_id=user.id, amount_paise=500, max_amount_paise=1000),
    )

    assert sorted(balance for _, balance in results) == [500, 1000]
    assert await wallet.balance(user.id) == 1000


async def test_incremental_billing_is_idempotent_links_usage_and_exhausts(database) -> None:
    user = await seed_user(database)
    wallet = WalletService(database)
    await wallet.recharge(user_id=user.id, amount_paise=5, max_amount_paise=1000)
    async with database.session_factory() as session:
        session.add(UsageSession(id="usage-1", user_id=user.id))
        await session.commit()

    first = await wallet.bill_session(
        user_id=user.id,
        usage_session_id="usage-1",
        billable_seconds=1,
        target_cost_paise=3,
    )
    duplicate = await wallet.bill_session(
        user_id=user.id,
        usage_session_id="usage-1",
        billable_seconds=1,
        target_cost_paise=3,
    )
    exhausted = await wallet.bill_session(
        user_id=user.id,
        usage_session_id="usage-1",
        billable_seconds=2,
        target_cost_paise=7,
    )

    assert first.charged_paise == 3 and first.balance_paise == 2
    assert duplicate.charged_paise == 0 and duplicate.cost_paise == 3
    assert exhausted.charged_paise == 2
    assert exhausted.balance_paise == 0 and exhausted.exhausted
    async with database.session_factory() as session:
        usage = (
            await session.execute(select(UsageSession).where(UsageSession.id == "usage-1"))
        ).scalar_one()
        deductions = list(
            (
                await session.execute(
                    select(WalletTransaction).where(WalletTransaction.usage_session_id == "usage-1")
                )
            ).scalars()
        )
        assert usage.billed_seconds == 2 and usage.cost_paise == 5
        assert sum(item.amount_paise for item in deductions) == -5


async def test_daily_wallet_resets_to_allowance_and_limits_topups(database) -> None:
    from datetime import timedelta

    from app.models import utc_now
    from app.wallet.service import day_start

    user = await seed_user(database)
    wallet = WalletService(database, daily_allowance_paise=3000, daily_topups=1)
    async with database.session_factory() as session:
        # Yesterday's top-up and spend never carry over or use up today's top-up.
        yesterday = day_start(utc_now()) - timedelta(minutes=1)
        for amount, kind in ((1000, TransactionKind.TOPUP), (-3900, TransactionKind.USAGE)):
            session.add(
                WalletTransaction(
                    user_id=user.id, amount_paise=amount, kind=kind, created_at=yesterday
                )
            )
        await session.commit()
    assert await wallet.balance(user.id) == 3000
    assert await wallet.topups_left(user.id) == 1
    assert await wallet.recent_transactions(user.id) == []

    _, balance = await wallet.recharge(user_id=user.id, amount_paise=1000, max_amount_paise=1000)
    assert balance == 4000 and await wallet.topups_left(user.id) == 0
    with pytest.raises(RechargeLimitError):
        await wallet.recharge(user_id=user.id, amount_paise=1, max_amount_paise=1000)
    assert await wallet.balance(user.id) == 4000


def test_day_start_is_kolkata_midnight() -> None:
    from datetime import UTC, datetime

    from app.wallet.service import day_start

    # 23:59:59 IST on 31 October is 18:29:59 UTC; that day began at 18:30 UTC on 30 October.
    assert day_start(datetime(2026, 10, 31, 18, 29, 59, tzinfo=UTC)) == datetime(
        2026, 10, 30, 18, 30, tzinfo=UTC
    )
    assert day_start(datetime(2026, 10, 31, 18, 30, tzinfo=UTC)) == datetime(
        2026, 10, 31, 18, 30, tzinfo=UTC
    )
