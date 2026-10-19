from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import anyio
import httpx
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from pydantic import AfterValidator, Field

from app.costs.models import BudgetExceeded, CostScope, Usage, WorkBound
from app.costs.service import CostService
from app.models import new_id
from app.voice.tool_receipts import ToolCall, ToolCallConflict, ToolReceipt, tool_request_hash

ToolRequestSender = Callable[[dict[str, Any]], Awaitable[None]]
ToolSpanObserver = Callable[[dict[str, Any]], Awaitable[None]]
TODO_TOOL_NAMES = frozenset(
    {"todo_add", "todo_list", "todo_update", "todo_complete", "todo_delete"}
)
MUTATING_TOOL_NAMES = TODO_TOOL_NAMES - {"todo_list"}
_tool_turn_identity: ContextVar[tuple[str, str] | None] = ContextVar(
    "tool_turn_identity", default=None
)


def set_tool_turn_identity(turn_id: str, generation_id: str) -> Token:
    return _tool_turn_identity.set((turn_id, generation_id))


def reset_tool_turn_identity(token: Token) -> None:
    _tool_turn_identity.reset(token)


_tool_span_observer: ContextVar[ToolSpanObserver | None] = ContextVar(
    "tool_span_observer",
    default=None,
)


class ToolExecutionError(RuntimeError):
    pass


class ClientToolTimeoutError(ToolExecutionError):
    pass


def _todo_id(value: str) -> str:
    return re.sub(r"[\s\ufeff]+", " ", value).strip()


def _todo_text(value: str) -> str:
    # Match the browser's effective limit without splitting a Unicode surrogate pair.
    return _todo_id(value).encode("utf-16-le")[:1000].decode("utf-16-le", errors="ignore").rstrip()


TodoText = Annotated[str, AfterValidator(_todo_text), Field(min_length=1, max_length=500)]
TodoId = Annotated[str, AfterValidator(_todo_id), Field(min_length=1, max_length=128)]


@dataclass(frozen=True)
class SearchResult:
    title: str
    snippet: str
    url: str
    image: str | None = None
    favicon: str | None = None


def _https_url(value: object) -> str | None:
    # Shown to the browser only; never fed to the model.
    if isinstance(value, str) and value.startswith("https://") and len(value) <= 2048:
        return value
    return None


class ExaSearchClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        timeout_seconds: float,
        costs: CostService,
        scope: CostScope,
        expires_at: datetime,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._costs = costs
        self._scope = scope
        self._expires_at = expires_at
        self._timeout_seconds = timeout_seconds
        self._monotonic_deadline = costs.clock.monotonic() + max(
            0, (expires_at - costs.clock.utc_now()).total_seconds()
        )
        self._client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = http_client is None

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchResult]:
        if type(max_results) is not int or not 1 <= max_results <= 3:
            raise ToolExecutionError("Search supports between one and three results.")
        if not isinstance(query, str) or len(query) > 1000:
            raise ToolExecutionError("Search query must contain at most 1,000 characters.")
        normalized = " ".join(query.split())
        if not normalized:
            raise ToolExecutionError("Search query cannot be empty.")
        if self._api_key is None:
            raise ToolExecutionError(
                "Web search is unavailable because EXA_API_KEY is not configured."
            )
        remaining = self._remaining()
        if remaining <= 0:
            raise ToolExecutionError("Search deadline expired.")
        attempt_id = new_id()
        await self._costs.reserve(
            self._scope, attempt_id, "exa", "fast", WorkBound(units={"requests": 1})
        )
        usage = None
        outcome = "failed"
        try:
            remaining = self._remaining()
            if remaining <= 0:
                usage = Usage(units={"requests": 0})
                outcome = "cancelled"
                raise ToolExecutionError("Search deadline expired.")
            async with asyncio.timeout(remaining):
                response = await self._client.post(
                    "https://api.exa.ai/search",
                    headers={"x-api-key": self._api_key},
                    json={
                        "query": normalized,
                        "type": "fast",
                        "numResults": max_results,
                        "contents": {"highlights": {"maxCharacters": 600}},
                    },
                )
                response.raise_for_status()
                payload = response.json()
                raw_results = payload.get("results") if isinstance(payload, dict) else None
                if not isinstance(raw_results, list):
                    raise ToolExecutionError("Exa returned a response without a results list.")
                results: list[SearchResult] = []
                for item in raw_results[:max_results]:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url")
                    if not isinstance(url, str) or not url:
                        continue
                    title = item.get("title")
                    highlights = item.get("highlights")
                    snippet = (
                        " ".join(
                            part.strip()
                            for part in highlights
                            if isinstance(part, str) and part.strip()
                        )
                        if isinstance(highlights, list)
                        else ""
                    )
                    if not snippet:
                        fallback = item.get("summary") or item.get("text")
                        snippet = fallback.strip() if isinstance(fallback, str) else ""
                    results.append(
                        SearchResult(
                            title=title.strip()
                            if isinstance(title, str) and title.strip()
                            else url,
                            snippet=snippet[:900],
                            url=url,
                            image=_https_url(item.get("image")),
                            favicon=_https_url(item.get("favicon")),
                        )
                    )
                request_id = payload.get("requestId")
                if (
                    isinstance(request_id, str)
                    and 0 < len(request_id) <= 128
                    and not any(c.isspace() for c in request_id)
                ):
                    usage = Usage(units={"requests": 1}, provider_request_id=request_id)
                    outcome = "succeeded"
                else:
                    outcome = "unknown"
                return results
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await self._costs.settle(attempt_id, usage, outcome)

    def _remaining(self) -> float:
        return min(
            (self._expires_at - self._costs.clock.utc_now()).total_seconds(),
            self._monotonic_deadline - self._costs.clock.monotonic(),
            self._timeout_seconds,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class ClientToolProxy:
    def __init__(
        self, *, sender: ToolRequestSender, timeout_seconds: float, owner_id: str | None = None
    ) -> None:
        self._sender = sender
        self._timeout_seconds = timeout_seconds
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._owner_id = owner_id

    async def request(self, *, call_id: str, name: str, arguments: dict[str, Any]) -> Any:
        if name not in TODO_TOOL_NAMES:
            raise ToolExecutionError(f"Unsupported client tool: {name}")
        identity = _tool_turn_identity.get()
        return await self._exchange(
            call_id,
            {
                "type": "tool_request",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "request_hash": tool_request_hash(name, arguments),
                **({"turn_id": identity[0], "generation_id": identity[1]} if identity else {}),
            },
            name,
        )

    async def _exchange(self, call_id: str, message: dict, description: str) -> Any:
        if call_id in self._pending:
            raise ToolExecutionError(f"Duplicate client tool call ID: {call_id}")
        future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        try:
            await self._sender(message)
            try:
                return await asyncio.wait_for(future, timeout=self._timeout_seconds)
            except TimeoutError as exc:
                raise ClientToolTimeoutError(
                    f"The browser did not finish {description} "
                    f"within {self._timeout_seconds:g} seconds."
                ) from exc
        finally:
            self._pending.pop(call_id, None)

    async def reconcile(
        self, user_id: str, call: ToolCall, request_hash: str
    ) -> ToolReceipt | None:
        if user_id != self._owner_id:
            raise ToolCallConflict("Browser receipt owner does not match the authenticated socket")
        request_id = "receipt_" + new_id()
        try:
            result = await self._exchange(
                request_id,
                {
                    "type": "tool_receipt_query",
                    "request_id": request_id,
                    "call_id": call.id,
                    "request_hash": request_hash,
                },
                "tool receipt lookup",
            )
        except ClientToolTimeoutError:
            return None
        if result is None:
            return None
        if type(result) is not dict or set(result) != {
            "call_id",
            "state",
            "result",
            "request_hash",
        }:
            raise ToolCallConflict("Invalid browser receipt response")
        return ToolReceipt(**result)

    def resolve(self, *, call_id: str, result: Any) -> bool:
        future = self._pending.get(call_id)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True

    def cancel_pending(self) -> int:
        cancelled = 0
        for future in tuple(self._pending.values()):
            if not future.done():
                future.cancel()
                cancelled += 1
        return cancelled


def set_tool_span_observer(observer: ToolSpanObserver) -> Token[ToolSpanObserver | None]:
    return _tool_span_observer.set(observer)


def reset_tool_span_observer(token: Token[ToolSpanObserver | None]) -> None:
    _tool_span_observer.reset(token)


async def execute_measured(
    *,
    name: str,
    call_id: str,
    operation: Callable[[], Awaitable[Any]],
    span_details: dict[str, Any] | None = None,
) -> str:
    started = time.monotonic()
    outcome = "success"
    error: str | None = None
    try:
        result = await operation()
        if isinstance(result, dict) and result.get("ok") is False:
            outcome = "error"
            error = "The client tool reported an error."
            result = {**result, "error": error}
    except asyncio.CancelledError:
        outcome = "cancelled"
        error = "Tool execution was cancelled because the voice turn was interrupted."
        raise
    except BudgetExceeded:
        outcome = "error"
        error = "Provider allowance is unavailable."
        raise
    except (ClientToolTimeoutError, httpx.HTTPError):
        outcome = "unknown"
        error = "The tool outcome is unconfirmed."
        result = {"ok": False, "state": "unknown", "error": error}
    except ToolExecutionError:
        outcome = "error"
        error = "The tool could not complete the request."
        result = {"ok": False, "error": error}
    except Exception:
        outcome = "unknown"
        error = "The tool outcome is unconfirmed."
        result = {"ok": False, "state": "unknown", "error": error}
    finally:
        ended = time.monotonic()
        observer = _tool_span_observer.get()
        if observer is not None:
            await observer(
                {
                    "name": name,
                    "call_id": call_id,
                    "start_server": started,
                    "end_server": ended,
                    "duration_ms": (ended - started) * 1000,
                    "outcome": outcome,
                    "error": error,
                    **(span_details or {}),
                }
            )
    return json.dumps(result, ensure_ascii=False)


def build_agent_tools(
    *,
    exa_client: ExaSearchClient,
    todo_proxy: ClientToolProxy,
) -> list[BaseTool]:
    @tool
    async def web_search(query: str, runtime: ToolRuntime[CostScope]) -> str:
        """Search the live web for fresh or factual information."""

        call_id = runtime.tool_call_id or str(uuid.uuid4())
        details: dict[str, Any] = {}

        async def operation() -> dict[str, Any]:
            try:
                results = await exa_client.search(query)
            except BudgetExceeded as exc:
                # Nothing was spent: answer without search instead of ending the call.
                raise ToolExecutionError("Web search is unavailable for this call.") from exc
            details["sources"] = [
                {"title": r.title, "url": r.url, "image": r.image, "favicon": r.favicon}
                for r in results
            ]
            return {
                "ok": True,
                "results": [
                    {"title": r.title, "snippet": r.snippet, "url": r.url} for r in results
                ],
            }

        return await execute_measured(
            name="web_search",
            call_id=call_id,
            operation=operation,
            span_details=details,
        )

    @tool
    async def todo_add(text: TodoText, runtime: ToolRuntime[CostScope]) -> str:
        """Add a new item to the user's browser-local todo list."""

        call_id = runtime.tool_call_id or str(uuid.uuid4())
        return await execute_measured(
            name="todo_add",
            call_id=call_id,
            operation=lambda: todo_proxy.request(
                call_id=call_id,
                name="todo_add",
                arguments={"text": text},
            ),
        )

    @tool
    async def todo_list(runtime: ToolRuntime[CostScope]) -> str:
        """List all browser-local todos, including IDs and completion state."""

        call_id = runtime.tool_call_id or str(uuid.uuid4())
        return await execute_measured(
            name="todo_list",
            call_id=call_id,
            operation=lambda: todo_proxy.request(
                call_id=call_id,
                name="todo_list",
                arguments={},
            ),
        )

    @tool
    async def todo_update(todo_id: TodoId, text: TodoText, runtime: ToolRuntime[CostScope]) -> str:
        """Replace the text of one browser-local todo using its ID."""

        call_id = runtime.tool_call_id or str(uuid.uuid4())
        return await execute_measured(
            name="todo_update",
            call_id=call_id,
            operation=lambda: todo_proxy.request(
                call_id=call_id,
                name="todo_update",
                arguments={"todo_id": todo_id, "text": text},
            ),
        )

    @tool
    async def todo_complete(todo_id: TodoId, runtime: ToolRuntime[CostScope]) -> str:
        """Mark one browser-local todo complete using its ID."""

        call_id = runtime.tool_call_id or str(uuid.uuid4())
        return await execute_measured(
            name="todo_complete",
            call_id=call_id,
            operation=lambda: todo_proxy.request(
                call_id=call_id,
                name="todo_complete",
                arguments={"todo_id": todo_id},
            ),
        )

    @tool
    async def todo_delete(todo_id: TodoId, runtime: ToolRuntime[CostScope]) -> str:
        """Delete one browser-local todo using its ID."""

        call_id = runtime.tool_call_id or str(uuid.uuid4())
        return await execute_measured(
            name="todo_delete",
            call_id=call_id,
            operation=lambda: todo_proxy.request(
                call_id=call_id,
                name="todo_delete",
                arguments={"todo_id": todo_id},
            ),
        )

    return [web_search, todo_add, todo_list, todo_update, todo_complete, todo_delete]
