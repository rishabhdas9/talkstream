from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.voice.tool_receipts import (
    ToolCall,
    ToolCallConflict,
    ToolCallInvalid,
    ToolExecutor,
    ToolReceipt,
)


class AddArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    text: str = Field(min_length=1, max_length=1000)


class EmptyArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


@pytest.fixture
async def rig(database):
    now = datetime(2026, 10, 18, 12, tzinfo=UTC)
    clock = SimpleNamespace(utc_now=lambda: now)
    async with database.engine.begin() as connection:
        for user in ("a", "b"):
            await connection.execute(
                text("INSERT INTO users VALUES (:id,:email,:now,1)"),
                {"id": user, "email": f"{user}@example.test", "now": now.isoformat()},
            )
            await connection.execute(
                text(
                    "INSERT INTO usage_sessions (id,user_id,started_at,billed_seconds,cost_paise) "
                    "VALUES (:id,:user,:now,0,0)"
                ),
                {"id": f"s-{user}", "user": user, "now": now.isoformat()},
            )
    state = SimpleNamespace(calls=[], outcome={"ok": True, "todo": {"text": "private todo"}})

    async def dispatch(user_id, session_id, turn_id, call):
        async with database.engine.connect() as connection:
            saved = (
                await connection.execute(
                    text("SELECT state FROM tool_receipts WHERE call_id=:id"), {"id": call.id}
                )
            ).scalar_one()
        assert saved == "pending", "external action started before durable claim"
        state.calls.append((user_id, session_id, turn_id, call))
        if isinstance(state.outcome, BaseException):
            raise state.outcome
        return state.outcome

    def make(**kwargs):
        return ToolExecutor(
            database,
            clock,
            schemas={"todo_add": AddArguments, "todo_list": EmptyArguments},
            dispatch=kwargs.pop("dispatch", dispatch),
            **kwargs,
        )

    async def rows():
        async with database.engine.connect() as connection:
            return (await connection.execute(text("SELECT * FROM tool_receipts"))).mappings().all()

    return SimpleNamespace(
        database=database,
        make=make,
        executor=make(),
        call=ToolCall("call-1", "todo_add", {"text": "private todo"}),
        state=state,
        rows=rows,
    )


async def test_receipt_is_durable_before_dispatch_and_replay_never_executes(rig):
    first = await rig.executor.execute("a", "s-a", "turn-1", rig.call)
    replay = await rig.make().execute("a", "s-a", "turn-2", rig.call)
    assert first.state == replay.state == "succeeded"
    assert first.result == {"ok": True, "todo": {"text": "private todo"}}
    assert replay.result == {"ok": True, "receipt_ref": "call-1", "result_available": False}
    assert len(rig.state.calls) == 1
    assert rig.state.calls[0][:3] == ("a", "s-a", "turn-1")
    saved = (await rig.rows())[0]
    assert saved["created_at"] == "2026-10-18T12:00:00+00:00"
    assert saved["receipt_ref"] == "call-1"
    assert "private todo" not in json.dumps(dict(saved))
    assert "arguments" not in saved and "result" not in saved


@pytest.mark.parametrize(
    "changed",
    [ToolCall("call-1", "todo_add", {"text": "different"}), ToolCall("call-1", "todo_list", {})],
)
async def test_same_id_different_request_is_rejected(rig, changed):
    await rig.executor.execute("a", "s-a", "t", rig.call)
    with pytest.raises(ToolCallConflict):
        await rig.executor.execute("a", "s-a", "t", changed)
    assert len(rig.state.calls) == 1


async def test_receipts_do_not_cross_owners_or_sessions(rig):
    await rig.executor.execute("a", "s-a", "t", rig.call)
    assert await rig.executor.get_receipt("b", "call-1") is None
    assert await rig.executor.get_receipt("a", "missing") is None
    with pytest.raises(ToolCallConflict):
        await rig.executor.execute("b", "s-b", "t", rig.call)
    with pytest.raises(ToolCallInvalid):
        await rig.executor.execute("b", "s-a", "t", dataclasses.replace(rig.call, id="new"))
    assert len(rig.state.calls) == 1


