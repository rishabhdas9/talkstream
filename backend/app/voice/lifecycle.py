from __future__ import annotations

import asyncio
import fcntl
import os
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import anyio
from sqlalchemy import text

from app.costs.service import CostService
from app.db import Database
from app.runtime import Clock, RuntimePolicy


class DatabaseOwnedError(RuntimeError):
    pass


@contextmanager
def database_owner(database: Database):
    name = database.engine.url.database
    if not name or name == ":memory:":
        raise RuntimeError("Talkstream requires a file-backed SQLite database")
    if name.startswith("file:") or "uri" in database.engine.url.query:
        raise RuntimeError("SQLite URI databases are not supported by the runtime owner lock")
    with owner_lock(Path(name)):
        yield


@contextmanager
def owner_lock(path: Path):
    """Exclusive runtime ownership of one SQLite file; restore takes the same lock."""
    path = path.resolve()
    with os.fdopen(os.open(f"{path}-owner.lock", os.O_CREAT | os.O_RDWR, 0o600), "a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DatabaseOwnedError(
                "Talkstream database is already owned by another runtime"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.utcoffset() is None else parsed.astimezone(UTC)


async def recover_abandoned_sessions(database: Database, clock: Clock, costs: CostService) -> int:
    if costs.database is not database:
        raise ValueError("Recovery must own the same cost database")
    async with costs.transaction() as conn:
        sessions = (
            (
                await conn.execute(
                    text(
                        "SELECT id,started_at,ready_at,billed_seconds FROM usage_sessions "
                        "WHERE ended_at IS NULL"
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in sessions:
            start = _utc(row["started_at"])
            anchor = _utc(row["ready_at"]) if row["ready_at"] else start
            last_tick = anchor + timedelta(seconds=row["billed_seconds"])
            end = max(start, min(last_tick, clock.utc_now()))
            await conn.execute(
                text(
                    "UPDATE usage_sessions SET ended_at=:ended,end_reason='process_restart' "
                    "WHERE id=:id"
                ),
                {"ended": end.isoformat(), "id": row["id"]},
            )
        # A dead owner cannot confirm whether its reserved request reached the provider.
        # Older unknown rows predate charging at the full bound and still hold budget.
        pending = (
            await conn.execute(
                text(
                    "SELECT id,outcome FROM cost_attempts WHERE state='reserved'"
                    " OR (state='unknown' AND actual_micro_inr IS NULL)"
                )
            )
        ).all()
        for attempt_id, outcome in pending:
            await costs.settle(attempt_id, None, outcome or "unknown", connection=conn)
        envelopes = (
            (await conn.execute(text("SELECT id FROM cost_envelopes WHERE state='active'")))
            .scalars()
            .all()
        )
        for envelope_id in envelopes:
            await costs.release_envelope(envelope_id, connection=conn)
        await conn.execute(text("UPDATE call_leases SET state='released' WHERE state='active'"))
    return len(sessions)


class SessionCleanupError(RuntimeError):
    pass


T = TypeVar("T")
_pending_cleanup: set[asyncio.Future] = set()


def retain_cleanup_task(task: asyncio.Future) -> None:
    _pending_cleanup.add(task)

    def finished(done: asyncio.Future) -> None:
        _pending_cleanup.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)


async def bounded_cleanup(
    work: Awaitable[T],
    *,
    timeout_seconds: float = 1,
    supervised_tasks: set[asyncio.Future] | None = None,
) -> T:

    task = asyncio.ensure_future(work)
    if supervised_tasks is not None:
        supervised_tasks.add(task)
        task.add_done_callback(supervised_tasks.discard)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
        if not done:
            raise SessionCleanupError("Call cleanup timed out")
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            retain_cleanup_task(task)


class SessionLifecycle:
    def __init__(self, finalize: Callable[[str], Awaitable[None]], *, timeout_seconds: float = 10):
        self._finalize = finalize
        self._timeout_seconds = timeout_seconds
        self._finish_task: asyncio.Task[None] | None = None

    async def _run(self, reason: str) -> None:

        with anyio.CancelScope(shield=True):
            await bounded_cleanup(self._finalize(reason), timeout_seconds=self._timeout_seconds)

    async def finish(self, reason: str) -> None:
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(self._run(reason), name="voice-finalization")
        cancelled = False
        with anyio.CancelScope(shield=True):
            while True:
                try:
                    await asyncio.shield(self._finish_task)
                    break
                except asyncio.CancelledError:
                    if self._finish_task.cancelled():
                        raise
                    cancelled = True
        if cancelled:
            raise asyncio.CancelledError


class TurnDeadlineExceeded(RuntimeError):
    def __init__(self, stage: str):
        self.stage = stage
        super().__init__(f"Voice stage timed out: {stage}")


class TurnDeadline:
    def __init__(
        self,
        policy: RuntimePolicy,
        clock: Clock,
        supervised_tasks: set[asyncio.Future] | None = None,
    ):
        self._clock = clock
        self._limits = {
            "initialization": policy.initialization_seconds,
            "stt_final": policy.stt_final_seconds,
            "first_answer": policy.first_answer_seconds,
            "tts_progress": policy.tts_progress_seconds,
            "turn_work": policy.turn_work_seconds,
        }
        self._expires: dict[str, float] = {}
        self._changed = asyncio.Event()
        self._tasks = supervised_tasks if supervised_tasks is not None else set()

    def arm(self, stage: str, *, started_at: float | None = None) -> None:
        start = self._clock.monotonic() if started_at is None else started_at
        self._expires.setdefault(stage, start + self._limits[stage])
        self._changed.set()

    def complete(self, stage: str) -> None:
        self._expires.pop(stage, None)
        self._changed.set()

    def progress(self, stage: str) -> None:
        self.complete(stage)
        self.arm(stage)

    async def run(self, stage: str, work: Awaitable[T]) -> T:
        self.arm(stage)
        try:
            return await self.monitor(work)
        finally:
            self.complete(stage)

    async def monitor(self, work: Awaitable[T]) -> T:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        changed = None
        try:
            while True:
                self._changed.clear()
                remaining = None
                if self._expires:
                    stage = min(self._expires, key=self._expires.__getitem__)
                    remaining = self._expires[stage] - self._clock.monotonic()
                    if remaining <= 0:
                        raise TurnDeadlineExceeded(stage)
                if task.done():
                    return task.result()
                changed = asyncio.create_task(self._changed.wait())
                done, _ = await asyncio.wait(
                    {task, changed}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    raise TurnDeadlineExceeded(stage)
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
        finally:
            if changed is not None:
                changed.cancel()
            if not task.done():
                task.cancel()
                with anyio.CancelScope(shield=True):
                    try:
                        _, pending = await asyncio.wait({task}, timeout=1)
                    finally:
                        if not task.done():
                            retain_cleanup_task(task)
                if pending:
                    raise SessionCleanupError("Timed-out voice work did not stop")
            if not task.cancelled():
                task.exception()
