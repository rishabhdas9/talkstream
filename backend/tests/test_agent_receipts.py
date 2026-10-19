from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from langchain.messages import AIMessageChunk, RemoveMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from sqlalchemy import text
from test_paid_boundaries import paid_boundary as paid_boundary

from app.voice.agent import AgentRunner
from app.voice.tool_receipts import ToolCallConflict, ToolExecutor
from app.voice.tools import ClientToolProxy, ExaSearchClient, build_agent_tools


class ScriptedModel(FakeListChatModel):
    script: list
    calls: int = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        value = self.script[self.calls]
        self.calls += 1
        if isinstance(value, dict) and "error" in value:
            request = httpx.Request("POST", "https://fixture.invalid")
            raise httpx.HTTPStatusError(
                "fixture", request=request, response=httpx.Response(value["error"])
            )
        if isinstance(value, dict) and "wait" in value:
            value["entered"].set()
            await value["wait"].wait()
        if isinstance(value, str):
            yield ChatGenerationChunk(message=AIMessageChunk(content=value))
        else:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=value.get("text", "") if isinstance(value, dict) else "",
                    tool_call_chunks=[
                        {
                            "name": call["name"],
                            "args": json.dumps(call["args"]),
                            "id": call["id"],
                            "index": index,
                        }
                        for index, call in enumerate(
                            value["calls"] if isinstance(value, dict) else value
                        )
                    ],
                )
            )
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            )
        )


def delete(call_id="provider-1", todo_id="todo-42"):
    return {"id": call_id, "name": "todo_delete", "args": {"todo_id": todo_id}}


async def collect(subject, *, turn_id=None, transcript="Delete the item", **options):
    async def noop(*args):
        pass

    kwargs = {"turn_id": turn_id} if turn_id is not None else {}
    return "".join(
        [
            chunk
            async for chunk in subject.stream_reply(
                transcript,
                observer=noop,
                tool_observer=noop,
                **kwargs,
                **options,
            )
        ]
    )


@pytest.fixture
async def agent_rig(paid_boundary):
    b = paid_boundary
    state = SimpleNamespace(mode="ok", effects=[], ordering=[], messages=[], queries=[])
    proxy = None

    async def rows():
        async with b.costs.database.engine.connect() as connection:
            return (await connection.execute(text("SELECT * FROM tool_receipts"))).mappings().all()

    async def sender(message):
        state.messages.append(message)
        if message["type"] == "tool_request":
            saved = [row for row in await rows() if row["call_id"] == message["call_id"]]
            state.ordering.append(saved[0]["state"] if saved else "unclaimed")
            state.effects.append((message["name"], message["arguments"]))
            if state.mode == "ok":
                todo = {
                    "id": "todo-42",
                    "text": message["arguments"].get("text", "fixture item"),
                    "completed": False,
                    "createdAt": "2026-10-18T12:00:00Z",
                }
                result = (
                    {"ok": True, "deleted": todo, "todos": []}
                    if message["name"] == "todo_delete"
                    else {"ok": True, "todo": todo, "todos": [todo]}
                )
                proxy.resolve(call_id=message["call_id"], result=result)
        elif message["type"] == "tool_receipt_query":
            state.queries.append(message)
            result = None
            if state.mode == "lost_then_confirm":
                result = {
                    "call_id": message["call_id"],
                    "request_hash": message["request_hash"],
                    "state": "succeeded",
                    "result": {
                        "ok": True,
                        "deleted": {
                            "id": "todo-42",
                            "text": "fixture item",
                            "completed": False,
                            "createdAt": "2026-10-18T12:00:00Z",
                        },
                        "todos": [],
                    },
                }
            proxy.resolve(call_id=message["request_id"], result=result)

    proxy = ClientToolProxy(sender=sender, timeout_seconds=0.02, owner_id="user")
    exa = ExaSearchClient(
        api_key=None,
        timeout_seconds=1,
        costs=b.costs,
        scope=b.scope,
        expires_at=b.costs.clock.utc_now() + timedelta(seconds=120),
    )
    core_tools = build_agent_tools(exa_client=exa, todo_proxy=proxy)

    def make(script, *, tools=None, lookup=False):
        kwargs = {"receipt_lookup": proxy.reconcile} if lookup else {}
        state.model = ScriptedModel(responses=["unused"], script=script)
        return AgentRunner(
            api_key="unused",
            base_url="https://fixture.invalid",
            model_name="sarvam-105b",
            thread_id="receipt-thread",
            tools=core_tools if tools is None else tools,
            costs=b.costs,
            scope=b.scope,
            expires_at=b.costs.clock.utc_now() + timedelta(seconds=120),
            chat_model=state.model,
            **kwargs,
        )

    yield SimpleNamespace(
        make=make, state=state, rows=rows, proxy=proxy, boundary=b, tools=core_tools
    )
    await exa.close()


