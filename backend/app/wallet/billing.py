from __future__ import annotations

import math
import time
from dataclasses import dataclass

from app.wallet.service import SessionBillingResult, WalletService


def prorated_cost_paise(*, billable_seconds: int, price_per_minute_paise: int) -> int:
    if billable_seconds < 0:
        raise ValueError("billable_seconds must be non-negative")
    if price_per_minute_paise <= 0:
        raise ValueError("price_per_minute_paise must be positive")
    return (billable_seconds * price_per_minute_paise + 30) // 60


def full_elapsed_seconds(*, connected_at: float, now: float) -> int:
    if now < connected_at:
        raise ValueError("billing clock cannot move backwards")
    return math.floor(now - connected_at)


def final_billable_seconds(*, connected_at: float, disconnected_at: float) -> int:
    if disconnected_at < connected_at:
        raise ValueError("disconnect cannot precede connect")
    elapsed = disconnected_at - connected_at
    return math.ceil(elapsed) if elapsed > 0 else 0


@dataclass
class BillingMeter:
    wallet: WalletService
    user_id: str
    usage_session_id: str
    connected_at: float
    price_per_minute_paise: int

    async def charge_full_elapsed(self, now: float | None = None) -> SessionBillingResult:
        current = time.monotonic() if now is None else now
        return await self.charge_through(
            full_elapsed_seconds(connected_at=self.connected_at, now=current)
        )

    async def finalize(self, disconnected_at: float | None = None) -> SessionBillingResult:
        ended = time.monotonic() if disconnected_at is None else disconnected_at
        return await self.charge_through(
            final_billable_seconds(
                connected_at=self.connected_at,
                disconnected_at=ended,
            )
        )

    async def charge_through(self, billable_seconds: int) -> SessionBillingResult:
        return await self.wallet.bill_session(
            user_id=self.user_id,
            usage_session_id=self.usage_session_id,
            billable_seconds=billable_seconds,
            target_cost_paise=prorated_cost_paise(
                billable_seconds=billable_seconds,
                price_per_minute_paise=self.price_per_minute_paise,
            ),
        )
