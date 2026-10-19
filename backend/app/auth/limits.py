from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import new_id
from app.runtime import RuntimePolicy


class OtpRateLimited(ValueError):
    pass


async def reserve_otp_send(
    session: AsyncSession,
    *,
    email: str,
    ip: str,
    now: datetime,
    policy: RuntimePolicy,
    pepper: str,
) -> None:
    """Reserve within the caller's immediate transaction, before changing a challenge."""
    current = now.astimezone(UTC)
    local_day = current.astimezone(ZoneInfo("Asia/Kolkata")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day_start = local_day.astimezone(UTC)
    hour_start = current - timedelta(hours=1)
    cooldown_start = current - timedelta(seconds=policy.otp_cooldown_seconds)

    def digest(kind: str, value: str) -> str:
        return hmac.new(
            pepper.encode(), f"otp-rate:{kind}:{value}".encode(), hashlib.sha256
        ).hexdigest()

    values = {
        "email": digest("email", email),
        "ip": digest("ip", ip),
        "hour": hour_start.isoformat(timespec="microseconds"),
        "cooldown": cooldown_start.isoformat(timespec="microseconds"),
        "day": day_start.isoformat(timespec="microseconds"),
        "oldest": min(hour_start, cooldown_start, day_start).isoformat(timespec="microseconds"),
    }
    counts = (
        await session.execute(
            text(
                "SELECT "
                "COALESCE(SUM(email_hash=:email AND sent_at>:cooldown),0), "
                "COALESCE(SUM(email_hash=:email AND sent_at>:hour),0), "
                "COALESCE(SUM(ip_hash=:ip AND sent_at>:hour),0), "
                "COALESCE(SUM(sent_at>=:day),0) "
                "FROM otp_send_attempts WHERE sent_at>=:oldest"
            ),
            values,
        )
    ).one()
    if (
        counts[0]
        or counts[1] >= policy.otp_email_per_hour
        or counts[2] >= policy.otp_ip_per_hour
        or counts[3] >= policy.otp_global_per_day
    ):
        raise OtpRateLimited("Too many requests. Please try again later.")
    await session.execute(
        text(
            "INSERT INTO otp_send_attempts (id,email_hash,ip_hash,sent_at) "
            "VALUES (:id,:email,:ip,:sent_at)"
        ),
        {**values, "id": new_id(), "sent_at": current.isoformat(timespec="microseconds")},
    )
