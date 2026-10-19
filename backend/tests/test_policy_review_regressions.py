import asyncio
import json

import pytest
from langchain.messages import AIMessage, HumanMessage
from test_agent_receipts import agent_rig as agent_rig
from test_agent_receipts import collect, delete
from test_paid_boundaries import paid_boundary as paid_boundary
from test_policy_tools import binding
from test_session_policy import SECRET
from test_session_policy import session_policy as session_policy


async def test_reconciled_result_is_checked_before_history(agent_rig):
    r = agent_rig
    r.state.mode = "lost_then_confirm"
    original = r.proxy.resolve

    def resolve(*, call_id, result):
        if isinstance(result, dict) and "result" in result:
            result = {**result, "result": {**result["result"], "debug": "sk-tool-private-12345"}}
        return original(call_id=call_id, result=result)

    r.proxy.resolve = resolve
    options, signals, events = binding(r)
    subject = r.make([[delete()], [delete("second")], "Checked."], lookup=True)
    await collect(subject, turn_id="policy-turn", transcript="Delete todo-42", **options)
    snapshot = await subject._agent.aget_state(subject._config())
    tools = [m.content for m in snapshot.values["messages"] if m.type == "tool"]
    assert "sk-tool-private-12345" not in json.dumps(tools)
    assert any(event.category == "credential_disclosure" for event in events)
    assert len([s for s in signals if "reference_result" in s]) == 1


async def test_denied_unknown_result_preserves_outcome_and_deduplication(agent_rig):
    r = agent_rig
    r.state.mode = "lost"
    options, _, _ = binding(r)

    async def classify(stage, text, context):
        return (
            {"allowed": False, "category": "policy_unavailable"}
            if "reference_result" in json.loads(text)
            else {"allowed": True, "category": "allowed"}
        )

    options["policy"]._classifier = classify
    subject = r.make([[delete()], [delete("second")], "Unconfirmed."], lookup=True)
    await collect(subject, turn_id="policy-turn", transcript="Delete todo-42", **options)
    assert len(r.state.effects) == 1
    assert [row["state"] for row in await r.rows()] == ["unknown"]


@pytest.mark.parametrize("cancelled", [False, True])
async def test_denied_candidate_preserves_successful_tool_history(
    agent_rig, session_policy, cancelled
):
    r = agent_rig
    voice_rig = session_policy([])
    candidate = "Unapproved candidate." if cancelled else "Key: " + SECRET
    subject = r.make([[delete()], candidate])
    voice_rig.voice._agent = subject
    await subject._agent.aupdate_state(
        subject._config(),
        {
            "messages": [
                HumanMessage(content="Earlier question."),
                AIMessage(content="Earlier approved answer."),
            ]
        },
    )

    entered = asyncio.Event()

    async def classify(stage, *args, **kwargs):
        if cancelled and stage == "output":
            entered.set()
            await asyncio.Event().wait()
        return '{"allowed":true,"category":"allowed"}'

    subject.classify_policy = classify

    async def noop(*args):
        pass

    work = voice_rig.voice._approved_answer(
        voice_rig.timer, "Delete todo-42", observer=noop, tool_observer=noop
    )
    if cancelled:
        task = asyncio.create_task(work)
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert SECRET not in await work
    snapshot = await subject._agent.aget_state(subject._config())
    assert len(r.state.effects) == 1 and (await r.rows())[0]["state"] == "succeeded"
    messages = snapshot.values["messages"]
    assert any(m.content == "Earlier approved answer." for m in messages)
    assert any(m.type == "tool" and json.loads(m.content)["state"] == "succeeded" for m in messages)
    assert SECRET not in json.dumps([m.model_dump() for m in messages])
    assert candidate not in json.dumps([m.model_dump() for m in messages])
    await subject.reconcile_interrupted_history("")
    after = (await subject._agent.aget_state(subject._config())).values["messages"]
    assert any(m.type == "tool" for m in after)
    assert any(m.content == "Earlier approved answer." for m in after)


def test_stt_vendor_error_returns_safe_terminal_error(tmp_path):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from test_websocket_protocol import ORIGIN, build_app, ticket_url

    from app.voice.session import VoiceSession

    class Stt:
        async def messages(self):
            yield {"type": "error", "data": {"message": SECRET}}

    class ErrorSession:
        def __init__(self, **kwargs):
            self._stt = Stt()
            self._probe = SimpleNamespace(enabled=False)
            self._session_id = "fixture-session"
            self._current_turn_id = lambda: None

        async def run(self):
            await VoiceSession._receive_stt_final(self)

    app, token = build_app(tmp_path, balance_paise=500, session_factory=ErrorSession)
    with (
        TestClient(app) as client,
        client.websocket_connect(ticket_url(client, token), headers=ORIGIN) as ws,
    ):
        message = ws.receive_json()
        assert message["type"] == "error" and SECRET not in message["message"]
        assert message["code"] == "voice_session_error"


@pytest.mark.parametrize("duplicate_label", [False, True])
async def test_two_clear_distinct_names_are_supported(agent_rig, duplicate_label):
    r = agent_rig
    original = r.proxy.resolve

    def resolve(*, call_id, result):
        if isinstance(result, dict) and "todos" in result:
            result = {
                **result,
                "todos": [
                    {
                        "id": "todo-42",
                        "text": "fixture item",
                        "completed": False,
                        "createdAt": "2026-10-18T12:00:00Z",
                    },
                    {
                        "id": "todo-43",
                        "text": "fixture item" if duplicate_label else "second item",
                        "completed": False,
                        "createdAt": "2026-10-18T12:00:00Z",
                    },
                ],
            }
        return original(call_id=call_id, result=result)

    r.proxy.resolve = resolve
    options, _, events = binding(r)
    subject = r.make(
        [
            [{"id": "list", "name": "todo_list", "args": {}}],
            [delete("del1", "todo-42"), delete("del2", "todo-43")],
            "Please clarify.",
        ]
    )
    await collect(
        subject, turn_id="policy-turn", transcript="Delete fixture item and second item", **options
    )
    if duplicate_label:
        assert r.state.effects == [("todo_list", {})]
        assert [e.category for e in events].count("clarification") == 2
        return
    assert len(r.state.effects) == 3
    assert {args["todo_id"] for name, args in r.state.effects if name == "todo_delete"} == {
        "todo-42",
        "todo-43",
    }
    assert all(e.category != "clarification" for e in events)
