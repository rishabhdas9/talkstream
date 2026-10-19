from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from app.costs.models import BudgetExceeded, CostScope, Usage, WorkBound
from app.costs.rates import PriceAdjustment, RateCard, UnitPrice
from app.costs.service import CostService
from app.runtime import RuntimePolicy
from app.voice.tools import ExaSearchClient, ToolExecutionError
from app.voice.tts import SarvamTtsClient, SarvamTtsError, TtsConfig


@pytest.fixture
async def paid_boundary(database, monkeypatch):
    from app.voice import tts as module

    now = datetime.now(UTC)
    clock = SimpleNamespace(utc_now=lambda: now, monotonic=time.monotonic)
    rates = RateCard(
        version="paid-fixture",
        captured_at=now,
        valid_until=now + timedelta(days=1),
        source="https://example.com/fixture",
        currency="INR",
        fx_micro_inr=1_000_000,
        qualified=True,
        taxes_and_fees=PriceAdjustment(numerator=1, denominator=1, note="Synthetic fixture"),
        unit_prices={
            "exa": {"fast": {"requests": UnitPrice(amount_micros=700_000, per_units=1)}},
            "sarvam": {
                "sarvam-105b": {
                    "input_tokens": UnitPrice(amount_micros=1, per_units=1),
                    "output_tokens": UnitPrice(amount_micros=2, per_units=1),
                },
                "bulbul:v3": {"characters": UnitPrice(amount_micros=3_000_000, per_units=1000)},
            },
        },
    )
    costs = CostService(
        database,
        clock,
        RuntimePolicy(session_micro_inr=5_000_000, user_day_micro_inr=10_000_000),
        rates,
    )
    scope = CostScope(user_id="user", session_id="call")
    await costs.reserve_envelope(scope, "envelope")
    async with database.engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO users VALUES ('user','fixture@example.test',:now,1)"),
            {"now": now.isoformat()},
        )
        await connection.execute(
            text(
                "INSERT INTO usage_sessions (id,user_id,started_at,billed_seconds,cost_paise) "
                "VALUES ('call','user',:now,0,0)"
            ),
            {"now": now.isoformat()},
        )
    sent = []
    received = asyncio.Event()
    submitted = asyncio.Event()
    state = SimpleNamespace(connects=0, final=True, hang_send=False, connect_error=False)

    class Connection:
        async def send(self, raw):
            message = json.loads(raw)
            sent.append(message)
            if message["type"] == "text":
                submitted.set()
                if state.hang_send:
                    await asyncio.Event().wait()
            if message["type"] == "flush":
                received.set()

        async def recv(self):
            await received.wait()
            if not state.final:
                await asyncio.Event().wait()
            return json.dumps({"type": "event", "data": {"event_type": "final"}})

        async def close(self, **kwargs):
            pass

    class Transport:
        async def __aenter__(self):
            async with database.engine.connect() as conn:
                assert (
                    await conn.execute(
                        text("SELECT COUNT(*) FROM cost_attempts WHERE state='reserved'")
                    )
                ).scalar_one() > 0
            state.connects += 1
            if state.connect_error:
                raise PermissionError("Invalid fixture credential")
            return Connection()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: Transport())
    client = SarvamTtsClient(
        TtsConfig("unused", "bulbul:v3", "shubh", "hi-IN", 24000, "linear16"),
        costs=costs,
        scope=scope,
        expires_at=now + timedelta(seconds=120),
    )

    async def noop(*args):
        pass

    async def generate(value):
        async def chunks():
            for item in value if isinstance(value, list) else [value]:
                yield item

        return [chunk async for chunk in client.synthesize(chunks(), observer=noop, probe=noop)]

    async def rows():
        async with database.engine.connect() as conn:
            return (
                (await conn.execute(text("SELECT * FROM cost_attempts ORDER BY created_at,id")))
                .mappings()
                .all()
            )

    yield SimpleNamespace(
        client=client,
        costs=costs,
        scope=scope,
        state=state,
        sent=sent,
        submitted=submitted,
        generate=generate,
        rows=rows,
    )
    await client.close_and_discard()
    await costs.release_envelope("envelope")


async def test_budget_denial_makes_no_external_request(paid_boundary):
    b = paid_boundary
    await b.costs.reserve(
        b.scope, "exhaust", "sarvam", "bulbul:v3", WorkBound(units={"characters": 1666})
    )
    await b.costs.settle("exhaust", Usage(units={"characters": 1666}), "succeeded")
    with pytest.raises(BudgetExceeded):
        await b.generate("hello")
    assert b.state.connects == 0 and b.sent == []