async def test_actual_agent_claims_before_browser_and_preserves_provider_pairs(agent_rig):
    r = agent_rig
    subject = r.make([[delete()], "Deleted."])
    assert await collect(subject, turn_id="call:1", generation_id="generation-1") == "Deleted."
    assert r.state.effects == [("todo_delete", {"todo_id": "todo-42"})]
    assert r.state.ordering == ["pending"]
    row = (await r.rows())[0]
    wire = r.state.messages[0]
    assert wire["turn_id"] == "call:1" and wire["generation_id"] == "generation-1"
    assert wire["call_id"] == row["call_id"] and wire["call_id"] != "provider-1"
    assert wire["request_hash"] == row["request_hash"]
    assert row["user_id"] == "user" and row["session_id"] == "call" and row["state"] == "succeeded"
    assert "todo-42" not in json.dumps(dict(row))
    snapshot = await subject._agent.aget_state(subject._config())
    messages = snapshot.values["messages"]
    calls = [
        call["id"] for message in messages if message.type == "ai" for call in message.tool_calls
    ]
    results = [message for message in messages if message.type == "tool"]
    assert calls == ["provider-1"] and [message.tool_call_id for message in results] == calls
    payload = json.loads(results[0].content)
    assert payload["state"] == "succeeded" and payload["call_id"] == row["call_id"]


async def test_same_provider_id_is_cached_and_changed_arguments_conflict(agent_rig):
    r = agent_rig
    subject = r.make([[delete()], [delete()], "Done."])
    assert await collect(subject, turn_id="call:1") == "Done."
    assert len(r.state.effects) == 1 and len(await r.rows()) == 1
    snapshot = await subject._agent.aget_state(subject._config())
    receipts = [
        json.loads(message.content)
        for message in snapshot.values["messages"]
        if message.type == "tool"
    ]
    assert [receipt["replayed"] for receipt in receipts] == [False, True]
    changed = r.make([[delete(todo_id="other")], "must not run"])
    with pytest.raises(ToolCallConflict):
        await collect(changed, turn_id="call:1")
    assert len(r.state.effects) == 1


@pytest.mark.parametrize("mode,expected", [("lost", "unknown"), ("lost_then_confirm", "succeeded")])
async def test_new_provider_id_after_lost_reply_reconciles_without_another_action(
    agent_rig, mode, expected
):
    r = agent_rig
    r.state.mode = mode
    subject = r.make(
        [[delete()], [delete("new-provider-id")], "The receipt was checked."], lookup=True
    )
    assert await collect(subject, turn_id="call:1") == "The receipt was checked."
    assert len(r.state.effects) == 1 and len(r.state.queries) == 1
    rows = await r.rows()
    assert len(rows) == 1 and rows[0]["state"] == expected
    assert r.state.queries[0]["call_id"] == rows[0]["call_id"]
    snapshot = await subject._agent.aget_state(subject._config())
    results = [
        json.loads(message.content)
        for message in snapshot.values["messages"]
        if message.type == "tool"
    ]
    assert [item["state"] for item in results] == ["unknown", expected]
    assert results[1]["replayed"] is True and results[1]["call_id"] == results[0]["call_id"]


