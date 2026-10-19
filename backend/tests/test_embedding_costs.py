from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from test_cost_service import clock as clock
from test_cost_service import rates as rates

from app.costs.models import BudgetExceeded, CostScope, WorkBound
from app.costs.rates import RateUnavailable, UnitPrice
from app.costs.service import CostService
from app.knowledge.embeddings import EmbeddingClientFactory, EmbeddingProviderError
from app.knowledge.metering import MeteredEmbeddingClient
from app.runtime import RuntimePolicy

MODEL = "text-embedding-3-small"


@pytest.fixture
def embedding_rates(rates):
    return rates.model_copy(
        update={
            "unit_prices": {
                provider: {
                    model: {"input_tokens": UnitPrice(amount_micros=1, per_units=1)}
                    for model in models
                }
                for provider, models in {
                    "openai": [MODEL, "text-embedding-3-large"],
                    "gemini": ["gemini-embedding-2"],
                    "openrouter:nebius": ["qwen/qwen3-embedding-8b"],
                }.items()
            }
        }
    )


@pytest.fixture
def embedding_costs(database, clock, embedding_rates):
    return CostService(
        database,
        clock,
        RuntimePolicy(session_micro_inr=5_000_000, user_day_micro_inr=10_000_000),
        embedding_rates,
    )


@pytest.fixture
async def embedding_rig(database, embedding_costs):
    state = SimpleNamespace(requests=0, tokens=4, malformed=False, block=False)
    started = asyncio.Event()
    scope = CostScope(user_id="u", job_id="embedding-job")

    async def handler(request):
        async with database.engine.connect() as conn:
            assert (
                await conn.execute(
                    text("SELECT COUNT(*) FROM cost_attempts WHERE state='reserved'")
                )
            ).scalar_one() > 0
        state.requests += 1
        started.set()
        if state.block:
            await asyncio.Event().wait()
        body = {"data": [{"index": 0, "embedding": [False] if state.malformed else [0.1] * 1536}]}
        if state.tokens is not None:
            body["usage"] = {"prompt_tokens": state.tokens}
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MeteredEmbeddingClient(
            client=EmbeddingClientFactory(http_client=http).client_for("openai", "private-key"),
            costs=embedding_costs,
            scope=scope,
            provider_id="openai",
        )
        yield SimpleNamespace(
            client=client, scope=scope, state=state, started=started, costs=embedding_costs
        )


async def embed(rig):
    return await rig.client.embed(["document"], model_id=MODEL, expected_dimensions=1536)


async def test_embedding_reserves_before_http_and_settles_real_usage(embedding_rig):
    assert len((await embed(embedding_rig))[0]) == 1536
    assert embedding_rig.state.requests == 1
    assert await embedding_rig.costs.spent_micro_inr() == 4
    assert await embedding_rig.costs.outstanding_micro_inr(embedding_rig.scope) == 0


async def test_embedding_budget_denial_skips_http(embedding_rig):
    await embedding_rig.costs.reserve(
        embedding_rig.scope,
        "exhaust",
        "openai",
        MODEL,
        WorkBound(units={"input_tokens": 10_000_000}),
    )
    with pytest.raises(BudgetExceeded):
        await embed(embedding_rig)
    assert embedding_rig.state.requests == 0


async def test_unqualified_rates_skip_http(embedding_rig):
    embedding_rig.client.costs = CostService(
        embedding_rig.costs.database, embedding_rig.costs.clock, RuntimePolicy(), None
    )
    with pytest.raises(RateUnavailable):
        await embed(embedding_rig)
    assert embedding_rig.state.requests == 0


async def test_missing_usage_charges_the_full_bound(embedding_rig):
    embedding_rig.state.tokens = None
    await embed(embedding_rig)
    bound = await embedding_rig.costs.outstanding_micro_inr(embedding_rig.scope)
    assert await embedding_rig.costs.spent_micro_inr() == bound > 0


async def test_malformed_vectors_still_account_reported_usage(embedding_rig):
    embedding_rig.state.malformed = True
    with pytest.raises(EmbeddingProviderError):
        await embed(embedding_rig)
    assert await embedding_rig.costs.spent_micro_inr() == 4


async def test_cancelled_embedding_retains_unknown_remote_hold(embedding_rig):
    embedding_rig.state.block = True
    work = asyncio.create_task(embed(embedding_rig))
    await embedding_rig.started.wait()
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert await embedding_rig.costs.outstanding_micro_inr(embedding_rig.scope) > 0


