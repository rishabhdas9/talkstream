from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from test_paid_boundaries import paid_boundary as paid_boundary
from test_session_policy import session_policy as session_policy
from test_stt import metered_stt as metered_stt

from app.config import Settings
from app.costs.service import CostService
from app.voice.providers import deepgram as module
from app.voice.providers.deepgram import DeepgramRecognizer, DeepgramSttClient


def result(transcript, start, duration=0.5, *, final=True, speech_final=False, languages=None):
    alternative = {"transcript": transcript, "confidence": 0.98}
    if languages is not None:
        alternative["languages"] = languages
    return {
        "type": "Results",
        "start": start,
        "duration": duration,
        "is_final": final,
        "speech_final": speech_final,
        "channel": {"alternatives": [alternative]},
        "metadata": {"request_id": "dg-1"},
    }


@pytest.fixture
async def deepgram_rig(metered_stt, monkeypatch):
    original, old_costs, _, clock = metered_stt
    rates = old_costs.rate_card
    rates.unit_prices["deepgram"] = {"nova-3": rates.unit_prices["sarvam"]["saaras:v3"]}
    costs = CostService(old_costs.database, clock, old_costs.policy, rates)
    config = replace(original._config, model="nova-3", language_code="multi")
    client = DeepgramSttClient(
        config, costs=costs, scope=original._scope, expires_at=original._expires_at
    )
    state = SimpleNamespace(url=None, headers=None, sent=[], summary=1.0, hang_close=False)
    state.close_sent = asyncio.Event()
    incoming = asyncio.Queue()

    class Connection:
        close_code = 1000

        async def send(self, frame):
            state.sent.append(frame)
            if frame == '{"type":"CloseStream"}':
                state.close_sent.set()
                if not state.hang_close:
                    await incoming.put(
                        {"type": "Metadata", "request_id": "dg-1", "duration": state.summary}
                    )

        async def recv(self):
            return json.dumps(await incoming.get())

        async def __aiter__(self):
            while True:
                yield json.dumps(await incoming.get())

        async def close(self):
            pass

    async def connect(url, *, additional_headers, **kwargs):
        if state.fail_connect:
            raise state.fail_connect
        state.url, state.headers = url, additional_headers
        return Connection()

    state.fail_connect = None
    monkeypatch.setattr(module, "connect", connect)
    recognizer = DeepgramRecognizer(client)

    async def events_after(*messages):
        for message in messages:
            await incoming.put(message)
        await incoming.put({"type": "Marker"})
        seen = []
        async for event in recognizer.events():
            if event.raw == {"type": "Marker"}:
                return seen
            if event.kind != "other":
                seen.append(event)
        return seen

    async def cost_rows():
        async with costs.database.engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT provider,state,actual_micro_inr FROM cost_attempts")
                )
            ).all()

    return SimpleNamespace(
        client=client,
        recognizer=recognizer,
        state=state,
        events_after=events_after,
        cost_rows=cost_rows,
        incoming=incoming,
    )


async def test_duplicate_final_commits_once(deepgram_rig):
    r = deepgram_rig
    await r.recognizer.connect()
    events = await r.events_after(
        result("hello", 0.0),
        result("hello", 0.0),  # Redelivered segment.
        result("there", 0.5, speech_final=True),
        {"type": "UtteranceEnd", "last_word_end": 1.0},
    )
    finals = [event for event in events if event.kind == "final"]
    assert [event.text for event in finals] == ["hello there"]
    assert [event.kind for event in events] == ["speech_start", "speech_end", "final"]
    assert {event.utterance_id for event in events} == {0}


async def test_absent_confidence_is_not_zero(deepgram_rig):
    r = deepgram_rig
    await r.recognizer.connect()
    (final,) = [
        event
        for event in await r.events_after(result("namaste", 0.0, speech_final=True))
        if event.kind == "final"
    ]
    assert final.confidence is None


@pytest.mark.parametrize(("languages", "expected"), [(["hi", "en"], "hi-IN"), (None, None)])
async def test_only_an_identified_language_is_reported(deepgram_rig, languages, expected):
    r = deepgram_rig
    await r.recognizer.connect()
    events = await r.events_after(result("kal milte", 0.0, speech_final=True, languages=languages))
    assert [event.language for event in events if event.kind == "final"] == [expected]


async def test_interim_text_and_noise_never_become_turns(deepgram_rig):
    r = deepgram_rig
    await r.recognizer.connect()
    events = await r.events_after(
        {"type": "SpeechStarted", "timestamp": 0.0},
        {"type": "UtteranceEnd", "last_word_end": 0.0},
        result("delete my", 0.0, final=False),
    )
    assert [event.kind for event in events] == ["speech_start", "partial"]
    # Replay and session track pending utterances by identity; partials must carry it.
    assert {event.utterance_id for event in events} == {0}