async def test_strict_schema_blocks_model_owned_identity_before_browser(agent_rig):
    r = agent_rig
    call = delete()
    call["args"]["user_id"] = "other-owner"
    subject = r.make([[call], "Rejected."])
    assert await collect(subject) == "Rejected."
    assert r.state.effects == [] and await r.rows() == []


async def test_registered_addon_uses_same_receipt_boundary(agent_rig):
    r = agent_rig
    seen = []

    @tool
    async def knowledge_search(query: str) -> str:
        """Find a private document excerpt."""
        seen.extend(await r.rows())
        return json.dumps({"ok": True, "results": [{"excerpt": "private content"}]})

    call = {"id": "addon-id", "name": "knowledge_search", "args": {"query": "private question"}}
    assert await collect(r.make([[call], "Found."], tools=[knowledge_search])) == "Found."
    assert len(seen) == 1 and seen[0]["state"] == "pending"
    row = (await r.rows())[0]
    assert row["tool_name"] == "knowledge_search" and row["state"] == "succeeded"
    assert "private" not in json.dumps(dict(row))


async def test_parallel_tools_keep_their_own_runtime_identity(agent_rig):
    r = agent_rig
    script = [
        [
            {"id": "add-a", "name": "todo_add", "args": {"text": "first"}},
            {"id": "add-b", "name": "todo_add", "args": {"text": "second"}},
        ],
        "Added.",
    ]
    assert await collect(r.make(script), turn_id="call:1") == "Added."
    assert len(r.state.effects) == 2 and r.state.ordering == ["pending", "pending"]
    rows = {row["call_id"]: row for row in await r.rows()}
    assert len(rows) == 2 and all(row["turn_id"] == "call:1" for row in rows.values())
    assert len({row["request_hash"] for row in rows.values()}) == 2
    assert all(
        message["request_hash"] == rows[message["call_id"]]["request_hash"]
        for message in r.state.messages
    )


async def test_database_wait_cannot_start_an_action_after_call_expiry(agent_rig, monkeypatch):
    r = agent_rig
    claim = ToolExecutor._claim
    now = r.boundary.costs.clock.utc_now()

    async def delayed_claim(self, *args):
        receipt = await claim(self, *args)
        # Preserve the real durable claim, then inject time spent waiting on its writer.
        monkeypatch.setattr(r.boundary.costs.clock, "utc_now", lambda: now + timedelta(seconds=121))
        return receipt

    monkeypatch.setattr(ToolExecutor, "_claim", delayed_claim)
    with pytest.raises(RuntimeError, match="deadline"):
        await collect(r.make([[delete()], "must not execute"]))
    assert r.state.effects == []
    assert (await r.rows())[0]["state"] == "unknown"


async def test_cached_proposals_cannot_bypass_three_tool_turn_limit(agent_rig):
    r = agent_rig
    subject = r.make([[delete()], [delete()], [delete()], [delete()], "must not continue"])
    with pytest.raises(RuntimeError, match="tool.*limit"):
        await collect(subject)
    assert len(r.state.effects) == 1


def orphan_tool_result_ids(messages):
    pending = []
    orphan = []
    for message in messages:
        if message.type == "ai":
            pending.extend(call["id"] for call in message.tool_calls)
        if message.type == "tool":
            if message.tool_call_id in pending:
                pending.remove(message.tool_call_id)
            else:
                orphan.append(message.tool_call_id)
    return orphan, pending


async def test_reconciliation_trims_combined_prose_and_keeps_committed_delete(agent_rig):
    r = agent_rig
    subject = r.make([{"text": "Unheard preamble.", "calls": [delete()]}, "Unheard final."])
    await collect(subject, turn_id="call:1")
    await subject.reconcile_interrupted_history("Heard only.")
    snapshot = await subject._agent.aget_state(subject._config())
    messages = snapshot.values["messages"]
    assert [
        message.content for message in messages if message.type == "ai" and message.content
    ] == ["Heard only."]
    assert orphan_tool_result_ids(messages) == ([], [])
    assert (
        json.loads(next(message.content for message in messages if message.type == "tool"))["state"]
        == "succeeded"
    )
    assert len(r.state.effects) == 1


