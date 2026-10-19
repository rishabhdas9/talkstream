from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import asynccontextmanager
from functools import partial

import anyio

from app.costs.models import CostScope, Usage, WorkBound
from app.costs.service import CostService
from app.knowledge.embeddings import EmbeddingProviderError
from app.models import new_id
from app.voice.lifecycle import bounded_cleanup, retain_cleanup_task


class MeteredEmbeddingClient:
    def __init__(
        self,
        *,
        client,
        costs: CostService | None,
        scope: CostScope,
        provider_id: str,
        rate_provider_id: str | None = None,
        evaluation_cap_micro_inr: int | None = None,
    ):
        self.client = client
        self.costs = costs
        self.scope = scope
        self.provider_id = rate_provider_id or provider_id
        self.evaluation_cap_micro_inr = evaluation_cap_micro_inr

    async def embed(
        self, texts: Sequence[str], *, model_id: str, expected_dimensions: int
    ) -> list[list[float]]:
        if not texts:
            return []
        async with self.reserve_batches(
            [texts], model_id=model_id, expected_dimensions=expected_dimensions
        ) as requests:
            return await requests[0]()

    @asynccontextmanager
    async def reserve_batches(
        self, batches: Sequence[Sequence[str]], *, model_id: str, expected_dimensions: int
    ):
        if self.costs is None:
            raise EmbeddingProviderError("Embedding spending controls are unavailable")
        pending = set()
        closed = False

        async def run(attempt_id: str, texts: tuple[str, ...]):
            if closed or attempt_id not in pending:
                raise ValueError("Embedding request is no longer admitted")
            pending.remove(attempt_id)
            return await self._request(
                attempt_id, texts, model_id=model_id, expected_dimensions=expected_dimensions
            )

        async def release_unused():
            for attempt_id in pending:
                await self.costs.settle(attempt_id, Usage(units={"input_tokens": 0}), "cancelled")

        try:
            requests = []
            for batch in batches:
                texts = tuple(batch)
                attempt_id = new_id()
                # UTF-8 bytes bound text tokens; framing is reserved for every input.
                maximum_tokens = sum(len(value.encode("utf-8")) + 1024 for value in texts)
                await self.costs.reserve(
                    self.scope,
                    attempt_id,
                    self.provider_id,
                    model_id,
                    WorkBound(units={"input_tokens": maximum_tokens}),
                    evaluation_cap_micro_inr=self.evaluation_cap_micro_inr,
                )
                pending.add(attempt_id)
                requests.append(partial(run, attempt_id, texts))
            yield requests
        finally:
            closed = True
            if pending:
                # These bounded local refunds must finish; a voice timeout would abandon holds.
                refund = asyncio.create_task(release_unused(), name="embedding-unused-refunds")
                retain_cleanup_task(refund)
                cancelled = False
                with anyio.CancelScope(shield=True):
                    while True:
                        try:
                            await asyncio.shield(refund)
                            break
                        except asyncio.CancelledError:
                            if refund.cancelled():
                                raise
                            cancelled = True
                if cancelled:
                    raise asyncio.CancelledError

    async def _request(
        self, attempt_id: str, texts: Sequence[str], *, model_id: str, expected_dimensions: int
    ) -> list[list[float]]:
        usage = None
        outcome = "failed"

        def report(tokens: int) -> None:
            nonlocal usage
            usage = Usage(units={"input_tokens": tokens})

        try:
            vectors = await self.client.embed(
                texts,
                model_id=model_id,
                expected_dimensions=expected_dimensions,
                usage_callback=report,
            )
            outcome = "succeeded" if usage is not None else "unknown"
            return vectors
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await bounded_cleanup(self.costs.settle(attempt_id, usage, outcome))