async def test_endpoint_without_a_new_segment_finalizes_pending_words(deepgram_rig):
    r = deepgram_rig
    await r.recognizer.connect()
    events = await r.events_after(
        result("remind me", 0.0), {"type": "UtteranceEnd", "last_word_end": 0.5}
    )
    assert [event.text for event in events if event.kind == "final"] == ["remind me"]


async def test_reconnect_resets_utterance_identity_and_time(deepgram_rig):
    r = deepgram_rig
    await r.recognizer.connect()
    await r.events_after(result("one", 3.0, speech_final=True))
    await r.recognizer.reconnect()
    events = await r.events_after(result("two", 0.0, speech_final=True))
    assert [(event.text, event.utterance_id) for event in events if event.kind == "final"] == [
        ("two", 0)
    ]


async def test_stream_sends_binary_audio_and_finalize_with_explicit_options(deepgram_rig):
    r = deepgram_rig
    await r.recognizer.connect()
    await r.recognizer.send_audio(b"\0" * 640)
    await r.recognizer.flush()
    query = parse_qs(urlsplit(r.state.url).query)
    expected = {
        "model": "nova-3",
        "language": "multi",
        "encoding": "linear16",
        "sample_rate": "16000",
        "endpointing": "100",
        "interim_results": "true",
    }
    assert expected.items() <= {key: values[0] for key, values in query.items()}.items()
    assert r.state.headers["Authorization"].startswith("Token ")
    assert r.state.sent == [b"\0" * 640, '{"type":"Finalize"}']


async def test_closing_summary_settles_reported_audio(deepgram_rig):
    r = deepgram_rig
    await r.recognizer.connect()
    await r.recognizer.send_audio(b"\0" * 32_000)
    await r.recognizer.close()
    assert r.state.sent[-1] == '{"type":"CloseStream"}'
    assert await r.cost_rows() == [("deepgram", "settled", 8_334)]


@pytest.mark.parametrize("summary", [0.5, None])
async def test_short_or_missing_summary_charges_only_audio_sent(deepgram_rig, summary):
    r = deepgram_rig
    r.state.summary = summary
    await r.recognizer.connect()
    await r.recognizer.send_audio(b"\0" * 32_000)
    await r.recognizer.close()
    assert await r.cost_rows() == [("deepgram", "unknown", 16_667)]


async def test_cancelled_summary_drain_charges_only_audio_sent(deepgram_rig):
    r = deepgram_rig
    r.state.hang_close = True
    await r.recognizer.connect()
    await r.recognizer.send_audio(b"\0" * 320)
    closing = asyncio.create_task(r.recognizer.close())
    await r.state.close_sent.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert await r.cost_rows() == [("deepgram", "unknown", 16_667)]


async def test_rejected_connection_charges_the_minimum_and_opens_nothing(deepgram_rig):
    r = deepgram_rig
    r.state.fail_connect = OSError("HTTP 401")
    with pytest.raises(OSError):
        await r.recognizer.connect()
    assert r.state.url is None
    assert await r.cost_rows() == [("deepgram", "unknown", 8_334)]


def test_deepgram_configuration_is_explicit():
    values = {
        "_env_file": None,
        "jwt_secret": "x" * 32,
        "stt_transport": "deepgram",
        "stt_model": "nova-3",
        "stt_language_code": "multi",
        "endpointing_strategy": "sarvam",
    }
    assert Settings(**values).stt_transport == "deepgram"
    for override in ({"stt_model": "saaras:v3"}, {"endpointing_strategy": "local_vad"}):
        with pytest.raises(ValidationError):
            Settings(**(values | override))


async def test_interim_text_cannot_reach_the_agent(deepgram_rig, session_policy):
    r = deepgram_rig
    await r.recognizer.connect()
    voice = session_policy([]).voice
    voice._stt, voice._endpointing_strategy = r.recognizer, "sarvam"
    receiver = asyncio.create_task(voice._receive_stt_final())
    await r.incoming.put(result("delete my task", 0.0, final=False))
    await asyncio.sleep(0.05)
    assert voice._response_queue.empty() and not receiver.done()
    await r.incoming.put(result("please keep", 0.0))
    await r.incoming.put(result("my task", 0.5, speech_final=True))
    await asyncio.wait_for(receiver, 1)
    _, transcript = voice._response_queue.get_nowait()
    assert transcript == "please keep my task"
