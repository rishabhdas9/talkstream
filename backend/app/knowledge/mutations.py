from __future__ import annotations

import asyncio
from functools import wraps

from sqlalchemy import text


class KnowledgeMutationConflictError(RuntimeError):
    pass


class KnowledgeDeletionPendingError(RuntimeError):
    pass


DELETION_PENDING_MESSAGE = "Source unavailable. Deletion cleanup is pending; retry deletion."


def user_mutation(method):
    @wraps(method)
    async def guarded(self, *, user_id, **kwargs):
        task = asyncio.current_task()
        if self._mutation_owners.get(user_id) is task:
            # File extraction calls text indexing in the same mutation.
            return await method(self, user_id=user_id, **kwargs)
        lock = self._mutation_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            raise KnowledgeMutationConflictError("A knowledge mutation is already in progress")
        async with lock:
            self._mutation_owners[user_id] = task
            try:
                return await method(self, user_id=user_id, **kwargs)
            finally:
                self._mutation_owners.pop(user_id, None)
                self._mutation_locks.pop(user_id, None)

    return guarded


async def ensure_mutation_available(
    session, user_id: str, *, deleting_source: str = "", include_ingestions: bool = True
):
    busy = await session.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM knowledge_reindex_jobs WHERE user_id=:user "
            "AND status IN ('pending','running')) OR (:ingestions AND EXISTS(SELECT 1 FROM "
            "knowledge_ingestions WHERE user_id=:user AND state IN "
            "('reserved','started','stored'))) OR EXISTS(SELECT 1 FROM knowledge_sources "
            "WHERE user_id=:user AND ((:ingestions AND status='processing') OR "
            "(status='deleting' AND id!=:source)))"
        ),
        {"user": user_id, "source": deleting_source, "ingestions": include_ingestions},
    )
    if busy:
        raise KnowledgeMutationConflictError("A knowledge mutation is already in progress")