async def test_ended_session_cannot_execute_a_new_action(rig):
    async with rig.database.engine.begin() as connection:
        await connection.execute(text("UPDATE usage_sessions SET ended_at=started_at"))
    with pytest.raises(ToolCallInvalid):
        await rig.executor.execute("a", "s-a", "t", rig.call)
    assert rig.state.calls == [] and await rig.rows() == []


@pytest.mark.parametrize(
    "call",
    [
        ToolCall("", "todo_add", {"text": "valid"}),
        ToolCall("x" * 129, "todo_add", {"text": "valid"}),
        ToolCall("c", "not_allowed", {}),
        ToolCall("c", "todo_add", {}),
        ToolCall("c", "todo_add", {"text": 1}),
        ToolCall("c", "todo_add", {"text": ""}),
        ToolCall("c", "todo_add", {"text": "x" * 1001}),
        ToolCall("c", "todo_add", {"text": "valid", "user_id": "b"}),
        ToolCall("c", "todo_add", {"text": float("nan")}),
        ToolCall("c", "todo_add", {"text": "\ud800"}),
    ],
)
async def test_untrusted_calls_fail_before_record_or_dispatch(rig, call):
    with pytest.raises(ToolCallInvalid):
        await rig.executor.execute("a", "s-a", "t", call)
    assert rig.state.calls == [] and await rig.rows() == []


