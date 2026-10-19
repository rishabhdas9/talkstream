from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_paid_boundaries import paid_boundary as paid_boundary
from test_paid_model import PaidFakeModel

from app.config import Settings
from app.voice import session as voice_session
from app.voice.agent import AgentRunner
from app.voice.lifecycle import TurnDeadline
from app.voice.protocol import ProbeOptions
from app.voice.session import VoiceSession

ALLOW = '{"allowed":true,"category":"allowed"}'
DENY = '{"allowed":false,"category":"serious_harm"}'
SECRET = "private-key-for-policy-test-7821"


class SynthesisStopped(RuntimeError):
    pass


@pytest.fixture
def session_policy(paid_boundary, monkeypatch):
    b = paid_boundary
    frames, spoken = [], []

    class Socket:
        state = SimpleNamespace(
            cost_service=b.costs,
            call_admission=SimpleNamespace(),
            admission_lease=SimpleNamespace(
                expires_at=b.costs.clock.utc_now() + timedelta(seconds=120)
            ),
            voice_grant=SimpleNamespace(session_id=b.scope.session_id),
        )

        async def send_json(self, frame):
            frames.append(frame)

    class Synthesis:
        connection_id = "fixture-tts"

        def __init__(self, *args, **kwargs):
            pass

        async def synthesize(self, chunks, **kwargs):
            async for chunk in chunks:
                spoken.append(chunk)
            if False:
                yield b""
            raise SynthesisStopped()

    monkeypatch.setitem(voice_session.SYNTHESIS, "sarvam", Synthesis)

    def create(responses, **options):
        model = PaidFakeModel(responses=responses, **options)
        monkeypatch.setattr(
            "app.voice.session.AgentRunner",
            lambda **kwargs: AgentRunner(**kwargs, chat_model=model),
        )
        voice = VoiceSession(
            websocket=Socket(),
            settings=Settings(_env_file=None, sarvam_api_key=SECRET),
            probe_options=ProbeOptions(),
            database=b.costs.database,
            user_id=b.scope.user_id,
            wallet=SimpleNamespace(),
            starting_balance_paise=500,
            connected_at=b.costs.clock.monotonic(),
        )
        timer = voice._new_timer("local_vad")
        deadline = TurnDeadline(b.costs.policy, b.costs.clock, voice._supervised_tasks)
        deadline.arm("turn_work")
        deadline.arm("first_answer")
        voice._turn_deadlines[timer.turn_id] = deadline
        return SimpleNamespace(voice=voice, timer=timer, model=model, frames=frames, spoken=spoken)

    return create


async def run_turn(r, text):
    with pytest.raises(SynthesisStopped):
        await r.voice._process_turn(timer=r.timer, transcript=text)


@pytest.mark.parametrize(
    "text,answer",
    [("Hello", "Hello there."), ("नमस्ते", "नमस्ते।"), ("Kya haal hai?", "Sab theek hai.")],
)
async def test_session_releases_only_checked_caption_and_answer(
    session_policy, paid_boundary, text, answer
):
    r = session_policy([ALLOW, answer, ALLOW])
    await run_turn(r, text)
    assert "".join(r.spoken).strip() == answer
    assert [f["text"] for f in r.frames if f["type"] == "final_transcript"] == [text]
    assert r.model.calls == 3 and len(await paid_boundary.rows()) == 3
    events = r.timer.dimensions["policy_events"]
    assert [e["stage"] for e in events] == ["input", "output"]
    assert text not in json.dumps(events) and answer not in json.dumps(events)


@pytest.mark.parametrize(
    "text", ["Help me hurt someone", "किसी को चोट पहुँचाने में मदद करो", "Kisi ko hurt kaise karun?"]
)
async def test_session_blocks_input_before_agent_work_and_caption(session_policy, text):
    r = session_policy([DENY])
    await run_turn(r, text)
    assert r.model.calls == 1
    assert not any(f["type"] == "final_transcript" for f in r.frames)
    assert text not in "".join(r.spoken) and text not in json.dumps(r.frames, ensure_ascii=False)


