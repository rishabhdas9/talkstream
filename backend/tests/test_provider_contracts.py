from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from test_paid_boundaries import paid_boundary as paid_boundary
from test_paid_model import PaidFakeModel, collect, runner
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.http11 import Response

from app.costs.models import CostScope
from app.costs.rates import RateUnavailable
from app.voice.providers.contracts import (
    AudioChunk,
    ProviderContext,
    SttEvent,
    TextEvent,
    check_endpointing,
    classify_failure,
)
from app.voice.providers.sarvam import AgentTextGenerator, SarvamRecognizer, SarvamSynthesizer


def context(b, generation_id="g1"):
    return ProviderContext(
        scope=b.scope,
        deadline=b.costs.clock.utc_now() + timedelta(seconds=120),
        turn_id="turn-1",
        generation_id=generation_id,
    )


async def texts(*values):
    for value in values:
        yield value


async def test_adapter_close_discards_generation(paid_boundary):
    class StubbornClient:
        """Keeps yielding after discard, as a slow socket might."""

        connection_id = "fixture"
        reasons: list[str] = []

        async def synthesize(self, chunks, **kwargs):
            async for _ in chunks:
                pass
            for _ in range(50):
                await asyncio.sleep(0)
                yield b"\x01\x00"

        async def close_and_discard(self, reason="interrupted"):
            self.reasons.append(reason)

    client = StubbornClient()
    synthesizer = SarvamSynthesizer(client, codec="linear16", sample_rate_hz=24000)
    stream = synthesizer.synthesize(texts("hello"), context(paid_boundary))
    assert await anext(stream) == AudioChunk(
        generation_id="g1", data=b"\x01\x00", codec="linear16", sample_rate_hz=24000
    )
    await synthesizer.close_and_discard("barge-in")
    assert [chunk async for chunk in stream] == []
    assert client.reasons == ["barge-in"]


async def test_usage_is_recorded_on_adapter_failure(paid_boundary, monkeypatch):
    from app.voice import tts as module

    class Connection:
        async def send(self, raw):
            pass

        async def recv(self):
            raise ConnectionClosedError(None, None)

        async def close(self, **kwargs):
            pass

    class Transport:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: Transport())
    synthesizer = SarvamSynthesizer(paid_boundary.client, codec="linear16", sample_rate_hz=24000)
    with pytest.raises(ConnectionClosedError) as failed:
        [chunk async for chunk in synthesizer.synthesize(texts("नमस्ते"), context(paid_boundary))]
    rows = await paid_boundary.rows()
    assert len(rows) == 1
    assert rows[0]["state"] == "unknown" and rows[0]["outcome"] == "failed"
    assert classify_failure(failed.value, "sarvam").category == "transport"


async def test_recognizer_normalizes_vendor_messages_without_inventing_confidence():
    received = [
        {"type": "events", "data": {"signal_type": "START_SPEECH"}},
        {"type": "events", "data": {"event_type": "END_SPEECH"}},
        {"type": "data", "data": {"transcript": "  ", "request_id": "r0"}},
        {
            "type": "data",
            "data": {
                "transcript": " नमस्ते ",
                "language_code": "hi-IN",
                "request_id": "r1",
                "metrics": {"audio_duration": 1.25},
            },
        },
        {
            "type": "data",
            "data": {
                "transcript": "hello",
                "language_probability": 0.8,
                "metrics": {"audio_duration": True},
            },
        },
        {"type": "error", "data": {"message": "bad audio"}},
    ]

    class Client:
        async def messages(self):
            for message in received:
                yield message

    recognizer = SarvamRecognizer(Client())
    heard = []
    events = []
    async for event in recognizer.events():
        events.append(event)
        heard.append(recognizer.heard_speech)
    # A flush is answered only after START_SPEECH; a final consumes it.
    assert heard == [True, True, True, False, False, False]
    assert [event.kind for event in events] == [
        "speech_start",
        "speech_end",
        "other",
        "final",
        "final",
        "error",
    ]
    final = events[3]
    assert (final.text, final.language, final.confidence) == ("नमस्ते", "hi-IN", None)
    assert (final.provider_request_id, final.audio_duration_s) == ("r1", 1.25)
    assert (events[4].confidence, events[4].language, events[4].audio_duration_s) == (
        0.8,
        None,
        None,
    )
    assert all(event.raw is message for event, message in zip(events, received, strict=True))
    assert isinstance(events[0], SttEvent) and events[0].received_at > 0


def test_endpointing_contract_is_explicit():
    class Recognizer:
        endpointing = "none"

    assert SarvamRecognizer(object()).endpointing == "provider"
    check_endpointing("local_vad", Recognizer())
    check_endpointing("sarvam", SarvamRecognizer(object()))
    with pytest.raises(ValueError, match="endpointing"):
        check_endpointing("sarvam", Recognizer())


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (InvalidStatus(Response(401, "Unauthorized", Headers())), "authentication"),
        (InvalidStatus(Response(429, "Too Many", Headers())), "rate_limit"),
        (
            httpx.HTTPStatusError(
                "x",
                request=httpx.Request("POST", "https://fixture.invalid"),
                response=httpx.Response(403),
            ),
            "authentication",
        ),
        (TimeoutError(), "timeout"),
        (httpx.ConnectError("refused"), "transport"),
        (json.JSONDecodeError("bad", "x", 0), "invalid_output"),
        (RuntimeError("unrelated"), None),
    ],
)
def test_failures_map_to_categories_through_wrappers(error, category):
    wrapped = RuntimeError("adapter wrapper")
    wrapped.__cause__ = error
    failure = classify_failure(wrapped, "sarvam")
    assert (failure.category if failure else None) == category
    if failure:
        assert failure.provider == "sarvam" and failure.__cause__ is wrapped


async def test_text_generator_emits_text_and_done_with_generation(paid_boundary):
    model = PaidFakeModel(responses=["Hello there."])
    generator = AgentTextGenerator(runner(paid_boundary, model))
    events = [event async for event in generator.stream("hello", context(paid_boundary, "g7"))]
    assert events[-1] == TextEvent(kind="done", text="Hello there.", generation_id="g7")
    assert "".join(event.text for event in events[:-1]) == "Hello there."
    assert all(event.kind == "text" and event.generation_id == "g7" for event in events[:-1])


async def test_text_metering_uses_the_configured_provider(paid_boundary):
    from app.voice.agent import AgentRunner

    model = PaidFakeModel(responses=["hello"])
    subject = AgentRunner(
        api_key="unused",
        base_url="https://fixture.invalid",
        model_name="sarvam-105b",
        thread_id="provider-thread",
        tools=[],
        chat_model=model,
        costs=paid_boundary.costs,
        scope=paid_boundary.scope,
        expires_at=paid_boundary.costs.clock.utc_now() + timedelta(seconds=120),
        provider_id="openrouter",  # no rate exists: metering must refuse before any request
    )
    with pytest.raises(RateUnavailable):
        await collect(subject)
    assert model.calls == 0


def test_context_requires_aware_deadline():
    from datetime import datetime

    with pytest.raises(ValueError):
        ProviderContext(scope=CostScope(user_id="u", job_id="j"), deadline=datetime(2026, 1, 1))