async def test_canonical_hash_is_independent_of_argument_order(rig):
    class PairArguments(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        first: str
        second: str

    executor = ToolExecutor(
        rig.database,
        rig.executor.clock,
        schemas={"pair": PairArguments},
        dispatch=lambda *args: asyncio.sleep(0, result={"ok": True}),
    )
    first = await executor.execute(
        "a", "s-a", "t", ToolCall("c", "pair", {"first": "1", "second": "2"})
    )
    again = await executor.execute(
        "a", "s-a", "t", ToolCall("c", "pair", {"second": "2", "first": "1"})
    )
    assert first.request_hash == "7c05421c3c1b0bd18b22535f2c8cd4a6510a09ab4af059ce005d24569bc413f4"
    assert first.request_hash == again.request_hash


async def test_registered_schema_normalization_precedes_hash_and_dispatch(rig):
    class NormalizedArguments(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
        text: str

    seen = []

    async def dispatch(user, session, turn, call):
        seen.append(call.arguments)
        return {"ok": True}

    executor = ToolExecutor(
        rig.database,
        rig.executor.clock,
        schemas={"todo_add": NormalizedArguments},
        dispatch=dispatch,
    )
    first = await executor.execute("a", "s-a", "t", ToolCall("c", "todo_add", {"text": " milk "}))
    second = await executor.execute("a", "s-a", "t", ToolCall("c", "todo_add", {"text": "milk"}))
    assert first.request_hash == second.request_hash and seen == [{"text": "milk"}]


async def test_concurrent_duplicates_claim_once_and_conflicts_do_not_wait_for_action(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def action(*args):
        effects.append("deleted")
        entered.set()
        await release.wait()
        return {"ok": True}

    executor = rig.make(dispatch=action)
    first = asyncio.create_task(executor.execute("a", "s-a", "t", rig.call))
    await asyncio.wait_for(entered.wait(), 0.5)
    try:
        other = rig.make(dispatch=action)
        duplicate = await asyncio.wait_for(other.execute("a", "s-a", "t", rig.call), 0.5)
        assert duplicate.state == "unknown" and duplicate.result["ok"] is False
        with pytest.raises(ToolCallConflict):
            await asyncio.wait_for(
                other.execute(
                    "a", "s-a", "t", dataclasses.replace(rig.call, arguments={"text": "other"})
                ),
                0.5,
            )
        assert effects == ["deleted"]
    finally:
        release.set()
        assert (await first).state == "succeeded"
    assert (await executor.get_receipt("a", "call-1")).state == "succeeded"


async def test_simultaneous_conflicting_claims_execute_only_one_request(rig):
    changed = dataclasses.replace(rig.call, arguments={"text": "other"})
    results = await asyncio.gather(
        rig.make().execute("a", "s-a", "t", rig.call),
        rig.make().execute("a", "s-a", "t", changed),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ToolCallConflict) for result in results) == 1
    assert (
        sum(isinstance(result, ToolReceipt) and result.state == "succeeded" for result in results)
        == 1
    )
    assert len(rig.state.calls) == 1 and len(await rig.rows()) == 1


async def test_request_arguments_are_snapshotted_before_database_wait(rig):
    async with rig.database.engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        pending = asyncio.create_task(rig.executor.execute("a", "s-a", "t", rig.call))
        await asyncio.sleep(0)
        rig.call.arguments["text"] = "changed after submission"
        await connection.commit()
    receipt = await pending
    assert receipt.state == "succeeded"
    assert rig.state.calls[0][3].arguments == {"text": "private todo"}


@pytest.mark.parametrize(
    "outcome,state",
    [
        ({"ok": False, "error": "private failure"}, "failed"),
        ({}, "unknown"),
        ({"ok": "yes"}, "unknown"),
    ],
)
async def test_failed_or_malformed_result_is_not_replayed_as_success(rig, outcome, state):
    rig.state.outcome = outcome
    receipt = await rig.executor.execute("a", "s-a", "t", rig.call)
    again = await rig.make().execute("a", "s-a", "t", rig.call)
    assert receipt.state == again.state == state
    assert again.result["ok"] is False
    assert len(rig.state.calls) == 1 and "private failure" not in json.dumps(
        dict((await rig.rows())[0])
    )


async def test_transport_failure_is_unknown_and_does_not_retry(rig):
    rig.state.outcome = TimeoutError("lost acknowledgement")
    with pytest.raises(TimeoutError):
        await rig.executor.execute("a", "s-a", "t", rig.call)
    receipt = await rig.make().execute("a", "s-a", "t", rig.call)
    assert receipt.state == "unknown" and len(rig.state.calls) == 1


async def test_unknown_mutation_with_a_new_id_reuses_original_receipt(rig):
    rig.state.outcome = TimeoutError("lost acknowledgement")
    executor = rig.make(side_effecting_tools={"todo_add"})
    with pytest.raises(TimeoutError):
        await executor.execute("a", "s-a", "t", rig.call)
    changed_id = dataclasses.replace(rig.call, id="new-provider-attempt")
    receipt = await executor.execute("a", "s-a", "next", changed_id)
    assert receipt.state == "unknown" and receipt.call_id == "call-1"
    assert len(rig.state.calls) == 1 and len(await rig.rows()) == 1


@pytest.mark.parametrize("state", ["pending", "unknown"])
async def test_unconfirmed_reported_result_is_not_a_terminal_failure(rig, state):
    rig.state.outcome = {"ok": False, "state": state, "error": "unconfirmed"}
    receipt = await rig.executor.execute("a", "s-a", "t", rig.call)
    assert receipt.state == "unknown" and (await rig.rows())[0]["state"] == "unknown"


async def test_cancellation_after_side_effect_preserves_unknown_receipt(rig):
    entered = asyncio.Event()

    async def action(*args):
        rig.state.calls.append("deleted")
        entered.set()
        await asyncio.Event().wait()

    executor = rig.make(dispatch=action)
    pending = asyncio.create_task(executor.execute("a", "s-a", "t", rig.call))
    await asyncio.wait_for(entered.wait(), 0.5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    receipt = await rig.make().execute("a", "s-a", "next", rig.call)
    assert receipt.state == "unknown" and rig.state.calls == ["deleted"]


async def test_restart_pending_claim_does_not_redispatch(rig):
    await rig.executor.execute("a", "s-a", "t", rig.call)
    async with rig.database.engine.begin() as connection:
        await connection.execute(text("UPDATE tool_receipts SET state='pending'"))
    receipt = await rig.make().execute("a", "s-a", "next", rig.call)
    assert receipt.state == "unknown" and len(rig.state.calls) == 1


async def test_browser_lookup_recovers_lost_ack_without_reexecuting(rig):
    rig.state.outcome = TimeoutError("lost acknowledgement")
    with pytest.raises(TimeoutError):
        await rig.executor.execute("a", "s-a", "t", rig.call)
    unknown = await rig.executor.get_receipt("a", "call-1")
    lookups = []

    async def lookup(user_id, call, request_hash):
        lookups.append((user_id, call.id, request_hash))
        return ToolReceipt(
            call.id, "succeeded", {"ok": True, "todos": ["private todo"]}, request_hash
        )

    executor = rig.make(browser_tools={"todo_add"}, reconcile=lookup)
    receipt = await executor.execute("a", "s-a", "next", rig.call)
    assert receipt.state == "succeeded" and receipt.result["todos"] == ["private todo"]
    assert lookups == [("a", "call-1", unknown.request_hash)] and len(rig.state.calls) == 1
    assert (await rig.make().get_receipt("a", "call-1")).state == "succeeded"
    assert "private todo" not in json.dumps(dict((await rig.rows())[0]))


@pytest.mark.parametrize("state", ["unknown", "pending"])
async def test_browser_without_confirmed_outcome_remains_unknown(rig, state):
    rig.state.outcome = TimeoutError("lost acknowledgement")
    with pytest.raises(TimeoutError):
        await rig.executor.execute("a", "s-a", "t", rig.call)

    async def lookup(user_id, call, request_hash):
        return ToolReceipt(call.id, state, {"ok": False, "error": "not confirmed"}, request_hash)

    receipt = await rig.make(browser_tools={"todo_add"}, reconcile=lookup).execute(
        "a", "s-a", "next", rig.call
    )
    assert receipt.state == "unknown" and receipt.result["ok"] is False
    assert len(rig.state.calls) == 1


async def test_failed_receipt_cannot_be_promoted_by_a_later_lookup(rig):
    rig.state.outcome = {"ok": False}
    await rig.executor.execute("a", "s-a", "t", rig.call)
    queried = []

    async def lookup(*args):
        queried.append(True)
        raise AssertionError("A terminal failure must not be replaced")

    receipt = await rig.make(browser_tools={"todo_add"}, reconcile=lookup).execute(
        "a", "s-a", "next", rig.call
    )
    assert receipt.state == "failed" and len(rig.state.calls) == 1 and queried == []


@pytest.mark.parametrize("mismatch", ["id", "hash", "result", "state"])
async def test_invalid_browser_receipt_never_promotes_unknown_to_success(rig, mismatch):
    rig.state.outcome = TimeoutError("lost acknowledgement")
    with pytest.raises(TimeoutError):
        await rig.executor.execute("a", "s-a", "t", rig.call)

    async def lookup(user_id, call, request_hash):
        return ToolReceipt(
            "wrong" if mismatch == "id" else call.id,
            "made_up" if mismatch == "state" else "succeeded",
            {"ok": False} if mismatch == "result" else {"ok": True},
            "wrong" if mismatch == "hash" else request_hash,
        )

    with pytest.raises(ToolCallConflict):
        await rig.make(browser_tools={"todo_add"}, reconcile=lookup).execute(
            "a", "s-a", "next", rig.call
        )
    assert (await rig.executor.get_receipt("a", "call-1")).state == "unknown"
    assert len(rig.state.calls) == 1
