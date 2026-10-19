from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import (
    and_,
    column,
    delete,
    exists,
    func,
    insert,
    literal,
    select,
    table,
    text,
)

from app.db import Database
from app.knowledge.models import KnowledgeDeletionEvent
from app.models import (
    OtpChallenge,
    TransactionKind,
    TurnMetric,
    UsageSession,
    WalletTransaction,
)
from app.runtime import Clock, RuntimePolicy

logger = logging.getLogger(__name__)


_TOOL_RECEIPTS = table("tool_receipts", column("session_id"))


@dataclass(frozen=True)
class PurgeResult:
    deleted: dict[str, int]


# Raw-SQL tables store aware ISO-8601 text; ORM tables are compared through their columns.
_RAW_DELETES = {
    "voice_tickets": "DELETE FROM voice_tickets WHERE expires_at < :otp_cutoff",
    "otp_send_attempts": "DELETE FROM otp_send_attempts WHERE sent_at < :cutoff",
    "tool_receipts": "DELETE FROM tool_receipts WHERE created_at < :cutoff",
    # Reserved, unknown and disputed attempts stay as evidence; reserved ones still hold budget.
    # Live-evaluation attempts stay: their cap is a cumulative sum over these rows.
    "cost_attempts": (
        "DELETE FROM cost_attempts WHERE state='settled' AND created_at < :cutoff"
        " AND (job_id IS NULL OR job_id NOT LIKE 'live-eval:%')"
    ),
    "call_leases": "DELETE FROM call_leases WHERE state='released' AND created_at < :cutoff",
    "cost_envelopes": (
        "DELETE FROM cost_envelopes WHERE state='closed' AND created_at < :cutoff"
        " AND NOT EXISTS (SELECT 1 FROM cost_attempts WHERE session_id=cost_envelopes.session_id)"
        " AND NOT EXISTS (SELECT 1 FROM call_leases WHERE envelope_id=cost_envelopes.id)"
    ),
    # Current and prior-period counters stay; any row still holding money is never dropped.
    "cost_periods": (
        "DELETE FROM cost_periods WHERE held_micro_inr=0 AND ("
        "(kind='global_month' AND period < :prior_month)"
        " OR (kind!='global_month' AND period < :prior_month_day))"
    ),
    "knowledge_ingestions": (
        "DELETE FROM knowledge_ingestions WHERE state IN ('complete','failed','released')"
        " AND created_at < :cutoff"
    ),
}


async def purge_expired(
    database: Database, now: datetime, policy: RuntimePolicy | None = None
) -> PurgeResult:
    policy = policy or RuntimePolicy()
    otp_cutoff = now - timedelta(days=policy.expired_otp_retention_days)
    cutoff = now - timedelta(days=policy.diagnostic_retention_days)
    local = now.astimezone(ZoneInfo("Asia/Kolkata"))
    prior = (local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    values = {
        "otp_cutoff": otp_cutoff.isoformat(timespec="microseconds"),
        "cutoff": cutoff.isoformat(timespec="microseconds"),
        "prior_month": prior,
        "prior_month_day": f"{prior}-01",
    }
    deleted: dict[str, int] = {}
    async with database.engine.connect() as conn:
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            for table, statement in _RAW_DELETES.items():
                deleted[table] = (await conn.execute(text(statement), values)).rowcount
            deleted["otp_challenges"] = (
                await conn.execute(delete(OtpChallenge).where(OtpChallenge.expires_at < otp_cutoff))
            ).rowcount
            deleted["turn_metrics"] = (
                await conn.execute(delete(TurnMetric).where(TurnMetric.created_at < cutoff))
            ).rowcount
            deleted["knowledge_deletion_events"] = (
                await conn.execute(
                    delete(KnowledgeDeletionEvent).where(
                        KnowledgeDeletionEvent.completed_at < cutoff
                    )
                )
            ).rowcount
            expired_sessions = (
                select(UsageSession.id)
                .where(
                    UsageSession.ended_at.is_not(None),
                    UsageSession.started_at < cutoff,
                    ~exists().where(TurnMetric.usage_session_id == UsageSession.id),
                    ~exists().where(_TOOL_RECEIPTS.c.session_id == UsageSession.id),
                )
                .scalar_subquery()
            )
            # The wallet balance is its ledger sum: fold an expired session's ticks into one row.
            await conn.execute(
                insert(WalletTransaction).from_select(
                    ["id", "user_id", "amount_paise", "kind", "created_at"],
                    select(
                        func.lower(func.hex(func.randomblob(16))),
                        WalletTransaction.user_id,
                        func.sum(WalletTransaction.amount_paise),
                        literal(TransactionKind.USAGE.value),
                        func.max(WalletTransaction.created_at),
                    )
                    .where(WalletTransaction.usage_session_id.in_(expired_sessions))
                    .group_by(WalletTransaction.user_id, WalletTransaction.usage_session_id),
                )
            )
            await conn.execute(
                delete(WalletTransaction).where(
                    and_(
                        WalletTransaction.usage_session_id.is_not(None),
                        WalletTransaction.usage_session_id.in_(expired_sessions),
                    )
                )
            )
            purged = delete(UsageSession).where(UsageSession.id.in_(expired_sessions))
            deleted["usage_sessions"] = (await conn.execute(purged)).rowcount
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    return PurgeResult(deleted=deleted)


async def purge_periodically(
    database: Database, clock: Clock, policy: RuntimePolicy, *, interval_seconds: float = 86_400
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await purge_expired(database, clock.utc_now(), policy)
        except Exception:
            logger.exception("retention_purge_failed")
