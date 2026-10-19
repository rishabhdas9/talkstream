from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.models import TransactionKind, UsageSession, WalletTransaction, utc_now


class WalletError(RuntimeError):
    pass


class RechargeAmountError(WalletError):
    pass


class RechargeLimitError(WalletError):
    pass


def day_start(now: datetime) -> datetime:
    """Midnight in Kolkata, as UTC: the wallet's daily reset."""
    local = now.astimezone(ZoneInfo("Asia/Kolkata"))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


@dataclass(frozen=True)
class SessionBillingResult:
    billable_seconds: int
    cost_paise: int
    charged_paise: int
    balance_paise: int
    exhausted: bool


class WalletService:
    """Daily wallet: every Kolkata day starts at daily_allowance_paise plus that
    day's ledger rows; earlier days' balances never carry over."""

    def __init__(
        self,
        database: Database,
        *,
        daily_allowance_paise: int = 0,
        daily_topups: int | None = None,
    ) -> None:
        self._database = database
        self.daily_allowance_paise = daily_allowance_paise
        self.daily_topups = daily_topups

    async def balance(self, user_id: str) -> int:
        async with self._database.session_factory() as session:
            return await self._balance_in_session(session, user_id)

    async def recent_transactions(
        self,
        user_id: str,
        *,
        limit: int = 200,
    ):
        # Billing writes one ledger row per tick; the listing folds usage rows
        # into one entry per session (full-ledger sums) while topups stay
        # individual. The raw per-tick ledger remains the source of truth.
        group_key = func.coalesce(WalletTransaction.usage_session_id, WalletTransaction.id)
        async with self._database.session_factory() as session:
            result = await session.execute(
                select(
                    func.max(WalletTransaction.id).label("id"),
                    func.sum(WalletTransaction.amount_paise).label("amount_paise"),
                    func.max(WalletTransaction.kind).label("kind"),
                    WalletTransaction.usage_session_id.label("usage_session_id"),
                    func.max(WalletTransaction.created_at).label("created_at"),
                )
                .where(
                    WalletTransaction.user_id == user_id,
                    WalletTransaction.created_at >= day_start(utc_now()),
                )
                .group_by(group_key)
                .order_by(func.max(WalletTransaction.created_at).desc())
                .limit(limit)
            )
            return list(result.all())

    async def recharge(
        self,
        *,
        user_id: str,
        amount_paise: int,
        max_amount_paise: int,
    ) -> tuple[WalletTransaction, int]:
        if amount_paise <= 0 or amount_paise > max_amount_paise:
            raise RechargeAmountError(f"amount_paise must be between 1 and {max_amount_paise}")
        async with self._database.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            if await self._topups_left(session, user_id) == 0:
                raise RechargeLimitError("Today's top-up has already been used")
            transaction = WalletTransaction(
                user_id=user_id,
                amount_paise=amount_paise,
                kind=TransactionKind.TOPUP,
            )
            session.add(transaction)
            await session.flush()
            balance = await self._balance_in_session(session, user_id)
            await session.commit()
            return transaction, balance

    async def mark_ready(self, *, user_id: str, usage_session_id: str, ready_at: datetime) -> None:
        if ready_at.utcoffset() is None:
            raise ValueError("Ready time must be timezone-aware")
        ready_at = ready_at.astimezone(UTC)
        async with self._database.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            usage = await session.get(UsageSession, usage_session_id)
            if usage is None or usage.user_id != user_id or usage.ended_at is not None:
                raise WalletError("Usage session is missing or has ended")
            if usage.ready_at is not None:
                existing = usage.ready_at
                if existing.utcoffset() is None:
                    existing = existing.replace(tzinfo=UTC)
                if existing != ready_at:
                    raise WalletError("Session readiness cannot be changed")
            else:
                usage.ready_at = ready_at
            await session.commit()

    async def bill_session(
        self,
        *,
        user_id: str,
        usage_session_id: str,
        billable_seconds: int,
        target_cost_paise: int,
    ) -> SessionBillingResult:
        if billable_seconds < 0 or target_cost_paise < 0:
            raise ValueError("billing targets must be non-negative")
        async with self._database.session_factory() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            usage_result = await session.execute(
                select(UsageSession).where(
                    UsageSession.id == usage_session_id,
                    UsageSession.user_id == user_id,
                )
            )
            usage = usage_result.scalar_one_or_none()
            if usage is None:
                await session.rollback()
                raise WalletError("usage session does not exist for this user")

            if usage.ended_at is not None:
                raise WalletError("Usage session has ended")
            balance_before = await self._balance_in_session(session, user_id)
            outstanding = max(0, target_cost_paise - usage.cost_paise)
            charged = min(outstanding, max(0, balance_before))
            if charged:
                session.add(
                    WalletTransaction(
                        user_id=user_id,
                        amount_paise=-charged,
                        kind=TransactionKind.USAGE,
                        usage_session_id=usage_session_id,
                    )
                )
            usage.billed_seconds = max(usage.billed_seconds, billable_seconds)
            usage.cost_paise += charged
            balance_after = balance_before - charged
            await session.commit()
            return SessionBillingResult(
                billable_seconds=usage.billed_seconds,
                cost_paise=usage.cost_paise,
                charged_paise=charged,
                balance_paise=balance_after,
                exhausted=balance_after <= 0 and outstanding > 0,
            )

    async def topups_left(self, user_id: str) -> int | None:
        async with self._database.session_factory() as session:
            return await self._topups_left(session, user_id)

    async def _topups_left(self, session: AsyncSession, user_id: str) -> int | None:
        if self.daily_topups is None:
            return None
        used = await session.scalar(
            select(func.count()).where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.kind == TransactionKind.TOPUP,
                WalletTransaction.created_at >= day_start(utc_now()),
            )
        )
        return max(0, self.daily_topups - int(used or 0))

    async def _balance_in_session(self, session: AsyncSession, user_id: str) -> int:
        result = await session.execute(
            select(func.coalesce(func.sum(WalletTransaction.amount_paise), 0)).where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.created_at >= day_start(utc_now()),
            )
        )
        return self.daily_allowance_paise + int(result.scalar_one())