async def test_missing_canonical_result_recovers_the_committed_receipt(agent_rig):
    r = agent_rig
    subject = r.make([[delete()], "Unheard."])
    await collect(subject, turn_id="call:1")
    snapshot = await subject._agent.aget_state(subject._config())
    result = next(message for message in snapshot.values["messages"] if message.type == "tool")
    await subject._agent.aupdate_state(
        subject._config(), {"messages": [RemoveMessage(id=result.id)]}
    )
    subject._last_budget.receipts.clear()
    await subject.reconcile_interrupted_history("")
    snapshot = await subject._agent.aget_state(subject._config())
    recovered = [message for message in snapshot.values["messages"] if message.type == "tool"]
    assert len(recovered) == 1 and json.loads(recovered[0].content)["state"] == "succeeded"
    assert json.loads(recovered[0].content)["result_available"] is False
    assert orphan_tool_result_ids(snapshot.values["messages"]) == ([], [])
    assert len(r.state.effects) == 1


async def test_missing_result_for_reused_id_never_borrows_other_arguments_success(agent_rig):
    r = agent_rig
    subject = r.make([[delete()], [delete(todo_id="different")], "must not run"])
    with pytest.raises(ToolCallConflict):
        await collect(subject, turn_id="call:1")
    await subject.reconcile_interrupted_history("")
    snapshot = await subject._agent.aget_state(subject._config())
    results = [
        json.loads(message.content)
        for message in snapshot.values["messages"]
        if message.type == "tool"
    ]
    assert [result["state"] for result in results] == ["succeeded", "failed"]
    assert results[1]["ok"] is False and orphan_tool_result_ids(snapshot.values["messages"]) == (
        [],
        [],
    )
    assert len(r.state.effects) == 1 and (await r.rows())[0]["state"] == "succeeded"


