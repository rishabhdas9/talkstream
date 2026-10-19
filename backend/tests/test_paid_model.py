from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from langchain.messages import AIMessageChunk
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from test_paid_boundaries import paid_boundary as paid_boundary
from test_paid_boundaries import paid_search as paid_search

from app.costs.models import BudgetExceeded, Usage, WorkBound
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyContext, PolicyDocument
from app.voice.agent import AgentRunner


@pytest.fixture
def policy_document():
    return PolicyDocument.load()


async def inspect_policy(subject, boundary, policy):
    context = PolicyContext(
        user_id=boundary.scope.user_id,
        session_id=boundary.scope.session_id,
        turn_id="policy-turn",
        policy_version=policy.version,
    )

    async def classify(stage, text, context):
        return await subject.classify_policy(stage, text, context, policy=policy)

    async def emit(event):
        pass

    engine = PolicyEngine(
        policy=policy, classifier=classify, event_sink=emit, clock=boundary.costs.clock
    )
    return await engine.inspect("input", "Explain phishing prevention.", context)


async def test_policy_classifier_reserves_and_settles_actual_usage(paid_boundary, policy_document):
    model = PaidFakeModel(responses=['{"allowed":true,"category":"allowed"}'])
    assert (
        await inspect_policy(runner(paid_boundary, model), paid_boundary, policy_document)
    ).allowed
    rows = await paid_boundary.rows()
    assert len(rows) == 1 and rows[0]["state"] == "settled"
    assert rows[0]["actual_micro_inr"] == 20
    assert '"stage": "input"' in model.seen[0][-1].content
    assert not any(message.type == "tool" for message in model.seen[0])


@pytest.mark.parametrize(
    "options", [{"report_usage": False}, {"failures": 1}, {"fail_after_text": True}]
)
async def test_policy_classifier_preserves_uncertain_cost_and_never_retries(
    paid_boundary, policy_document, options
):
    model = PaidFakeModel(responses=['{"allowed":true,"category":"allowed"}'], **options)
    result = await inspect_policy(runner(paid_boundary, model), paid_boundary, policy_document)
    assert result.allowed is (not options.get("failures") and not options.get("fail_after_text"))
    assert model.calls == 1
    assert (await paid_boundary.rows())[0]["state"] == "unknown"


async def test_policy_classifier_cancellation_retains_paid_hold(paid_boundary, policy_document):
    model = PaidFakeModel(responses=['{"allowed":true,"category":"allowed"}'], sleep=1)
    task = asyncio.create_task(
        inspect_policy(runner(paid_boundary, model), paid_boundary, policy_document)
    )
    for _ in range(100):
        if model.calls:
            break
        await asyncio.sleep(0.001)
    assert model.calls == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await paid_boundary.rows())[0]["state"] == "unknown"


async def test_policy_classifier_deadline_and_wrong_owner_prevent_work(
    paid_boundary, policy_document
):
    model = PaidFakeModel(responses=['{"allowed":true,"category":"allowed"}'])
    subject = runner(paid_boundary, model)
    subject._middleware.monotonic_deadline = -1
    assert not (await inspect_policy(subject, paid_boundary, policy_document)).allowed
    assert model.calls == 0 and await paid_boundary.rows() == []
    context = PolicyContext(
        user_id="other",
        session_id=paid_boundary.scope.session_id,
        turn_id="policy-turn",
        policy_version="v1",
    )
    with pytest.raises(ValueError):
        await subject.classify_policy("input", "Hello", context, policy=policy_document)
    assert model.calls == 0 and await paid_boundary.rows() == []


async def test_policy_classifier_budget_denial_prevents_work(paid_boundary, policy_document):
    b = paid_boundary
    await b.costs.reserve(
        b.scope,
        "exhaust",
        "sarvam",
        "sarvam-105b",
        WorkBound(units={"input_tokens": 5_000_000, "output_tokens": 0}),
    )
    await b.costs.settle(
        "exhaust", Usage(units={"input_tokens": 5_000_000, "output_tokens": 0}), "succeeded"
    )
    model = PaidFakeModel(responses=['{"allowed":true,"category":"allowed"}'])
    with pytest.raises(BudgetExceeded):
        await inspect_policy(runner(b, model), b, policy_document)
    assert model.calls == 0


