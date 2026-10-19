from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from test_paid_boundaries import paid_boundary as paid_boundary

from app.voice.tool_receipts import ToolCall, ToolCallConflict
from app.voice.tools import (
    ClientToolProxy,
    ClientToolTimeoutError,
    ExaSearchClient,
    ToolExecutionError,
    execute_measured,
    reset_tool_span_observer,
    set_tool_span_observer,
)


async def test_client_tool_proxy_correlates_result() -> None:
    sent: list[dict[str, object]] = []

    async def sender(message: dict[str, object]) -> None:
        sent.append(message)

    proxy = ClientToolProxy(sender=sender, timeout_seconds=1)
    pending = asyncio.create_task(
        proxy.request(call_id="call-1", name="todo_add", arguments={"text": "buy milk"})
    )
    await asyncio.sleep(0)

    assert sent[0]["call_id"] == "call-1"
    assert proxy.resolve(call_id="call-1", result={"ok": True, "id": "todo-1"})
    assert await pending == {"ok": True, "id": "todo-1"}


async def test_client_tool_timeout_becomes_explicit_error() -> None:
    async def sender(message: dict[str, object]) -> None:
        del message

    proxy = ClientToolProxy(sender=sender, timeout_seconds=0.01)

    with pytest.raises(ClientToolTimeoutError, match="did not finish"):
        await proxy.request(call_id="call-1", name="todo_list", arguments={})


async def test_barge_in_cancels_pending_tool_and_discards_late_result() -> None:
    sent = asyncio.Event()

    async def sender(message: dict[str, object]) -> None:
        del message
        sent.set()

    proxy = ClientToolProxy(sender=sender, timeout_seconds=10)
    pending = asyncio.create_task(proxy.request(call_id="call-1", name="todo_list", arguments={}))
    await sent.wait()

    assert proxy.cancel_pending() == 1
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not proxy.resolve(call_id="call-1", result={"ok": True})


async def test_browser_receipt_query_is_read_only_and_has_separate_correlation():
    messages = []

    async def sender(message):
        messages.append(message)

    proxy = ClientToolProxy(sender=sender, timeout_seconds=0.02, owner_id="owner")
    call = ToolCall("original", "todo_delete", {"todo_id": "todo-1"})
    pending = asyncio.create_task(proxy.reconcile("owner", call, "a" * 64))
    await asyncio.sleep(0)
    query = messages[0]
    assert query["type"] == "tool_receipt_query" and query["call_id"] == "original"
    assert query["request_hash"] == "a" * 64 and query["request_id"] != "original"
    assert "arguments" not in query and "name" not in query
    assert not proxy.resolve(call_id="original", result={"ok": True})
    proxy.resolve(
        call_id=query["request_id"],
        result={
            "call_id": "original",
            "state": "succeeded",
            "request_hash": "a" * 64,
            "result": {"ok": True},
        },
    )
    assert (await pending).state == "succeeded"
    with pytest.raises(ToolCallConflict):
        await proxy.reconcile("another-owner", call, "a" * 64)
    assert len(messages) == 1


async def test_missing_browser_receipt_reply_stays_unconfirmed():
    messages = []

    async def sender(message):
        messages.append(message)

    proxy = ClientToolProxy(sender=sender, timeout_seconds=0.001, owner_id="owner")
    assert (
        await proxy.reconcile(
            "owner", ToolCall("original", "todo_delete", {"todo_id": "todo-1"}), "a" * 64
        )
        is None
    )
    assert len(messages) == 1 and messages[0]["type"] == "tool_receipt_query"


async def test_exa_client_uses_mocked_httpx_and_normalizes_results(paid_boundary) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "exa-key"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Fresh result",
                        "url": "https://example.test/result",
                        "highlights": [" First fact. ", "Second fact."],
                        "image": "https://example.test/photo.jpg",
                        "favicon": "http://example.test/favicon.ico",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ExaSearchClient(
            api_key="exa-key",
            timeout_seconds=1,
            http_client=http_client,
            costs=paid_boundary.costs,
            scope=paid_boundary.scope,
            expires_at=paid_boundary.costs.clock.utc_now() + timedelta(seconds=120),
        )
        results = await client.search("  current   fact  ")

    assert results[0].title == "Fresh result"
    assert results[0].snippet == "First fact. Second fact."
    assert results[0].url == "https://example.test/result"
    assert results[0].image == "https://example.test/photo.jpg"
    assert results[0].favicon is None  # only https media reaches the browser


async def test_measured_tool_error_returns_model_visible_error_and_span() -> None:
    spans: list[dict[str, object]] = []

    async def observer(span: dict[str, object]) -> None:
        spans.append(span)

    async def operation() -> object:
        raise ToolExecutionError("tool is unavailable")

    token = set_tool_span_observer(observer)
    try:
        result = await execute_measured(
            name="web_search",
            call_id="call-1",
            operation=operation,
        )
    finally:
        reset_tool_span_observer(token)

    assert json.loads(result) == {"ok": False, "error": "The tool could not complete the request."}
    assert spans[0]["name"] == "web_search"
    assert spans[0]["outcome"] == "error"
    assert float(spans[0]["duration_ms"]) >= 0


async def test_search_over_call_budget_answers_without_search_instead_of_ending_call() -> None:
    from types import SimpleNamespace

    from app.costs.models import BudgetExceeded
    from app.voice.tools import build_agent_tools

    class DeniedSearch:
        async def search(self, query: str) -> list[object]:
            raise BudgetExceeded("Session provider allowance exhausted")

    tools = build_agent_tools(exa_client=DeniedSearch(), todo_proxy=None)
    web_search = next(item for item in tools if item.name == "web_search")
    result = await web_search.coroutine(query="news", runtime=SimpleNamespace(tool_call_id="c1"))

    assert json.loads(result) == {"ok": False, "error": "The tool could not complete the request."}


async def test_search_sources_reach_the_span_but_not_the_model() -> None:
    from types import SimpleNamespace

    from app.voice.tools import SearchResult, build_agent_tools

    class FoundSearch:
        async def search(self, query: str) -> list[SearchResult]:
            return [
                SearchResult("Cafe", "Open late.", "https://cafe.test", "https://cafe.test/a.jpg")
            ]

    spans: list[dict[str, object]] = []

    async def observer(span: dict[str, object]) -> None:
        spans.append(span)

    tools = build_agent_tools(exa_client=FoundSearch(), todo_proxy=None)
    web_search = next(item for item in tools if item.name == "web_search")
    token = set_tool_span_observer(observer)
    try:
        result = await web_search.coroutine(
            query="cafes", runtime=SimpleNamespace(tool_call_id="c1")
        )
    finally:
        reset_tool_span_observer(token)

    assert json.loads(result)["results"] == [
        {"title": "Cafe", "snippet": "Open late.", "url": "https://cafe.test"}
    ]
    assert spans[0]["sources"] == [
        {
            "title": "Cafe",
            "url": "https://cafe.test",
            "image": "https://cafe.test/a.jpg",
            "favicon": None,
        }
    ]