async def test_real_cancellation_after_delete_does_not_replay_on_next_turn(agent_rig):
    r = agent_rig
    entered = asyncio.Event()
    subject = r.make([[delete()], {"entered": entered, "wait": asyncio.Event()}, "Hello."])
    task = asyncio.create_task(collect(subject, turn_id="call:1"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await subject.reconcile_interrupted_history("")
    assert await collect(subject, turn_id="call:2", transcript="Say hello") == "Hello."
    snapshot = await subject._agent.aget_state(subject._config())
    assert orphan_tool_result_ids(snapshot.values["messages"]) == ([], [])
    assert len(r.state.effects) == 1 and (await r.rows())[0]["state"] == "succeeded"


async def test_answer_recovery_cannot_replay_a_tool_under_a_new_id(agent_rig):
    r = agent_rig
    subject = r.make([[delete()], {"error": 503}, [delete("retry-id")], "must not execute"])
    with pytest.raises(RuntimeError, match="recovery"):
        await collect(subject)
    assert len(r.state.effects) == 1 and r.state.model.calls == 3
    assert len(await r.boundary.rows()) == 3


async def test_only_one_model_recovery_is_available_across_the_turn(agent_rig):
    r = agent_rig
    subject = r.make([{"error": 503}, [delete()], {"error": 503}, "must not retry"])
    with pytest.raises(httpx.HTTPStatusError):
        await collect(subject)
    assert r.state.model.calls == 3 and len(r.state.effects) == 1


async def test_no_answer_recovery_after_visible_prose(agent_rig):
    r = agent_rig
    subject = r.make(
        [{"text": "I will check.", "calls": [delete()]}, {"error": 503}, "must not rewrite"]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await collect(subject)
    assert r.state.model.calls == 2 and len(r.state.effects) == 1


@pytest.mark.parametrize(
    "name,first,second,effective",
    [
        ("todo_add", {"text": " Buy   milk "}, {"text": "Buy milk"}, {"text": "Buy milk"}),
        ("todo_add", {"text": "x" * 500 + "a"}, {"text": "x" * 500 + "b"}, {"text": "x" * 500}),
        (
            "todo_add",
            {"text": "a" * 499 + " b"},
            {"text": "a" * 499},
            {"text": "a" * 499},
        ),
        ("todo_delete", {"todo_id": " todo-42 "}, {"todo_id": "todo-42"}, {"todo_id": "todo-42"}),
    ],
)
async def test_equivalent_effective_arguments_cannot_repeat_uncertain_mutation(
    agent_rig, name, first, second, effective
):
    r = agent_rig
    r.state.mode = "lost"
    subject = r.make(
        [
            [{"id": "first", "name": name, "args": first}],
            [{"id": "second", "name": name, "args": second}],
            "Unconfirmed.",
        ],
        lookup=True,
    )
    assert await collect(subject) == "Unconfirmed."
    assert r.state.effects == [(name, effective)]
    rows = await r.rows()
    assert len(rows) == 1 and len(r.state.queries) == 1
    assert r.state.messages[0]["request_hash"] == rows[0]["request_hash"]


async def test_invalid_proposal_yields_failed_result_and_keeps_next_turn_usable(agent_rig):
    r = agent_rig
    call = {"id": "empty", "name": "todo_add", "args": {"text": "   "}}
    subject = r.make([[call], "What should I add?", "Hello."])
    assert await collect(subject) == "What should I add?"
    assert r.state.effects == [] and await r.rows() == []
    snapshot = await subject._agent.aget_state(subject._config())
    messages = snapshot.values["messages"]
    assert orphan_tool_result_ids(messages) == ([], [])
    result = json.loads(next(message for message in messages if message.type == "tool").content)
    assert result["ok"] is False and result["state"] == "failed"
    assert await collect(subject, transcript="Say hello", turn_id="next") == "Hello."


async def test_canonical_history_trims_whole_older_exchanges(agent_rig):
    r = agent_rig

    @tool
    async def knowledge_search(query: str) -> str:
        """Return an excerpt."""
        return json.dumps({"ok": True, "results": [{"excerpt": "f" * 1200}]})

    script = []
    for index in range(4):
        script.extend(
            [
                [{"id": f"k-{index}", "name": "knowledge_search", "args": {"query": "facts"}}],
                "Answer.",
            ]
        )
    subject = r.make(script, tools=[knowledge_search])
    for index in range(4):
        await collect(subject, turn_id=f"call:{index}", transcript=f"request-{index}:" + "x" * 600)
    snapshot = await subject._agent.aget_state(subject._config())
    messages = snapshot.values["messages"]
    assert not any(
        message.type == "human" and message.content.startswith("request-0:") for message in messages
    )
    assert any(
        message.type == "human" and message.content.startswith("request-3:") for message in messages
    )
    assert orphan_tool_result_ids(messages) == ([], [])


async def test_large_tool_result_requests_restatement_and_never_replays(agent_rig):
    r = agent_rig
    effects = []

    @tool
    async def knowledge_search(query: str) -> str:
        """Return an excerpt."""
        effects.append(query)
        return json.dumps({"ok": True, "results": [{"excerpt": "x" * 7000}]})

    call = {"id": "large", "name": "knowledge_search", "args": {"query": "facts"}}
    subject = r.make([[call], "Hello."], tools=[knowledge_search])
    assert "restate" in (await collect(subject)).lower()
    assert r.state.model.calls == 1 and (await r.rows())[0]["state"] == "succeeded"
    snapshot = await subject._agent.aget_state(subject._config())
    assert orphan_tool_result_ids(snapshot.values["messages"]) == ([], [])
    assert await collect(subject, transcript="Say hello") == "Hello."
    assert effects == ["facts"] and r.state.model.calls == 2


async def test_todo_text_limit_does_not_split_a_surrogate_pair(agent_rig):
    r = agent_rig
    call = {"id": "unicode", "name": "todo_add", "args": {"text": "a" * 499 + "😀"}}
    assert await collect(r.make([[call], "Added."])) == "Added."
    assert r.state.effects == [("todo_add", {"text": "a" * 499})]
    assert (await r.rows())[0]["state"] == "succeeded"