@pytest.mark.parametrize(
    "options",
    [{"responses": ["not json"]}, {"responses": ["ok"], "requested_tools": ["todo_list"]}],
)
async def test_policy_classifier_invalid_output_never_allows(
    paid_boundary, policy_document, options
):
    model = PaidFakeModel(**options)
    assert not (
        await inspect_policy(runner(paid_boundary, model), paid_boundary, policy_document)
    ).allowed
    assert model.calls == 1 and len(await paid_boundary.rows()) == 1


class PaidFakeModel(FakeListChatModel):
    calls: int = 0
    failures: int = 0
    status: int = 503
    fail_after_text: bool = False
    report_usage: bool = True
    reported_output: int = 5
    requested_tools: list[str] = []
    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        self.seen.append(messages)
        if self.calls <= self.failures:
            request = httpx.Request("POST", "https://fixture.invalid")
            raise httpx.HTTPStatusError(
                "fixture", request=request, response=httpx.Response(self.status)
            )
        if self.requested_tools and self.calls % 2 == 1:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": name,
                            "args": '{"query":"news"}' if name == "web_search" else "{}",
                            "id": f"tool-{self.calls}-{i}",
                            "index": i,
                        }
                        for i, name in enumerate(self.requested_tools)
                    ],
                )
            )
        else:
            async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
                yield chunk
                if self.fail_after_text:
                    request = httpx.Request("POST", "https://fixture.invalid")
                    raise httpx.HTTPStatusError(
                        "partial", request=request, response=httpx.Response(503)
                    )
        if self.report_usage:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": self.reported_output,
                        "total_tokens": 10 + self.reported_output,
                    },
                )
            )


def runner(b, model, tools=()):
    return AgentRunner(
        api_key="unused",
        base_url="https://fixture.invalid",
        model_name="sarvam-105b",
        thread_id="test-thread",
        tools=list(tools),
        chat_model=model,
        costs=b.costs,
        scope=b.scope,
        expires_at=b.costs.clock.utc_now() + timedelta(seconds=120),
    )


async def collect(subject, query="hello"):
    async def noop(*args):
        pass

    return "".join(
        [part async for part in subject.stream_reply(query, observer=noop, tool_observer=noop)]
    )


async def test_text_budget_denial_prevents_model_request(paid_boundary):
    b = paid_boundary
    await b.costs.reserve(
        b.scope,
        "exhaust",
        "sarvam",
        "sarvam-105b",
        WorkBound(units={"input_tokens": 5_000_000, "output_tokens": 0}),
    )
    await b.costs.settle(
        "exhaust", Usage(units={"input_tokens": 5_000_000, "output_tokens": 0}), "succeeded"
    )
    model = PaidFakeModel(responses=["hello"])
    with pytest.raises(BudgetExceeded):
        await collect(runner(b, model))
    assert model.calls == 0


async def test_text_stream_usage_settles_actual_tokens(paid_boundary):
    model = PaidFakeModel(responses=["hello"])
    assert await collect(runner(paid_boundary, model)) == "hello"
    row = (await paid_boundary.rows())[0]
    assert row["state"] == "settled" and row["actual_micro_inr"] == 20
    assert json.loads(row["bound_json"])["units"]["output_tokens"] == 512


async def test_text_missing_usage_retains_hold(paid_boundary):
    model = PaidFakeModel(responses=["hello"], report_usage=False)
    assert await collect(runner(paid_boundary, model)) == "hello"
    assert (await paid_boundary.rows())[0]["state"] == "unknown"


async def test_bounded_text_retry_has_distinct_attempts(paid_boundary):
    model = PaidFakeModel(responses=["hello"], failures=1)
    assert await collect(runner(paid_boundary, model)) == "hello"
    rows = await paid_boundary.rows()
    assert len({r["id"] for r in rows}) == 2 and model.calls == 2
    assert {r["state"] for r in rows} == {"unknown", "settled"}


async def test_text_does_not_retry_partial_output(paid_boundary):
    model = PaidFakeModel(responses=["hello"], fail_after_text=True)
    with pytest.raises(httpx.HTTPStatusError):
        await collect(runner(paid_boundary, model))
    assert model.calls == 1 and len(await paid_boundary.rows()) == 1


async def test_text_invalid_credentials_are_not_retried(paid_boundary):
    model = PaidFakeModel(responses=["hello"], failures=2, status=401)
    with pytest.raises(httpx.HTTPStatusError):
        await collect(runner(paid_boundary, model))
    assert model.calls == 1 and (await paid_boundary.rows())[0]["state"] == "unknown"