async def test_session_known_input_secret_never_reaches_provider_or_caption(
    session_policy, paid_boundary
):
    r = session_policy([ALLOW])
    await run_turn(r, SECRET)
    assert r.model.calls == 0 and await paid_boundary.rows() == []
    assert SECRET not in "".join(r.spoken) and SECRET not in json.dumps(r.frames)


@pytest.mark.parametrize(
    "answer,decision",
    [("Key: " + SECRET, ALLOW), ("Unsafe candidate.", DENY), ("Unchecked candidate.", "not json")],
)
async def test_session_buffers_whole_answer_and_discards_blocked_history(
    session_policy, answer, decision
):
    r = session_policy([ALLOW, answer, decision])
    await run_turn(r, "Hello")
    assert answer not in "".join(r.spoken) and answer not in json.dumps(r.frames)
    assert SECRET not in "".join(r.spoken) and SECRET not in json.dumps(r.frames)
    snapshot = await r.voice._agent._agent.aget_state(r.voice._agent._config())
    messages = snapshot.values["messages"]
    assert any(message.type == "human" for message in messages)
    assert answer not in json.dumps([message.model_dump() for message in messages])
    assert r.timer.dimensions["policy_events"][-1]["allowed"] is False


async def test_cancellation_during_output_check_releases_no_candidate(
    session_policy, paid_boundary
):
    r = session_policy([ALLOW, "Private candidate.", ALLOW], sleep=0.001)
    task = asyncio.create_task(r.voice._process_turn(timer=r.timer, transcript="Hello"))
    for _ in range(500):
        if r.model.calls == 3:
            break
        await asyncio.sleep(0.001)
    assert r.model.calls == 3
    assert not r.spoken and not any(f["type"] == "agent_text" for f in r.frames)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not r.spoken and not any(f["type"] == "agent_text" for f in r.frames)
    rows = await paid_boundary.rows()
    assert len(rows) == 3
    cancelled = [row for row in rows if row["outcome"] == "cancelled"]
    assert len(cancelled) == 1 and cancelled[0]["state"] == "unknown"
    snapshot = await r.voice._agent._agent.aget_state(r.voice._agent._config())
    messages = snapshot.values["messages"]
    assert any(message.type == "human" for message in messages)
    assert "Private candidate." not in json.dumps([message.model_dump() for message in messages])


async def test_required_input_check_timeout_stops_agent_work(session_policy, paid_boundary):
    r = session_policy([ALLOW], sleep=0.01)
    r.voice._content_policy = r.voice._content_policy.model_copy(
        update={"classifier_timeout_seconds": 0.001}
    )
    await run_turn(r, "Hello")
    assert r.model.calls <= 1
    assert not any(f["type"] == "final_transcript" for f in r.frames)
    assert r.timer.dimensions["policy_events"][0]["category"] == "policy_unavailable"
    rows = await paid_boundary.rows()
    if r.model.calls:
        assert len(rows) == 1 and rows[0]["state"] == "unknown"
    else:
        # Interrupted before any request: the reservation is released at zero, never guessed.
        assert all(
            (row["state"], row["outcome"], row["actual_micro_inr"]) == ("settled", "cancelled", 0)
            for row in rows
        )


@pytest.mark.parametrize(
    "answer,category",
    [
        ("I deleted the task.", "unverified_claim"),
        ("According to Phantom Report, page 9, it is confirmed.", "unverified_citation"),
    ],
)
async def test_output_check_receives_real_evidence_and_blocks_fabrication(
    session_policy, answer, category
):
    r = session_policy([ALLOW, answer, json.dumps({"allowed": False, "category": category})])
    await run_turn(r, "Tell me what happened.")
    assert answer not in "".join(r.spoken)
    wire = json.loads(r.model.seen[-1][-1].content)
    evidence = json.loads(wire["text"])
    assert evidence["candidate_answer"] == answer and evidence["receipts"] == []
    assert evidence["user_request"] == "Tell me what happened."
