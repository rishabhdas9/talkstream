from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC
from typing import TypeVar
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.costs.models import (
    BudgetExceeded,
    CostAccountingError,
    CostScope,
    Reservation,
    Usage,
    WorkBound,
)
from app.costs.rates import RateCard, RateUnavailable, estimate_micro_inr
from app.db import Database
from app.runtime import Clock, RuntimePolicy

T = TypeVar("T")


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: dict) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


async def _uninterrupted(work: Awaitable[T], *, undo=None) -> T:
    """Finish a short ledger transaction, then deliver any caller cancellation.

    Cancelling SQLite I/O inside a transaction can orphan a connection that keeps the
    database write lock until garbage collection. `undo` reverses committed work whose
    caller was cancelled meanwhile.
    """
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if not cancelled:
        return task.result()
    if not task.cancelled() and task.exception() is None and undo is not None:
        await _uninterrupted(undo(task.result()))
    raise asyncio.CancelledError


class CostService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        policy: RuntimePolicy,
        rates: RateCard | None,
        *,
        user_day_limit: bool = True,
    ):
        self.database = database
        self.clock = clock
        self.policy = policy
        self.user_day_limit = user_day_limit
        # Own the nested maps; callers cannot change a reservation's rates during an await.
        self._rates = RateCard.model_validate_json(rates.model_dump_json()) if rates else None

    @property
    def rate_card(self) -> RateCard | None:
        return self._rates.model_copy(deep=True) if self._rates else None

    def estimate(self, provider: str, model: str, bound: WorkBound) -> int:
        return estimate_micro_inr(provider, model, bound, self._current_rates())

    def _current_rates(self) -> RateCard:
        if self._rates is None:
            raise RateUnavailable("Provider rates are not configured")
        self._rates.require_current(self.clock.utc_now())
        return self._rates

    @asynccontextmanager
    async def transaction(self):
        """Share this short write transaction with local admission, never provider I/O."""
        async with self.database.engine.connect() as conn:
            try:
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
                conn.info["talkstream_cost_writer"] = self
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            finally:
                conn.info.pop("talkstream_cost_writer", None)

    async def _check_accounting(self, conn):
        if (
            await conn.execute(text("SELECT 1 FROM cost_attempts WHERE state='disputed' LIMIT 1"))
        ).first():
            raise CostAccountingError("Provider usage needs operator reconciliation")
        # A reconciled hold stays until the Kolkata day ends: per-user day counters are stale too.
        held = await conn.execute(
            text(
                "SELECT 1 FROM recovery_holds WHERE until_day IS NULL OR until_day >= :day LIMIT 1"
            ),
            {"day": self._time()["day"]},
        )
        if held.first():
            raise CostAccountingError("Restored provider spending needs operator reconciliation")

    async def reconcile_restored_spend(self, *, day_micro_inr: int, month_micro_inr: int) -> None:
        """Record operator-supplied current-period vendor totals for a restore hold.

        A restored counter is not evidence of unused budget, so the ledger keeps whichever
        of the restored or supplied totals is higher. Per-user day counters cannot be
        rebuilt from vendor totals, so paid work resumes at the next Kolkata day.
        """
        if not 0 <= day_micro_inr <= month_micro_inr:
            raise ValueError("Reconciled spend must satisfy 0 <= day <= month")
        times = self._time()
        async with self.transaction() as conn:
            for kind, period, spent in (
                ("global_day", times["day"], day_micro_inr),
                ("global_month", times["month"], month_micro_inr),
            ):
                values = {"kind": kind, "period": period, "spent": spent}
                await conn.execute(
                    text(
                        "INSERT INTO cost_periods(kind,owner,period) VALUES (:kind,'*',:period) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    values,
                )
                await conn.execute(
                    text(
                        "UPDATE cost_periods SET spent_micro_inr=MAX(spent_micro_inr,:spent) "
                        "WHERE kind=:kind AND owner='*' AND period=:period"
                    ),
                    values,
                )
            await conn.execute(
                text("UPDATE recovery_holds SET until_day=:day WHERE until_day IS NULL"),
                {"day": times["day"]},
            )

    def _time(self) -> dict[str, str]:
        now = self.clock.utc_now().astimezone(UTC)
        local = now.astimezone(ZoneInfo("Asia/Kolkata"))
        return {
            "day": local.strftime("%Y-%m-%d"),
            "month": local.strftime("%Y-%m"),
            "now": now.isoformat(timespec="microseconds"),
        }

    def _periods(self, user: str, day: str, month: str):
        return (
            ("user_day", user, day, self.policy.user_day_micro_inr),
            ("global_day", "*", day, self.policy.global_day_micro_inr),
            ("global_month", "*", month, self.policy.variable_month_micro_inr),
        )

    async def _check_budget(self, conn, user: str, day: str, month: str, amount: int):
        for kind, owner, period, cap in self._periods(user, day, month):
            if kind == "user_day" and not self.user_day_limit:
                continue
            spent, held = (
                await conn.execute(
                    text(
                        "SELECT COALESCE(SUM(CASE WHEN period=:period "
                        "THEN spent_micro_inr ELSE 0 END),0), "
                        "COALESCE(SUM(held_micro_inr),0) FROM cost_periods WHERE "
                        "kind=:kind AND owner=:owner"
                    ),
                    {"kind": kind, "owner": owner, "period": period},
                )
            ).one()
            if spent + held + amount > cap:
                raise BudgetExceeded(f"{kind} provider allowance exhausted")

    async def _charge(self, conn, user: str, day: str, month: str, *, spent=0, held=0):
        for kind, owner, period, _ in self._periods(user, day, month):
            values = {"kind": kind, "owner": owner, "period": period, "spent": spent, "held": held}
            await conn.execute(
                text(
                    "INSERT INTO cost_periods(kind,owner,period) VALUES (:kind,:owner,:period) "
                    "ON CONFLICT DO NOTHING"
                ),
                values,
            )
            await conn.execute(
                text(
                    "UPDATE cost_periods SET spent_micro_inr=spent_micro_inr+:spent, "
                    "held_micro_inr=held_micro_inr+:held "
                    "WHERE kind=:kind AND owner=:owner AND period=:period"
                ),
                values,
            )

    async def reserve_envelope(
        self,
        scope: CostScope,
        envelope_id: str,
        maximum_micro_inr: int | None = None,
        *,
        connection: AsyncConnection | None = None,
    ) -> Reservation:
        if scope.session_id is None:
            raise ValueError("A call envelope requires a session")
        maximum = self.policy.session_micro_inr if maximum_micro_inr is None else maximum_micro_inr
        reservation = Reservation(id=envelope_id, maximum_micro_inr=maximum)
        if not 0 < maximum <= self.policy.session_micro_inr:
            raise BudgetExceeded("Call envelope exceeds session allowance")
        fingerprint = _fingerprint({"scope": scope.model_dump(), "maximum": maximum})
        async with self.transaction() if connection is None else nullcontext(connection) as conn:
            if conn.info.get("talkstream_cost_writer") is not self:
                raise ValueError("Envelope needs this service's immediate transaction")
            await self._check_accounting(conn)
            existing = (
                (
                    await conn.execute(
                        text("SELECT * FROM cost_envelopes WHERE id=:id OR session_id=:session"),
                        {"id": envelope_id, "session": scope.session_id},
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if existing["id"] != envelope_id or existing["request_hash"] != fingerprint:
                    raise ValueError("Envelope identity/arguments do not match")
                return reservation
            self._current_rates()
            when = self._time()
            await self._check_budget(conn, scope.user_id, when["day"], when["month"], maximum)
            await self._charge(conn, scope.user_id, when["day"], when["month"], held=maximum)
            await conn.execute(
                text(
                    "INSERT INTO cost_envelopes "
                    "(id,request_hash,user_id,session_id,maximum_micro_inr,"
                    "state,day_key,month_key,created_at) "
                    "VALUES (:id,:hash,:user,:session,:maximum,'active',:day,:month,:now)"
                ),
                {
                    "id": envelope_id,
                    "hash": fingerprint,
                    "user": scope.user_id,
                    "session": scope.session_id,
                    "maximum": maximum,
                    **when,
                },
            )
        return reservation

    async def release_envelope(
        self, envelope_id: str, *, connection: AsyncConnection | None = None
    ) -> None:
        async with self.transaction() if connection is None else nullcontext(connection) as conn:
            if conn.info.get("talkstream_cost_writer") is not self:
                raise ValueError("Envelope needs this service's immediate transaction")
            envelope = (
                (
                    await conn.execute(
                        text("SELECT * FROM cost_envelopes WHERE id=:id"), {"id": envelope_id}
                    )
                )
                .mappings()
                .one_or_none()
            )
            if envelope is None:
                raise KeyError("Unknown cost envelope")
            if envelope["state"] == "closed":
                return
            unused = (
                envelope["maximum_micro_inr"]
                - envelope["spent_micro_inr"]
                - envelope["held_micro_inr"]
            )
            await self._charge(
                conn, envelope["user_id"], envelope["day_key"], envelope["month_key"], held=-unused
            )
            await conn.execute(
                text("UPDATE cost_envelopes SET state='closed' WHERE id=:id"), {"id": envelope_id}
            )

    async def reserve(
        self,
        scope: CostScope,
        attempt_id: str,
        provider: str,
        model: str,
        bound: WorkBound,
        *,
        evaluation_cap_micro_inr: int | None = None,
    ) -> Reservation:
        def release(reservation: Reservation):
            # The caller is gone before any provider request, so the hold is unused.
            units = {unit: 0 for unit in bound.units}
            return self._settle(reservation.id, Usage(units=units), "cancelled")

        return await _uninterrupted(
            self._reserve(
                scope,
                attempt_id,
                provider,
                model,
                bound,
                evaluation_cap_micro_inr=evaluation_cap_micro_inr,
            ),
            undo=release,
        )

    async def _reserve(
        self,
        scope: CostScope,
        attempt_id: str,
        provider: str,
        model: str,
        bound: WorkBound,
        *,
        evaluation_cap_micro_inr: int | None = None,
    ) -> Reservation:
        is_evaluation = bool(scope.job_id and scope.job_id.startswith("live-eval:"))
        if (is_evaluation or evaluation_cap_micro_inr is not None) and (
            not is_evaluation
            or type(evaluation_cap_micro_inr) is not int
            or evaluation_cap_micro_inr <= 0
        ):
            raise BudgetExceeded("Live evaluation allowance is unavailable")
        # Snapshot mutable unit dictionaries before any database await.
        bound = WorkBound.model_validate_json(bound.model_dump_json())
        fingerprint = _fingerprint(
            {
                "scope": scope.model_dump(),
                "provider": provider,
                "model": model,
                "bound": bound.model_dump(),
            }
        )
        async with self.transaction() as conn:
            await self._check_accounting(conn)
            existing = (
                (
                    await conn.execute(
                        text("SELECT * FROM cost_attempts WHERE id=:id"), {"id": attempt_id}
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["request_hash"] != fingerprint:
                    raise ValueError("Attempt ID already has different arguments")
                return Reservation(id=attempt_id, maximum_micro_inr=existing["maximum_micro_inr"])
            rates = self._current_rates()
            maximum = estimate_micro_inr(provider, model, bound, rates)
            reservation = Reservation(id=attempt_id, maximum_micro_inr=maximum)
            if is_evaluation:
                used = (
                    await conn.execute(
                        text(
                            "SELECT COALESCE(SUM(CASE WHEN state='settled' THEN actual_micro_inr "
                            "ELSE maximum_micro_inr END),0) FROM cost_attempts "
                            "WHERE job_id LIKE 'live-eval:%'"
                        )
                    )
                ).scalar_one()
                if used + maximum > evaluation_cap_micro_inr:
                    raise BudgetExceeded("Live evaluation allowance exhausted")
            when = self._time()
            if scope.session_id is None:
                await self._check_budget(conn, scope.user_id, when["day"], when["month"], maximum)
                await self._charge(conn, scope.user_id, when["day"], when["month"], held=maximum)
            else:
                envelope = (
                    (
                        await conn.execute(
                            text("SELECT * FROM cost_envelopes WHERE session_id=:session"),
                            {"session": scope.session_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if envelope is None or envelope["state"] != "active":
                    raise BudgetExceeded("No active call envelope")
                if envelope["user_id"] != scope.user_id:
                    raise ValueError("Call envelope belongs to a different user")
                if envelope["spent_micro_inr"] + envelope["held_micro_inr"] + maximum > min(
                    envelope["maximum_micro_inr"], self.policy.session_micro_inr
                ):
                    raise BudgetExceeded("Session provider allowance exhausted")
                await self._check_budget(conn, scope.user_id, when["day"], when["month"], 0)
                # Existing envelope capacity is already held against user/global quotas.
                await conn.execute(
                    text(
                        "UPDATE cost_envelopes SET held_micro_inr=held_micro_inr+:maximum "
                        "WHERE id=:id"
                    ),
                    {"id": envelope["id"], "maximum": maximum},
                )
            await conn.execute(
                text(
                    "INSERT INTO cost_attempts (id,request_hash,user_id,session_i"
                    "d,job_id,provider,model,"
                    "bound_json,rate_card_json,maximum_micro_inr,state,day_key,mo"
                    "nth_key,created_at) "
                    "VALUES (:id,:hash,:user,:session,:job,:provider,:model,:bound,:rates,:maximum,"
                    "'reserved',:day,:month,:now)"
                ),
                {
                    "id": attempt_id,
                    "hash": fingerprint,
                    "user": scope.user_id,
                    "session": scope.session_id,
                    "job": scope.job_id,
                    "provider": provider,
                    "model": model,
                    "bound": bound.model_dump_json(),
                    "rates": rates.model_dump_json(),
                    "maximum": maximum,
                    **when,
                },
            )
        return reservation

    async def settle(
        self,
        attempt_id: str,
        usage: Usage | None,
        outcome: str,
        *,
        connection: AsyncConnection | None = None,
    ) -> None:
        await _uninterrupted(self._settle(attempt_id, usage, outcome, connection))

    async def _settle(
        self,
        attempt_id: str,
        usage: Usage | None,
        outcome: str,
        connection: AsyncConnection | None = None,
    ) -> None:
        if outcome not in {"succeeded", "failed", "cancelled", "unknown"}:
            raise ValueError("Unknown provider outcome")
        if usage is not None:
            usage = Usage.model_validate_json(usage.model_dump_json())
        encoded_usage = None if usage is None else _json(usage.model_dump())
        async with self.transaction() if connection is None else nullcontext(connection) as conn:
            if conn.info.get("talkstream_cost_writer") is not self:
                raise ValueError("Settlement needs this service's immediate transaction")
            attempt = (
                (
                    await conn.execute(
                        text("SELECT * FROM cost_attempts WHERE id=:id"), {"id": attempt_id}
                    )
                )
                .mappings()
                .one_or_none()
            )
            if attempt is None:
                raise KeyError("Unknown provider attempt")
            if attempt["state"] == "disputed":
                raise CostAccountingError("Disputed usage requires operator reconciliation")
            if attempt["state"] == "settled":
                if usage is not None and (
                    encoded_usage != attempt["usage_json"] or outcome != attempt["outcome"]
                ):
                    raise ValueError("Settled usage/outcome cannot be changed")
                return
            if attempt["actual_micro_inr"] is not None:
                return  # Unknown usage was already charged at its full bound.
            maximum = attempt["maximum_micro_inr"]
            if usage is None:
                # Unknown usage costs its full bound, so the hold ends with its day.
                actual = maximum
            else:
                bound = WorkBound.model_validate_json(attempt["bound_json"])
                card = RateCard.model_validate_json(attempt["rate_card_json"])
                try:
                    actual = estimate_micro_inr(attempt["provider"], attempt["model"], usage, card)
                except RateUnavailable:
                    actual = None
            if usage is not None and (
                usage.units.keys() != bound.units.keys()
                or any(count > bound.units[unit] for unit, count in usage.units.items())
                or actual is None
            ):
                await conn.execute(
                    text(
                        "UPDATE cost_attempts SET state='disputed',outcome=:outcome,"
                        "usage_json=:usage,"
                        "actual_micro_inr=:actual WHERE id=:id"
                    ),
                    {
                        "id": attempt_id,
                        "outcome": outcome,
                        "usage": encoded_usage,
                        "actual": actual,
                    },
                )
                disputed = True
            else:
                disputed = False
                if attempt["session_id"] is None:
                    await self._charge(
                        conn,
                        attempt["user_id"],
                        attempt["day_key"],
                        attempt["month_key"],
                        spent=actual,
                        held=-maximum,
                    )
                else:
                    envelope = (
                        (
                            await conn.execute(
                                text("SELECT * FROM cost_envelopes WHERE session_id=:session"),
                                {"session": attempt["session_id"]},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    freed = actual if envelope["state"] == "active" else maximum
                    await self._charge(
                        conn,
                        attempt["user_id"],
                        envelope["day_key"],
                        envelope["month_key"],
                        held=-freed,
                    )
                    await self._charge(
                        conn,
                        attempt["user_id"],
                        attempt["day_key"],
                        attempt["month_key"],
                        spent=actual,
                    )
                    await conn.execute(
                        text(
                            "UPDATE cost_envelopes SET held_micro_inr=held_micro_inr-:maximum, "
                            "spent_micro_inr=spent_micro_inr+:actual WHERE id=:id"
                        ),
                        {"id": envelope["id"], "maximum": maximum, "actual": actual},
                    )
                await conn.execute(
                    text(
                        "UPDATE cost_attempts SET state=:state,outcome=:outcome,"
                        "usage_json=:usage,"
                        "actual_micro_inr=:actual WHERE id=:id"
                    ),
                    {
                        "id": attempt_id,
                        # An "unknown" outcome may carry a locally bounded usage, still unconfirmed.
                        "state": "unknown" if usage is None or outcome == "unknown" else "settled",
                        "outcome": outcome,
                        "usage": encoded_usage,
                        "actual": actual,
                    },
                )

        if disputed:
            raise CostAccountingError("Reported usage exceeds or differs from the work bound")

    async def outstanding_micro_inr(self, scope: CostScope) -> int:
        async with self.database.engine.connect() as conn:
            if scope.session_id is not None:
                row = (
                    (
                        await conn.execute(
                            text(
                                "SELECT * FROM cost_envelopes WHERE user_id=:user"
                                " AND session_id=:session"
                            ),
                            {"user": scope.user_id, "session": scope.session_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                return (
                    0
                    if row is None
                    else (
                        row["maximum_micro_inr"] - row["spent_micro_inr"]
                        if row["state"] == "active"
                        else row["held_micro_inr"]
                    )
                )
            return (
                await conn.execute(
                    text(
                        "SELECT COALESCE(SUM(maximum_micro_inr),0) FROM cost_attempts "
                        "WHERE user_id=:user AND job_id=:job AND state!='settled'"
                    ),
                    {"user": scope.user_id, "job": scope.job_id},
                )
            ).scalar_one()

    async def spent_micro_inr(self) -> int:
        async with self.database.engine.connect() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT COALESCE(SUM(spent_micro_inr),0) "
                        "FROM cost_periods WHERE kind='global_day'"
                    )
                )
            ).scalar_one()