async def test_unbounded_current_input_requests_restatement_without_model_work(paid_boundary):
    model = PaidFakeModel(responses=["hello"])
    subject = runner(paid_boundary, model)
    reply = await collect(subject, "अ" * 9000)
    assert "restate" in reply.lower()
    assert model.calls == 0 and await paid_boundary.rows() == []
    assert await collect(subject, "Hello") == "hello" and model.calls == 1


async def test_oversized_completed_exchange_requires_restatement_before_forgetting(paid_boundary):
    model = PaidFakeModel(responses=["y" * 1000, "second"])
    subject = runner(paid_boundary, model)
    assert await collect(subject, "x" * 6000) == "y" * 1000
    assert "restate" in (await collect(subject, "Follow up on that")).lower()
    assert model.calls == 1
    assert await collect(subject, "A fresh request") == "second" and model.calls == 2


async def test_text_trims_whole_old_turns_to_input_bound(paid_boundary):
    model = PaidFakeModel(responses=["answer"])
    subject = runner(paid_boundary, model)
    for index in range(6):
        await collect(subject, f"turn-{index}:" + "x" * 2000)
    assert not any("turn-0:" in m.content for m in model.seen[-1])
    assert all(
        json.loads(r["bound_json"])["units"]["input_tokens"] <= 8000
        for r in await paid_boundary.rows()
    )


async def test_turn_refuses_fourth_tool_before_execution(paid_boundary):
    effects = []

    @tool
    async def todo_list() -> str:
        """List todo items."""
        effects.append("listed")
        return "[]"

    model = PaidFakeModel(responses=["done"], requested_tools=["todo_list"] * 4)
    with pytest.raises(RuntimeError, match="tool"):
        await collect(runner(paid_boundary, model, [todo_list]))
    assert len(effects) <= 3


async def test_turn_allows_only_one_search(paid_search):
    s = paid_search

    @tool
    async def web_search(query: str) -> str:
        """Search the web."""
        return str(await s.client.search(query))

    model = PaidFakeModel(responses=["done"], requested_tools=["web_search"] * 2)
    with pytest.raises(RuntimeError, match="search"):
        await collect(runner(s.boundary, model, [web_search]))
    assert s.state.calls <= 1


async def test_three_tools_can_complete_and_budget_resets_next_turn(paid_boundary):
    effects = []

    @tool
    async def todo_list() -> str:
        """List todo items."""
        effects.append("listed")
        return "[]"

    model = PaidFakeModel(responses=["done"], requested_tools=["todo_list"] * 3)
    subject = runner(paid_boundary, model, [todo_list])
    assert await collect(subject) == "done"
    assert await collect(subject) == "done"
    assert len(effects) == 6


async def test_interruption_preserves_completed_tool_call_ids(paid_boundary):
    @tool
    async def todo_list() -> str:
        """List todo items."""
        return "[]"

    model = PaidFakeModel(responses=["done"], requested_tools=["todo_list"])
    subject = runner(paid_boundary, model, [todo_list])
    await collect(subject)
    await subject.reconcile_interrupted_history("do")
    snapshot = await subject._agent.aget_state(subject._config())
    messages = snapshot.values["messages"]
    calls = {call["id"] for m in messages if m.type == "ai" for call in m.tool_calls}
    results = {m.tool_call_id for m in messages if m.type == "tool"}
    assert results and results <= calls


@pytest.mark.parametrize("transient", [False, True])
@pytest.mark.parametrize(
    "raw_usage,expected_state",
    [
        ({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, "settled"),
        ({"total_tokens": 15}, "unknown"),
        ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 15}, "unknown"),
        ({"prompt_tokens": 10, "total_tokens": 15}, "unknown"),
        ({"prompt_tokens": None, "completion_tokens": None, "total_tokens": 15}, "unknown"),
    ],
)
async def test_actual_chat_adapter_streams_metered_usage_without_sdk_retry(
    paid_boundary, raw_usage, expected_state, monkeypatch, transient
):
    from app.voice import agent as implementation

    calls = []

    async def http(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["max_tokens"] == 512
        assert "max_completion_tokens" not in body
        assert body["stream_options"]["include_usage"] is True
        if transient and len(calls) == 1:
            return httpx.Response(
                503, json={"error": {"message": "fixture unavailable", "type": "server_error"}}
            )
        events = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "hello"},
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": raw_usage,
            },
        ]
        content = (
            "".join(
                "data: "
                + json.dumps(
                    {
                        "id": "fixture-completion",
                        "model": "sarvam-105b",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        **event,
                    }
                )
                + "\n\n"
                for event in events
            )
            + "data: [DONE]\n\n"
        )
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
        factory = implementation.SarvamChatModel
        monkeypatch.setattr(
            implementation,
            "SarvamChatModel",
            lambda **kwargs: factory(**kwargs, http_async_client=client),
        )
        assert await collect(runner(paid_boundary, None)) == "hello"
    rows = await paid_boundary.rows()
    assert len(calls) == len(rows) == 1 + transient
    settled = [r for r in rows if r["state"] == "settled"]
    if expected_state == "settled":
        assert len(settled) == 1 and settled[0]["actual_micro_inr"] == 20
    else:
        assert not settled and all(r["state"] == "unknown" for r in rows)


