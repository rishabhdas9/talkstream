from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from test_paid_boundaries import paid_boundary as paid_boundary

from app.voice.agent import AgentRunner, truncate_text_to_played_audio
from app.voice.chunker import SentenceChunker


def runner(responses: list[str], *, boundary, sleep: float | None = None) -> AgentRunner:
    return AgentRunner(
        api_key="unused-fake-key",
        base_url="https://unused.invalid/v1",
        model_name="sarvam-105b",
        thread_id="call-scoped-thread",
        tools=[],
        costs=boundary.costs,
        scope=boundary.scope,
        expires_at=boundary.costs.clock.utc_now() + timedelta(seconds=120),
        chat_model=FakeListChatModel(responses=responses, sleep=sleep),
    )


async def collect_reply(subject: AgentRunner, transcript: str):
    observations: list[tuple[str, float]] = []
    spans: list[dict[str, object]] = []

    async def observer(name: str, timestamp: float, details: dict[str, object]) -> None:
        del details
        observations.append((name, timestamp))

    async def tool_observer(span: dict[str, object]) -> None:
        spans.append(span)

    chunks = [
        chunk
        async for chunk in subject.stream_reply(
            transcript,
            observer=observer,
            tool_observer=tool_observer,
        )
    ]
    return "".join(chunks), observations, spans


async def test_fake_chat_model_exposes_stream_boundaries_and_history(paid_boundary) -> None:
    subject = runner(["A short spoken reply.", "A follow-up reply."], boundary=paid_boundary)

    text, observations, spans = await collect_reply(subject, "Hello")
    follow_up, _, _ = await collect_reply(subject, "Continue")

    names = [name for name, _ in observations]
    assert text == "A short spoken reply."
    assert follow_up == "A follow-up reply."
    assert names[0] == "t_llm_request_start"
    assert names.count("t_llm_first_visible_token") == 1
    assert names[-1] == "t_llm_complete"
    assert spans == []


async def test_first_speakable_boundary_is_observable_at_chunker_edge(paid_boundary) -> None:
    subject = runner(
        ["This is long enough to become a speakable sentence."], boundary=paid_boundary
    )
    observed: list[str] = []
    chunker = SentenceChunker(min_chars=10)

    async def observer(name: str, timestamp: float, details: dict[str, object]) -> None:
        del timestamp, details
        observed.append(name)

    async def tool_observer(span: dict[str, object]) -> None:
        del span

    speakable: list[str] = []
    async for token in subject.stream_reply(
        "Speak",
        observer=observer,
        tool_observer=tool_observer,
    ):
        for chunk in chunker.push(token):
            if not speakable:
                observed.append("t_llm_first_speakable_chunk")
            speakable.append(chunk)

    assert speakable == ["This is long enough to become a speakable sentence."]
    assert observed.index("t_llm_first_visible_token") < observed.index(
        "t_llm_first_speakable_chunk"
    )


async def test_stream_cancellation_stops_fake_model(paid_boundary) -> None:
    subject = runner(["This response streams slowly."], sleep=0.05, boundary=paid_boundary)

    async def observer(name: str, timestamp: float, details: dict[str, object]) -> None:
        del name, timestamp, details

    async def tool_observer(span: dict[str, object]) -> None:
        del span

    async def consume() -> None:
        async for _ in subject.stream_reply(
            "Start",
            observer=observer,
            tool_observer=tool_observer,
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.08)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_interrupted_history_removes_unspoken_assistant_message(paid_boundary) -> None:
    subject = runner(["The generated answer should be truncated."], boundary=paid_boundary)
    await collect_reply(subject, "Question")

    result = await subject.reconcile_interrupted_history("The generated")

    assert result["removed_assistant_messages"] == 1
    assert result["retained_spoken_characters"] == len("The generated")


@pytest.mark.parametrize(
    ("played_ms", "generated_ms", "expected"),
    [(0, 1000, ""), (1000, 1000, "one two three four"), (500, 1000, "one two")],
)
def test_text_truncation_tracks_played_audio_ratio(
    played_ms: float,
    generated_ms: float,
    expected: str,
) -> None:
    assert (
        truncate_text_to_played_audio(
            "one two three four",
            played_audio_ms=played_ms,
            generated_audio_ms=generated_ms,
        )
        == expected
    )