async def test_cancelled_tts_records_submitted_characters(paid_boundary):
    b = paid_boundary
    b.state.hang_send = True
    task = asyncio.create_task(b.generate("hello"))
    await asyncio.wait_for(b.submitted.wait(), 0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    attempts = await b.rows()
    assert len(attempts) == 1
    assert json.loads(attempts[0]["bound_json"])["units"]["characters"] == 5
    assert attempts[0]["outcome"] == "cancelled"
    assert attempts[0]["state"] == "unknown"
    assert attempts[0]["actual_micro_inr"] == attempts[0]["maximum_micro_inr"]


async def test_closing_paused_synthesis_awaits_real_provider_context(paid_boundary, monkeypatch):
    import base64

    from app.voice import tts as implementation

    b = paid_boundary
    allow_close = asyncio.Event()
    closed = asyncio.Event()

    class Connection:
        async def send(self, raw):
            pass

        async def recv(self):
            return json.dumps(
                {"type": "audio", "data": {"audio": base64.b64encode(b"audio").decode()}}
            )

    class Transport:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            await allow_close.wait()
            closed.set()

    monkeypatch.setattr(implementation, "connect", lambda *args, **kwargs: Transport())
    retained = []
    real_stream = b.client._stream

    def retain_stream(*args, **kwargs):
        stream = real_stream(*args, **kwargs)
        retained.append(stream)
        return stream

    monkeypatch.setattr(b.client, "_stream", retain_stream)

    async def chunks():
        yield "hello"

    async def noop(*args):
        pass

    outer = b.client.synthesize(chunks(), observer=noop, probe=noop)
    assert await anext(outer) == b"audio"
    closer = asyncio.create_task(outer.aclose())
    try:
        await asyncio.sleep(0.02)
        assert not closer.done() and not closed.is_set()
    finally:
        allow_close.set()
        await closer
        for stream in retained:
            await stream.aclose()
    assert closed.is_set() and (await b.rows())[0]["state"] == "unknown"


async def test_successful_tts_reserves_before_text_and_settles(paid_boundary):
    b = paid_boundary
    await b.generate("hello")
    rows = await b.rows()
    assert rows[0]["state"] == "settled"
    assert rows[0]["actual_micro_inr"] == 15_000
    assert b.state.connects == 1


async def test_tts_rejects_oversized_text_before_connect(paid_boundary):
    with pytest.raises(SarvamTtsError, match="1,000"):
        await paid_boundary.generate("x" * 1001)
    assert paid_boundary.state.connects == 0


async def test_tts_deadline_cancels_missing_completion(paid_boundary):
    from datetime import timedelta

    b = paid_boundary
    b.state.final = False
    b.client._expires_at = b.costs.clock.utc_now() + timedelta(seconds=0.02)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(b.generate("hello"), 0.5)
    assert time.monotonic() - started < 0.2
    rows = await b.rows()
    assert rows[0]["state"] == "unknown"


async def test_tts_reserves_each_chunk_and_bounds_indic_characters(paid_boundary):
    b = paid_boundary
    await b.generate(["hi", "हाय"])
    rows = await b.rows()
    assert len({r["id"] for r in rows}) == 2
    assert sorted(json.loads(r["bound_json"])["units"]["characters"] for r in rows) == [2, 9]
    assert sum(r["actual_micro_inr"] for r in rows) == 33_000


async def test_tts_stops_before_unfunded_next_chunk(paid_boundary):
    b = paid_boundary
    await b.costs.reserve(
        b.scope, "exhaust", "sarvam", "bulbul:v3", WorkBound(units={"characters": 1664})
    )
    await b.costs.settle("exhaust", Usage(units={"characters": 1664}), "succeeded")
    with pytest.raises(BudgetExceeded):
        await b.generate(["hi", "hello"])
    assert [m["data"]["text"] for m in b.sent if m["type"] == "text"] == ["hi"]
    rows = await b.rows()
    assert len(rows) == 2 and any(r["state"] == "unknown" for r in rows)


async def test_tts_invalid_credentials_retain_attempt(paid_boundary):
    b = paid_boundary
    b.state.connect_error = True
    with pytest.raises(PermissionError):
        await b.generate("hello")
    rows = await b.rows()
    assert len(rows) == 1 and rows[0]["state"] == "unknown"
    assert rows[0]["outcome"] == "failed"
    assert b.sent == []


async def test_empty_synthesis_does_not_reserve_or_connect(paid_boundary):
    await paid_boundary.generate("")
    assert await paid_boundary.rows() == []
    assert paid_boundary.state.connects == 0


async def test_synthesis_client_cannot_be_reused(paid_boundary):
    await paid_boundary.generate("hello")
    with pytest.raises(SarvamTtsError, match="already used"):
        await paid_boundary.generate("hello")
    assert paid_boundary.state.connects == 1


async def test_synthesis_cumulative_limit_prevents_next_submission(paid_boundary):
    b = paid_boundary
    with pytest.raises(SarvamTtsError, match="1,000"):
        await b.generate(["x" * 999, "hi"])
    assert [m["data"]["text"] for m in b.sent if m["type"] == "text"] == ["x" * 999]
    assert (await b.rows())[0]["state"] == "unknown"


@pytest.fixture
async def paid_search(paid_boundary):
    b = paid_boundary
    state = SimpleNamespace(calls=0, mode="ok", started=asyncio.Event())

    async def handler(request):
        state.calls += 1
        state.started.set()
        assert json.loads(request.content)["type"] == "fast"
        assert json.loads(request.content)["numResults"] <= 3
        rows = await b.rows()
        assert any(r["provider"] == "exa" and r["state"] == "reserved" for r in rows)
        if state.mode == "hang":
            await asyncio.Event().wait()
        if state.mode == "401":
            return httpx.Response(401)
        payload = {"results": [{"url": "https://example.test", "highlights": ["fact"]}]}
        if state.mode != "missing":
            payload["requestId"] = "exa-fixture-request"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ExaSearchClient(
            api_key="unused",
            timeout_seconds=0.03,
            costs=b.costs,
            scope=b.scope,
            expires_at=b.costs.clock.utc_now() + timedelta(seconds=120),
            http_client=http,
        )
        yield SimpleNamespace(client=client, state=state, boundary=b)


async def test_search_budget_denial_never_calls_http(paid_search):
    s = paid_search
    b = s.boundary
    await b.costs.reserve(
        b.scope, "exhaust", "sarvam", "bulbul:v3", WorkBound(units={"characters": 1666})
    )
    await b.costs.settle("exhaust", Usage(units={"characters": 1666}), "succeeded")
    with pytest.raises(BudgetExceeded):
        await s.client.search("latest news")
    assert s.state.calls == 0


@pytest.mark.parametrize(
    "mode,outcome", [("hang", "failed"), ("401", "failed"), ("missing", "unknown")]
)
async def test_uncertain_search_retains_bound(paid_search, mode, outcome):
    s = paid_search
    s.state.mode = mode
    if mode == "missing":
        assert await s.client.search("latest news")
    else:
        with pytest.raises((TimeoutError, httpx.HTTPStatusError)):
            await s.client.search("latest news")
    row = (await s.boundary.rows())[0]
    assert row["state"] == "unknown" and row["outcome"] == outcome
    assert row["maximum_micro_inr"] == 700_000


async def test_search_success_settles_one_fast_request(paid_search):
    s = paid_search
    assert (await s.client.search(" latest  news "))[0].snippet == "fact"
    row = (await s.boundary.rows())[0]
    assert row["state"] == "settled" and row["actual_micro_inr"] == 700_000
    assert "exa-fixture-request" in row["usage_json"]


async def test_cancelled_search_retains_distinct_attempt(paid_search):
    s = paid_search
    s.state.mode = "hang"
    task = asyncio.create_task(s.client.search("latest news"))
    await s.state.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = (await s.boundary.rows())[0]
    assert row["state"] == "unknown" and row["outcome"] == "cancelled"


@pytest.mark.parametrize("count", [0, 4, -1, True, 1.5])
async def test_search_rejects_unbounded_results_without_http(paid_search, count):
    with pytest.raises(ToolExecutionError):
        await paid_search.client.search("news", max_results=count)
    assert paid_search.state.calls == 0


async def test_search_deadline_rejects_before_http(paid_search):
    s = paid_search
    s.client._expires_at = s.boundary.costs.clock.utc_now() - timedelta(seconds=1)
    with pytest.raises(ToolExecutionError, match="deadline"):
        await s.client.search("news")
    assert s.state.calls == 0 and await s.boundary.rows() == []


async def test_missing_search_key_rejects_before_reservation(paid_search):
    s = paid_search
    s.client._api_key = None
    with pytest.raises(ToolExecutionError, match="EXA_API_KEY"):
        await s.client.search("news")
    assert s.state.calls == 0 and await s.boundary.rows() == []


async def test_tool_budget_failure_propagates_to_stop_generation():
    from app.voice.tools import execute_measured

    async def denied():
        raise BudgetExceeded("No allowance")

    with pytest.raises(BudgetExceeded):
        await execute_measured(name="web_search", call_id="call", operation=denied)


async def test_search_expiring_during_reservation_never_reaches_http(paid_search, monkeypatch):
    s = paid_search
    reserve = s.boundary.costs.reserve

    async def expires(*args, **kwargs):
        held = await reserve(*args, **kwargs)
        s.client._expires_at = s.boundary.costs.clock.utc_now() - timedelta(seconds=1)
        return held

    monkeypatch.setattr(s.boundary.costs, "reserve", expires)
    with pytest.raises(ToolExecutionError, match="deadline"):
        await s.client.search("news")
    assert s.state.calls == 0
    row = (await s.boundary.rows())[0]
    assert row["state"] == "settled" and row["actual_micro_inr"] == 0


async def test_tts_expiring_during_reservation_never_connects(paid_boundary, monkeypatch):
    b = paid_boundary
    reserve = b.costs.reserve

    async def expires(*args, **kwargs):
        held = await reserve(*args, **kwargs)
        b.client._expires_at = b.costs.clock.utc_now() - timedelta(seconds=1)
        return held

    monkeypatch.setattr(b.costs, "reserve", expires)
    with pytest.raises(SarvamTtsError, match="deadline"):
        await b.generate("hello")
    assert b.state.connects == 0
    row = (await b.rows())[0]
    assert row["state"] == "settled" and row["actual_micro_inr"] == 0
