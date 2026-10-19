from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db import Database
from app.models import User, new_id
from app.runtime import Clock, RuntimePolicy


@dataclass(frozen=True)
class TicketGrant:
    user_id: str
    session_id: str
    access_expires_at: datetime


class TicketService:
    def __init__(self, database: Database, clock: Clock, policy: RuntimePolicy):
        self.database = database
        self.clock = clock
        self.policy = policy

    async def issue(self, user: User, *, access_expires_at: datetime) -> str:
        now = self.clock.utc_now().astimezone(UTC)
        deadline = access_expires_at.astimezone(UTC)
        if deadline <= now:
            raise ValueError("Expired access token")
        ticket = secrets.token_urlsafe(32)
        async with self.database.engine.begin() as connection:
            # Only one pending ticket per user; clean up expired reservations on issuance.
            await connection.execute(
                text("DELETE FROM voice_tickets WHERE user_id=:user OR expires_at<=:now"),
                {"user": user.id, "now": now.isoformat(timespec="microseconds")},
            )
            await connection.execute(
                text(
                    "INSERT INTO voice_tickets "
                    "(ticket_hash,user_id,email,session_id,expires_at,access_expires_at) "
                    "VALUES (:hash,:user,:email,:session,:expiry,:deadline)"
                ),
                {
                    "hash": hashlib.sha256(ticket.encode()).hexdigest(),
                    "user": user.id,
                    "email": user.email,
                    "session": new_id(),
                    "expiry": min(
                        now + timedelta(seconds=self.policy.ticket_ttl_seconds), deadline
                    ).isoformat(timespec="microseconds"),
                    "deadline": deadline.isoformat(timespec="microseconds"),
                },
            )
        return ticket

    async def consume(self, ticket: str) -> TicketGrant | None:
        if not 32 <= len(ticket) <= 128:
            return None
        now = self.clock.utc_now().astimezone(UTC).isoformat(timespec="microseconds")
        async with self.database.engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "DELETE FROM voice_tickets WHERE ticket_hash=:hash "
                            "AND expires_at>:now AND access_expires_at>:now "
                            "AND EXISTS (SELECT 1 FROM users WHERE users.id=voice_tickets.user_id "
                            "AND users.email=voice_tickets.email AND users.is_verified=1) "
                            "RETURNING user_id,session_id,access_expires_at"
                        ),
                        {"hash": hashlib.sha256(ticket.encode()).hexdigest(), "now": now},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return TicketGrant(
            row["user_id"], row["session_id"], datetime.fromisoformat(row["access_expires_at"])
        )
