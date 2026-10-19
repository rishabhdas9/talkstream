from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy import text

from app.db import Database
from app.runtime import Clock
from app.voice.lifecycle import bounded_cleanup

ReceiptState = Literal["pending", "succeeded", "failed", "unknown"]


class ToolCallConflict(RuntimeError):
    pass


class ToolCallInvalid(ValueError):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolReceipt:
    call_id: str
    state: ReceiptState
    result: dict[str, Any]
    request_hash: str
    replayed: bool = False


ToolDispatcher = Callable[[str, str, str, ToolCall], Awaitable[dict[str, Any]]]
ReceiptLookup = Callable[[str, ToolCall, str], Awaitable[ToolReceipt | None]]


def _identity(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(ord(c) < 33 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise ToolCallInvalid("Invalid tool execution identity")


def _json_bytes(value: Any, maximum: int) -> bytes:
    def check(item, depth=0):
        if depth > 16:
            raise ValueError("Tool JSON is too deeply nested")
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("Tool JSON keys must be strings")
                check(child, depth + 1)
        elif type(item) is list:
            for child in item:
                check(child, depth + 1)
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise ValueError("Tool values must be JSON")

    check(value)
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > maximum:
        raise ValueError("Tool JSON exceeds its size limit")
    return encoded


def tool_request_hash(name: str, arguments: dict[str, Any]) -> str:
    return hashlib.sha256(_json_bytes({"name": name, "arguments": arguments}, 8192)).hexdigest()


def application_call_id(user_id: str, session_id: str, turn_id: str, provider_id: str) -> str:
    parts = [user_id, session_id, turn_id, provider_id]
    for value in parts:
        _identity(value)
    return "tool_" + hashlib.sha256(_json_bytes(parts, 4096)).hexdigest()


class ToolExecutor:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        *,
        schemas: Mapping[str, type[BaseModel]],
        dispatch: ToolDispatcher,
        browser_tools: set[str] | frozenset[str] = frozenset(),
        side_effecting_tools: set[str] | frozenset[str] = frozenset(),
        reconcile: ReceiptLookup | None = None,
    ):
        self.database, self.clock = database, clock
        self._schemas = dict(schemas)
        self._dispatch = dispatch
        self._browser_tools = frozenset(browser_tools)
        self._side_effecting_tools = frozenset(side_effecting_tools)
        self._lookup = reconcile

    def _now(self) -> str:
        now = self.clock.utc_now()
        if now.utcoffset() is None:
            raise ToolCallInvalid("Tool receipt clock must be aware UTC")
        return now.astimezone(UTC).isoformat()

    def prepare_call(self, call: ToolCall) -> tuple[ToolCall, str]:
        try:
            _identity(call.id)
            _identity(call.name)
            if call.name not in self._schemas or type(call.arguments) is not dict:
                raise ToolCallInvalid("Unsupported tool or arguments")
            encoded = _json_bytes({"name": call.name, "arguments": call.arguments}, 8192)
            # Snapshot caller-owned dictionaries before the first asynchronous boundary.
            arguments = json.loads(encoded)["arguments"]
            arguments = (
                self._schemas[call.name]
                .model_validate(arguments, strict=True, extra="forbid")
                .model_dump(mode="json")
            )
            _json_bytes(arguments, 8192)
        except (ValueError, TypeError, RecursionError, ValidationError) as exc:
            raise ToolCallInvalid("Invalid tool request") from exc
        return ToolCall(call.id, call.name, arguments), tool_request_hash(call.name, arguments)

    @staticmethod
    def _receipt(row: Mapping, result: dict[str, Any] | None = None) -> ToolReceipt:
        return ToolReceipt(
            call_id=row["call_id"],
            state=row["state"],
            result=result
            if result is not None
            else {
                "ok": row["state"] == "succeeded",
                "receipt_ref": row["receipt_ref"],
                "result_available": False,
            },
            request_hash=row["request_hash"],
        )

    async def get_receipt(self, user_id: str, call_id: str) -> ToolReceipt | None:
        _identity(user_id)
        _identity(call_id)
        async with self.database.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text("SELECT * FROM tool_receipts WHERE call_id=:id AND user_id=:owner"),
                        {"id": call_id, "owner": user_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._receipt(row) if row is not None else None

    async def _claim(self, user_id, session_id, turn_id, call, request_hash):
        now = self._now()
        async with self.database.engine.connect() as connection:
            try:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
                authorized = (
                    await connection.execute(
                        text(
                            "SELECT 1 FROM usage_sessions WHERE id=:session "
                            "AND user_id=:owner AND ended_at IS NULL"
                        ),
                        {"session": session_id, "owner": user_id},
                    )
                ).first()
                if authorized is None:
                    raise ToolCallInvalid("Tool execution requires the owner's active session")
                row = (
                    (
                        await connection.execute(
                            text("SELECT * FROM tool_receipts WHERE call_id=:id"), {"id": call.id}
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is not None:
                    if row["user_id"] != user_id or row["request_hash"] != request_hash:
                        raise ToolCallConflict(
                            "Tool call ID already belongs to a different request"
                        )
                elif call.name in self._side_effecting_tools:
                    row = (
                        (
                            await connection.execute(
                                text(
                                    "SELECT * FROM tool_receipts WHERE user_id=:owner "
                                    "AND request_hash=:hash AND state IN ('pending','unknown') "
                                    "ORDER BY created_at,call_id LIMIT 1"
                                ),
                                {"owner": user_id, "hash": request_hash},
                            )
                        )
                        .mappings()
                        .first()
                    )
                fresh = row is None
                if fresh:
                    await connection.execute(
                        text(
                            "INSERT INTO tool_receipts (call_id,user_id,session_id,turn_id,"
                            "tool_name,request_hash,state,receipt_ref,created_at,updated_at) "
                            "VALUES (:id,:owner,:session,:turn,:name,:hash,'pending',:id,:now,:now)"
                        ),
                        {
                            "id": call.id,
                            "owner": user_id,
                            "session": session_id,
                            "turn": turn_id,
                            "name": call.name,
                            "hash": request_hash,
                            "now": now,
                        },
                    )
                    row = (
                        (
                            await connection.execute(
                                text("SELECT * FROM tool_receipts WHERE call_id=:id"),
                                {"id": call.id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                await connection.commit()
                return self._receipt(row), fresh
            except BaseException:
                await connection.rollback()
                raise

    async def _settle(self, user_id, call_id, state, result=None):
        async with self.database.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tool_receipts SET state=:state,updated_at=:now "
                    "WHERE call_id=:id AND user_id=:owner AND state IN ('pending','unknown')"
                ),
                {"state": state, "now": self._now(), "id": call_id, "owner": user_id},
            )
            row = (
                (
                    await connection.execute(
                        text("SELECT * FROM tool_receipts WHERE call_id=:id AND user_id=:owner"),
                        {"id": call_id, "owner": user_id},
                    )
                )
                .mappings()
                .one()
            )
        return self._receipt(row, result if row["state"] == state else None)

    @staticmethod
    def _outcome(result: Any) -> ReceiptState:
        if type(result) is not dict or type(result.get("ok")) is not bool:
            return "unknown"
        try:
            _json_bytes(result, 65536)
        except (ValueError, TypeError, RecursionError):
            return "unknown"
        state = "succeeded" if result["ok"] else "failed"
        if result.get("state") is not None and result["state"] != state:
            return "unknown"
        return state

    async def execute(
        self, user_id: str, session_id: str, turn_id: str, call: ToolCall
    ) -> ToolReceipt:
        for value in (user_id, session_id, turn_id):
            _identity(value)
        call, request_hash = self.prepare_call(call)
        receipt, fresh = await self._claim(user_id, session_id, turn_id, call, request_hash)
        if not fresh:
            call = replace(call, id=receipt.call_id)
            if receipt.state == "pending":
                receipt = await self._settle(user_id, call.id, "unknown")
            if (
                call.name in self._browser_tools
                and self._lookup is not None
                and receipt.state != "failed"
            ):
                found = await self._lookup(user_id, call, request_hash)
                if found is not None:
                    try:
                        if not isinstance(found, ToolReceipt):
                            raise ValueError("Invalid receipt")
                        _json_bytes(found.result, 65536)
                    except (ValueError, TypeError, RecursionError) as exc:
                        raise ToolCallConflict("Invalid browser receipt") from exc
                    if (
                        not isinstance(found, ToolReceipt)
                        or found.call_id != call.id
                        or found.request_hash != request_hash
                        or found.state not in ("succeeded", "failed", "unknown", "pending")
                        or (
                            found.state in ("succeeded", "failed")
                            and self._outcome(found.result) != found.state
                        )
                        or (
                            found.state in ("unknown", "pending")
                            and (
                                type(found.result) is not dict
                                or found.result.get("ok") is not False
                            )
                        )
                        or (receipt.state == "succeeded" and found.state != "succeeded")
                    ):
                        raise ToolCallConflict("Browser receipt does not match the saved request")
                    if found.state in ("unknown", "pending"):
                        return replace(receipt, replayed=True)
                    reconciled = await self._settle(user_id, call.id, found.state, found.result)
                    if reconciled.state != found.state:
                        raise ToolCallConflict("Browser receipt conflicts with a completed outcome")
                    receipt = reconciled
            return replace(receipt, replayed=True)
        try:
            result = await self._dispatch(user_id, session_id, turn_id, call)
        except BaseException:
            # The claim stays conservative even if cancellation also prevents settlement.
            with contextlib.suppress(Exception):
                await bounded_cleanup(self._settle(user_id, call.id, "unknown"))
            raise
        state = self._outcome(result)
        return await bounded_cleanup(
            self._settle(user_id, call.id, state, result if state != "unknown" else None)
        )