async def test_character_cutoff_cancels_generation_and_retains_bound(paid_boundary):
    model = PaidFakeModel(responses=["x" * 1100])
    with pytest.raises(RuntimeError, match="character"):
        await collect(runner(paid_boundary, model))
    row = (await paid_boundary.rows())[0]
    assert row["state"] == "unknown" and row["outcome"] == "cancelled"


async def test_pending_tool_history_is_explicitly_unknown(paid_boundary):
    from langchain.messages import AIMessage

    subject = runner(paid_boundary, PaidFakeModel(responses=["hello"]))
    await collect(subject)
    await subject._agent.aupdate_state(
        subject._config(),
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "todo_add", "args": {"text": "milk"}, "id": "pending-tool"}
                    ],
                )
            ]
        },
    )
    await subject.reconcile_interrupted_history("")
    snapshot = await subject._agent.aget_state(subject._config())
    results = [
        m
        for m in snapshot.values["messages"]
        if m.type == "tool" and m.tool_call_id == "pending-tool"
    ]
    assert len(results) == 1 and "unknown" in results[0].content
    assert "do not claim success" in results[0].content


async def test_cancelled_model_stream_keeps_unknown_hold(paid_boundary):
    import asyncio

    model = PaidFakeModel(responses=["hello"], sleep=0.03)
    task = asyncio.create_task(collect(runner(paid_boundary, model)))
    for _ in range(50):
        if model.calls:
            break
        await asyncio.sleep(0.01)
    assert model.calls == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = (await paid_boundary.rows())[0]
    assert row["state"] == "unknown" and row["outcome"] == "cancelled"


async def test_model_expiry_after_reservation_is_confirmed_zero(paid_boundary, monkeypatch):
    b = paid_boundary
    now = b.costs.clock.utc_now()
    reserve = b.costs.reserve

    async def expires(*args, **kwargs):
        held = await reserve(*args, **kwargs)
        b.costs.clock.utc_now = lambda: now + timedelta(seconds=121)
        return held

    monkeypatch.setattr(b.costs, "reserve", expires)
    model = PaidFakeModel(responses=["hello"])
    with pytest.raises(RuntimeError, match="deadline"):
        await collect(runner(b, model))
    assert model.calls == 0
    row = (await b.rows())[0]
    assert row["state"] == "settled" and row["actual_micro_inr"] == 0


async def test_reported_model_overrun_freezes_new_paid_work(paid_boundary):
    from app.costs.models import CostAccountingError

    model = PaidFakeModel(responses=["hello"], reported_output=513)
    with pytest.raises(CostAccountingError):
        await collect(runner(paid_boundary, model))
    assert (await paid_boundary.rows())[0]["state"] == "disputed"
    with pytest.raises(CostAccountingError):
        await paid_boundary.generate("hello")
    assert paid_boundary.state.connects == 0


async def test_one_search_roundtrip_meters_both_model_requests(paid_search):
    s = paid_search

    @tool
    async def web_search(query: str) -> str:
        """Search the web."""
        return str(await s.client.search(query))

    model = PaidFakeModel(responses=["done"], requested_tools=["web_search"])
    assert await collect(runner(s.boundary, model, [web_search])) == "done"
    assert s.state.calls == 1 and model.calls == 2
    rows = await s.boundary.rows()
    assert len(rows) == 3 and all(r["state"] == "settled" for r in rows)
    assert await s.boundary.costs.spent_micro_inr() == 700_040
