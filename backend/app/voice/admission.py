from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.costs.models import BudgetExceeded, CostScope, WorkBound
from app.costs.service import CostService
from app.db import Database
from app.models import new_id
from app.runtime import Clock, RuntimePolicy


class CallLimitExceeded(ValueError):
    pass


@dataclass(frozen=True)
class AdmissionLease:
    session_id: str
    expires_at: datetime
    envelope_id: str


def remaining_audio_ms(deadline: datetime, now: datetime) -> int:
    delta = deadline - now
    return max(
        0, delta.days * 86_400_000 + delta.seconds * 1000 + (delta.microseconds + 999) // 1000
    )


def recognition_bound_ms(deadline: datetime, now: datetime) -> int:
    milliseconds = remaining_audio_ms(deadline, now)
    return ((milliseconds + 999) // 1000) * 1000


class CallAdmission:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        policy: RuntimePolicy,
        costs: CostService,
        *,
        stt_provider: str,
        stt_model: str,
        daily_call_limit: bool = True,
    ):
        self.database = database
        self.clock = clock
        self.policy = policy
        self.costs = costs
        self.stt_provider = stt_provider
        self.stt_model = stt_model
        self.daily_call_limit = daily_call_limit

    async def admit(
        self, user_id: str, session_id: str, *, access_expires_at: datetime | None = None
    ) -> AdmissionLease:
        scope = CostScope(user_id=user_id, session_id=session_id)
        if access_expires_at is not None and access_expires_at.utcoffset() is None:
            raise ValueError("Access deadline must be timezone-aware")
        async with self.costs.transaction() as conn:
            now = self.clock.utc_now().astimezone(UTC)
            expiry = now + timedelta(seconds=self.policy.session_seconds)
            if access_expires_at is not None:
                expiry = min(expiry, access_expires_at.astimezone(UTC))
            if expiry <= now:
                raise CallLimitExceeded("Call authorization has expired")
            existing = (
                (
                    await conn.execute(
                        text("SELECT * FROM call_leases WHERE session_id=:session"),
                        {"session": session_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["user_id"] != user_id:
                    raise ValueError("Session identity does not match")
                old_expiry = datetime.fromisoformat(existing["expires_at"])
                if existing["state"] != "active" or old_expiry <= now:
                    raise CallLimitExceeded("Session cannot be reused")
                return AdmissionLease(session_id, old_expiry, existing["envelope_id"])
            day = now.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
            user_active, global_active, daily = (
                await conn.execute(
                    text(
                        "SELECT COALESCE(SUM(user_id=:user AND state='active'),0), "
                        "COALESCE(SUM(state='active'),0), "
                        "COALESCE(SUM(user_id=:user AND day_key=:day),0) "
                        "FROM call_leases"
                    ),
                    {"user": user_id, "day": day},
                )
            ).one()
            if (
                user_active >= self.policy.user_active_calls
                or global_active >= self.policy.global_active_calls
                or (self.daily_call_limit and daily >= self.policy.user_calls_per_day)
            ):
                raise CallLimitExceeded("Call limit reached. Try again later.")
            quote = self.costs.estimate(
                self.stt_provider,
                self.stt_model,
                WorkBound(units={"audio_ms": recognition_bound_ms(expiry, now)}),
            )
            if quote > self.policy.session_micro_inr:
                raise BudgetExceeded("Full permitted recognition stream exceeds session allowance")
            envelope_id = new_id()
            await self.costs.reserve_envelope(scope, envelope_id, connection=conn)
            await conn.execute(
                text(
                    "INSERT INTO call_leases "
                    "(session_id,user_id,envelope_id,expires_at,state,day_key,created_at) "
                    "VALUES (:session,:user,:envelope,:expiry,'active',:day,:now)"
                ),
                {
                    "session": session_id,
                    "user": user_id,
                    "envelope": envelope_id,
                    "expiry": expiry.isoformat(timespec="microseconds"),
                    "day": day,
                    "now": now.isoformat(timespec="microseconds"),
                },
            )
        return AdmissionLease(session_id, expiry, envelope_id)

    async def release(self, session_id: str) -> None:
        async with self.costs.transaction() as conn:
            lease = (
                (
                    await conn.execute(
                        text("SELECT * FROM call_leases WHERE session_id=:session"),
                        {"session": session_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if lease is None or lease["state"] == "released":
                return
            await self.costs.release_envelope(lease["envelope_id"], connection=conn)
            await conn.execute(
                text("UPDATE call_leases SET state='released' WHERE session_id=:session"),
                {"session": session_id},
            )

    async def active_count(self) -> int:
        async with self.database.engine.connect() as conn:
            return (
                await conn.execute(text("SELECT COUNT(*) FROM call_leases WHERE state='active'"))
            ).scalar_one()