async def test_embedding_call_scope_cannot_bypass_session_envelope(embedding_rig):
    embedding_rig.client.scope = CostScope(user_id="u", session_id="call")
    with pytest.raises(BudgetExceeded, match="envelope"):
        await embed(embedding_rig)
    assert embedding_rig.state.requests == 0


async def test_live_probe_cap_is_cumulative_and_atomic(embedding_costs):
    bound = WorkBound(units={"input_tokens": 7})

    async def reserve(index):
        try:
            return await embedding_costs.reserve(
                CostScope(user_id="u", job_id=f"live-eval:{index}"),
                f"probe-{index}",
                "openai",
                MODEL,
                bound,
                evaluation_cap_micro_inr=10,
            )
        except BudgetExceeded:
            return None

    results = await asyncio.gather(reserve(1), reserve(2))
    assert sum(item is not None for item in results) == 1


async def test_production_service_profiles_and_empty_search_do_not_bypass_metering(
    database, settings, embedding_costs
):
    from app.knowledge.service import EmbeddingProviderUnavailableError, KnowledgeService
    from app.models import User

    async with database.session_factory() as session:
        session.add(User(id="u", email="u@example.com"))
        await session.commit()
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"usage": {"prompt_tokens": 5}, "data": [{"index": 0, "embedding": [0.1] * 1536}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        enabled = settings.model_copy(update={"openai_api_key": "env-key"})
        service = KnowledgeService(
            database=database,
            settings=enabled,
            embedding_client=None,
            client_factory=EmbeddingClientFactory(http_client=http),
            costs=embedding_costs,
        )
        assert await service.search_knowledge(user_id="u", query="refund", limit=4) == []
        with pytest.raises(EmbeddingProviderUnavailableError, match="evaluation allowance"):
            await service.test_profile("u", "openai", MODEL)
        assert requests == []
        service.settings = enabled.model_copy(update={"live_evaluation_cap_micro_inr": 10_000})
        assert await service.test_profile("u", "openai", MODEL) == 1536
        assert await embedding_costs.spent_micro_inr() == 5
        assert len(requests) == 1
        async with database.engine.connect() as conn:
            row = (await conn.execute(text("SELECT job_id,user_id FROM cost_attempts"))).one()
        assert row.user_id == "u" and row.job_id.startswith("live-eval:")
        with pytest.raises(ValueError, match="different user"):
            await service._client_for(
                "u", "openai", cost_scope=CostScope(user_id="other", session_id="call")
            )
        assert len(requests) == 1


async def test_actual_addon_has_shared_cost_service(settings):
    from app.main import create_app

    app = create_app(settings.model_copy(update={"knowledge_rag_enabled": True}))
    async with app.router.lifespan_context(app):
        assert app.state.knowledge_service.costs is app.state.cost_service


async def test_ingestion_reindex_and_voice_search_share_real_quota_ledger(
    database, settings, embedding_costs
):
    import json

    from cryptography.fernet import Fernet
    from test_knowledge_sources_api import FakeVectorStore

    from app.knowledge.service import KnowledgeService
    from app.models import User

    async with database.session_factory() as session:
        session.add(User(id="u", email="u@example.com"))
        await session.commit()
    requests = []

    async def handler(request):
        assert request.headers["authorization"] == "Bearer user-owned-key"
        requests.append(request)
        body = json.loads(request.content)
        dimensions = 1536 if body["model"] == MODEL else 3072
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 5 * len(body["input"])},
                "data": [
                    {"index": i, "embedding": [0.1] * dimensions}
                    for i, _ in enumerate(body["input"])
                ],
            },
        )

    enabled = settings.model_copy(
        update={
            "openai_api_key": "env-key",
            "credential_encryption_key": Fernet.generate_key().decode(),
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = KnowledgeService(
            database=database,
            settings=enabled,
            embedding_client=None,
            client_factory=EmbeddingClientFactory(http_client=http),
            costs=embedding_costs,
            vector_store=FakeVectorStore(),
        )
        await service.set_provider_key("u", "openai", "user-owned-key")
        await service.save_profile(user_id="u", provider_id="openai", model_id=MODEL)
        source = await service.add_text_source(
            user_id="u", name="Refund", text="Refunds take five days."
        )
        call = CostScope(user_id="u", session_id="voice-call")
        with pytest.raises(BudgetExceeded):
            await service.search_knowledge(user_id="u", query="refund", limit=4, cost_scope=call)
        assert len(requests) == 1
        await embedding_costs.reserve_envelope(call, "voice-envelope")
        results = await service.search_knowledge(
            user_id="u", query="refund", limit=4, cost_scope=call
        )
        assert results[0].source_name == "Refund"
        profile = await service.save_profile(
            user_id="u", provider_id="openai", model_id="text-embedding-3-large"
        )
        job = await service.get_reindex_job(profile.id)
        await service.run_pending_jobs()
        async with database.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT user_id,session_id,job_id,state FROM cost_attempts "
                            "ORDER BY created_at"
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert len(requests) == len(rows) == 3
        assert all(row["user_id"] == "u" and row["state"] == "settled" for row in rows)
        assert {row["job_id"] for row in rows} == {None, source.id, job.id}
        assert sum(row["session_id"] == "voice-call" for row in rows) == 1
        assert await embedding_costs.spent_micro_inr() == 15


async def test_all_batches_are_reserved_before_any_http(embedding_rig):
    rig = embedding_rig
    async with rig.client.reserve_batches(
        [["first"], ["second"]], model_id=MODEL, expected_dimensions=1536
    ) as requests:
        assert rig.state.requests == 0
        async with rig.costs.database.engine.connect() as conn:
            assert (
                await conn.execute(
                    text("SELECT COUNT(*) FROM cost_attempts WHERE state='reserved'")
                )
            ).scalar_one() == 2
        await requests[0]()
        await requests[1]()
    assert rig.state.requests == 2
    assert await rig.costs.spent_micro_inr() == 8


async def test_batch_admission_failure_releases_only_unused_reservations(embedding_rig):
    rig = embedding_rig
    held = 10_000_000 - 1500
    await rig.costs.reserve(
        rig.scope, "previous", "openai", MODEL, WorkBound(units={"input_tokens": held})
    )
    with pytest.raises(BudgetExceeded):
        async with rig.client.reserve_batches(
            [["first"], ["second"]], model_id=MODEL, expected_dimensions=1536
        ):
            raise AssertionError("The whole job cannot fit")
    assert rig.state.requests == 0
    assert await rig.costs.outstanding_micro_inr(rig.scope) == held


async def test_unissued_batches_release_after_unknown_remote_first_batch(embedding_rig):
    rig = embedding_rig
    rig.state.tokens = None
    with pytest.raises(RuntimeError, match="stop"):
        async with rig.client.reserve_batches(
            [["first"], ["second"]], model_id=MODEL, expected_dimensions=1536
        ) as requests:
            await requests[0]()
            raise RuntimeError("stop")
    assert rig.state.requests == 1
    async with rig.costs.database.engine.connect() as conn:
        states = (
            await conn.execute(
                text(
                    "SELECT state,actual_micro_inr,maximum_micro_inr FROM cost_attempts"
                    " ORDER BY created_at"
                )
            )
        ).all()
    (state, charged, bound), settled = states
    assert state == "unknown" and charged == bound and tuple(settled[:2]) == ("settled", 0)
    assert await rig.costs.outstanding_micro_inr(rig.scope) > 0


async def test_reserved_request_cannot_run_twice_or_after_context(embedding_rig):
    rig = embedding_rig
    async with rig.client.reserve_batches(
        [["first"], ["second"]], model_id=MODEL, expected_dimensions=1536
    ) as requests:
        await requests[0]()
        with pytest.raises(ValueError):
            await requests[0]()
    with pytest.raises(ValueError):
        await requests[1]()
    assert rig.state.requests == 1


async def test_actual_ingestion_admits_whole_job_before_http(database, settings, embedding_costs):
    from test_knowledge_sources_api import FakeVectorStore

    from app.knowledge.service import KnowledgeService
    from app.models import User

    async with database.session_factory() as session:
        session.add(User(id="u", email="u@example.com"))
        await session.commit()
    requests = []

    async def handler(request):
        requests.append(request)
        raise AssertionError("Whole job is unaffordable; HTTP must never start")

    held = 10_000_000 - 1500
    previous = CostScope(user_id="u", job_id="previous")
    await embedding_costs.reserve(
        previous, "previous", "openai", MODEL, WorkBound(units={"input_tokens": held})
    )
    enabled = settings.model_copy(
        update={
            "openai_api_key": "private-key",
            "knowledge_chunk_characters": 100,
            "knowledge_chunk_overlap": 0,
            "knowledge_embedding_batch_size": 1,
        }
    )
    vectors = FakeVectorStore()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = KnowledgeService(
            database=database,
            settings=enabled,
            embedding_client=None,
            client_factory=EmbeddingClientFactory(http_client=http),
            costs=embedding_costs,
            vector_store=vectors,
        )
        await service.save_profile(user_id="u", provider_id="openai", model_id=MODEL)
        with pytest.raises(BudgetExceeded):
            await service.add_text_source(user_id="u", name="Two chunks", text="word " * 30)
        assert requests == [] and vectors.points == {}
        assert await embedding_costs.outstanding_micro_inr(previous) == held
        sources = await service.list_sources("u")
        assert sources[0].status.value == "failed"


async def test_slow_unused_refunds_finish_and_preserve_original_failure(embedding_rig, monkeypatch):
    rig = embedding_rig
    settle = rig.costs.settle

    async def slow_settle(*args, **kwargs):
        await asyncio.sleep(0.55)
        await settle(*args, **kwargs)

    monkeypatch.setattr(rig.costs, "settle", slow_settle)
    with pytest.raises(BudgetExceeded, match="original-admission-failure"):
        async with rig.client.reserve_batches(
            [["first"], ["second"], ["third"]], model_id=MODEL, expected_dimensions=1536
        ):
            raise BudgetExceeded("original-admission-failure")
    assert rig.state.requests == 0
    assert await rig.costs.outstanding_micro_inr(rig.scope) == 0


async def test_cancellation_during_unused_refunds_cannot_abandon_them(embedding_rig, monkeypatch):
    rig = embedding_rig
    settle = rig.costs.settle
    entered = asyncio.Event()

    async def slow_settle(*args, **kwargs):
        entered.set()
        await asyncio.sleep(0.05)
        await settle(*args, **kwargs)

    monkeypatch.setattr(rig.costs, "settle", slow_settle)

    async def abandon():
        async with rig.client.reserve_batches(
            [["first"], ["second"]], model_id=MODEL, expected_dimensions=1536
        ):
            pass

    work = asyncio.create_task(abandon())
    await entered.wait()
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert rig.state.requests == 0
    assert await rig.costs.outstanding_micro_inr(rig.scope) == 0


async def test_unaffordable_reindex_makes_no_requests_and_keeps_old_generation(
    database, settings, embedding_costs
):
    import json

    from test_knowledge_sources_api import FakeVectorStore

    from app.knowledge.service import KnowledgeService
    from app.models import User

    async with database.session_factory() as session:
        session.add(User(id="u", email="u@example.com"))
        await session.commit()
    models = []

    async def handler(request):
        body = json.loads(request.content)
        models.append(body["model"])
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 5},
                "data": [{"index": 0, "embedding": [0.1] * 1536}],
            },
        )

    enabled = settings.model_copy(
        update={
            "openai_api_key": "private-key",
            "knowledge_chunk_characters": 100,
            "knowledge_chunk_overlap": 0,
            "knowledge_embedding_batch_size": 1,
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = KnowledgeService(
            database=database,
            settings=enabled,
            embedding_client=None,
            client_factory=EmbeddingClientFactory(http_client=http),
            costs=embedding_costs,
            vector_store=FakeVectorStore(),
        )
        old = await service.save_profile(user_id="u", provider_id="openai", model_id=MODEL)
        await service.add_text_source(user_id="u", name="Three chunks", text="word " * 59)
        assert models == [MODEL] * 3
        # Leave enough for one reindex batch but not all three.
        held = 10_000_000 - await embedding_costs.spent_micro_inr() - 2000
        await embedding_costs.reserve(
            CostScope(user_id="u", job_id="previous"),
            "previous",
            "openai",
            MODEL,
            WorkBound(units={"input_tokens": held}),
        )
        pending = await service.save_profile(
            user_id="u", provider_id="openai", model_id="text-embedding-3-large"
        )
        await service.run_pending_jobs()
        assert models == [MODEL] * 3
        assert (await service.get_reindex_job(pending.id)).status.value == "failed"
        assert (await service.get_active_profile("u")).id == old.id
