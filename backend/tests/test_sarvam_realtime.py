from __future__ import annotations

import asyncio
import base64
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
from app.voice.protocol import CaptureFrame, encode_capture_frame
from app.voice.providers.contracts import ProviderFailure
from app.voice.providers.sarvam_realtime import SarvamRealtimeClient, SarvamRealtimeRecognizer


@pytest.fixture
async def realtime_rig(metered_stt, monkeypatch):
    original, old_costs, _, clock = metered_stt
    rates = old_costs.rate_card
    rates.unit_prices["sarvam"]["saaras:v3-realtime"] = rates.unit_prices["sarvam"]["saaras:v3"]
    costs = CostService(old_costs.database, clock, old_costs.policy, rates)
    config = replace(original._config, model="saaras:v3-realtime", language_code="auto")
    client = SarvamRealtimeClient(
        config, costs=costs, scope=original._scope, expires_at=original._expires_at
    )
    state = SimpleNamespace(
        url=None,
        headers=None,
        hang_end=False,
        summary=1.25,
        opened=0,
        end_sent=asyncio.Event(),
        hang_begin=False,
        summary_id="r1",
        echoed_language="auto",
        waiting=asyncio.Event(),
    )
    incoming, consumed = asyncio.Queue(), asyncio.Queue()
    sent = []

    class Connection:
        close_code = 1000
        closed = False

        async def send(self, raw):
            message = json.loads(raw)
            sent.append(message)
            if message == {"event": "end"}:
                state.end_sent.set()
            if message == {"event": "end"} and not state.hang_end:
                await incoming.put(
                    {
                        "event": "session.end",
                        "request_id": state.summary_id,
                        "total_duration_s": 2.0,
                        "total_utterances": 1,
                        "audio_duration_s": state.summary,
                    }
                )

        async def recv(self):
            state.waiting.set()
            message = await incoming.get()
            await consumed.put(message)
            if isinstance(message, Exception):
                raise message
            return json.dumps(message)

        async def __aiter__(self):
            while not self.closed:
                yield await self.recv()

        async def close(self):
            self.closed = True

    connection = Connection()

    async def connect(url, **kwargs):
        async with costs.database.engine.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM cost_attempts WHERE state='reserved' "
                        "AND model='saaras:v3-realtime'"
                    )
                )
            ).scalar_one() > 0
        state.opened += 1
        state.url, state.headers = url, kwargs["additional_headers"]
        if not state.hang_begin:
            await incoming.put(
                {
                    "event": "session.begin",
                    "request_id": "r1",
                    "config": {
                        "model": "saaras:v3-realtime",
                        "language_code": state.echoed_language,
                        "encoding": "linear16",
                        "sample_rate": 16000,
                        "turn_detection": "vad",
                        "stream_type": "fast",
                    },
                }
            )
        return connection

    monkeypatch.setattr("app.voice.providers.sarvam_realtime.connect", connect)
    recognizer = SarvamRealtimeRecognizer(client)
    rig = SimpleNamespace(
        client=client,
        recognizer=recognizer,
        costs=costs,
        clock=clock,
        state=state,
        incoming=incoming,
        consumed=consumed,
        sent=sent,
        connection=connection,
    )
    yield rig
    await recognizer.close()


async def test_provider_endpointing_receives_required_audio(realtime_rig, session_policy):
    r = realtime_rig
    await r.recognizer.connect()
    voice = session_policy([]).voice
    voice._stt, voice._endpointing_strategy = r.recognizer, "sarvam"
    voice._gate = None
    voice._stt_ready.set()
    for seq, pcm in enumerate([b"\x01\x00" * 512, b"\x00\x00" * 512]):
        await voice._handle_audio_frame(encode_capture_frame(CaptureFrame(seq, seq * 32, pcm)))
    assert [base64.b64decode(m["audio"]) for m in r.sent] == [b"\x01\x00" * 512, b"\x00\x00" * 512]
    query = parse_qs(urlsplit(r.state.url).query)
    assert urlsplit(r.state.url).path == "/speech-to-text-realtime/ws"
    assert query["endpointing"] == ["vad"] and query["encoding"] == ["linear16"]
    assert query["language_code"] == ["auto"] and "language-code" not in query


async def test_interim_text_cannot_execute_tools(realtime_rig, session_policy):
    r = realtime_rig
    await r.recognizer.connect()
    voice = session_policy([]).voice
    voice._stt = r.recognizer
    receiver = asyncio.create_task(voice._receive_stt_final())
    await r.incoming.put(
        {
            "event": "transcript.partial",
            "utterance_idx": 0,
            "text": "delete my task",
            "language": "en-IN",
        }
    )
    while (await r.consumed.get()).get("event") != "transcript.partial":
        pass
    await asyncio.sleep(0)
    assert voice._response_queue.empty()
    assert not receiver.done()
    await r.incoming.put(
        {"event": "transcript.final", "utterance_idx": 0, "text": "please keep my task"}
    )
    await asyncio.wait_for(receiver, 1)
    _, transcript = voice._response_queue.get_nowait()
    assert transcript == "please keep my task"


async def test_early_duplicate_empty_and_out_of_order_finals(realtime_rig):
    r = realtime_rig
    await r.recognizer.connect()
    stream = r.recognizer.events()
    await r.incoming.put(
        {
            "event": "transcript.final",
            "utterance_idx": 0,
            "text": "Kal 5 pm meeting है",
            "language": "hi-IN",
        }
    )
    first = await anext(stream)
    assert (first.kind, first.text, first.language, first.confidence) == (
        "final",
        "Kal 5 pm meeting है",
        "hi-IN",
        None,
    )
    for message in [
        {"event": "transcript.final", "utterance_idx": 0, "text": "duplicate"},
        {"event": "transcript.final", "utterance_idx": 1, "text": "   "},
        {"event": "transcript.partial", "utterance_idx": 3, "text": "new"},
        {"event": "transcript.final", "utterance_idx": 3, "text": "new final"},
        {"event": "transcript.final", "utterance_idx": 2, "text": "stale"},
    ]:
        await r.incoming.put(message)
    events = [await anext(stream) for _ in range(5)]
    assert [e.kind for e in events] == ["other", "other", "partial", "final", "other"]
    assert events[-2].language is None and events[-2].confidence is None
    await stream.aclose()


@pytest.mark.parametrize("duration", [1.25, "1.25"])
async def test_close_reconciles_only_session_total(realtime_rig, duration):
    r = realtime_rig
    r.state.summary = duration
    await r.recognizer.connect()
    await r.recognizer.send_audio(b"\x00\x00" * 16000)
    await r.recognizer.close()
    async with r.costs.database.engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM cost_attempts"))).mappings().one()
    assert row["state"] == "settled"
    assert json.loads(row["usage_json"])["units"] == {"audio_ms": 2000}
    assert r.sent[-1] == {"event": "end"} and r.connection.closed


@pytest.mark.parametrize("duration", [None, True, -1, "NaN", "bad", 0.01])
async def test_invalid_or_underreported_total_charges_only_audio_sent(realtime_rig, duration):
    r = realtime_rig
    r.state.summary = duration
    await r.recognizer.connect()
    await r.recognizer.send_audio(b"\x00\x00" * 16000)
    await r.recognizer.close()
    async with r.costs.database.engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM cost_attempts"))).mappings().one()
    assert row["state"] == "unknown"
    assert json.loads(row["usage_json"])["units"] == {"audio_ms": 2000}


async def test_unaffordable_stream_never_opens_transport(realtime_rig):
    r = realtime_rig
    r.costs._rates = None
    from app.costs.rates import RateUnavailable

    with pytest.raises(RateUnavailable):
        await r.recognizer.connect()
    assert r.state.opened == 0


async def test_disconnect_and_expired_stream_remain_bounded(realtime_rig):
    r = realtime_rig
    await r.recognizer.connect()
    await r.incoming.put(ConnectionError("private provider detail"))
    with pytest.raises(ConnectionError):
        await anext(r.recognizer.events())
    from datetime import timedelta

    r.clock.now += timedelta(seconds=121)
    with pytest.raises(RuntimeError, match="deadline"):
        await r.recognizer.send_audio(b"\x00\x00")


@pytest.mark.parametrize(
    "overrides",
    [
        {"stt_model": "saaras:v3"},
        {"endpointing_strategy": "local_vad"},
        {"stt_language_code": "unknown"},
    ],
)
def test_realtime_configuration_rejects_incompatible_contract(overrides):
    values = dict(
        jwt_secret="test-secret-that-is-at-least-thirty-two-characters",
        stt_transport="sarvam_realtime",
        stt_model="saaras:v3-realtime",
        stt_language_code="auto",
        endpointing_strategy="sarvam",
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **(values | overrides))


async def test_invalid_utterance_identity_is_not_a_turn(realtime_rig):
    r = realtime_rig
    await r.recognizer.connect()
    await r.incoming.put({"event": "transcript.final", "utterance_idx": True, "text": "delete"})
    with pytest.raises(ProviderFailure) as failed:
        await anext(r.recognizer.events())
    assert failed.value.category == "invalid_output"


async def test_initialization_waits_for_session_begin_and_times_out(realtime_rig):
    r = realtime_rig
    r.state.hang_begin = True
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(r.recognizer.connect(), 0.5)
    assert r.connection.closed
    async with r.costs.database.engine.connect() as conn:
        row = (await conn.execute(text("SELECT state FROM cost_attempts"))).one()
    assert row.state == "unknown"


async def test_cancelled_summary_drain_closes_and_keeps_spend(realtime_rig):
    r = realtime_rig
    r.state.hang_end = True
    await r.recognizer.connect()
    close = asyncio.create_task(r.recognizer.close())
    await r.state.end_sent.wait()
    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert r.connection.closed
    async with r.costs.database.engine.connect() as conn:
        row = (await conn.execute(text("SELECT state FROM cost_attempts"))).one()
    assert row.state == "unknown"


async def test_wrong_request_summary_does_not_release_reserved_spend(realtime_rig):
    r = realtime_rig
    r.state.summary_id = "different-socket"
    await r.recognizer.connect()
    await r.recognizer.close()
    async with r.costs.database.engine.connect() as conn:
        row = (await conn.execute(text("SELECT state FROM cost_attempts"))).one()
    assert row.state == "unknown"


async def test_capabilities_distinguish_configured_language_from_detection(settings):
    import httpx

    from app.main import create_app

    app = create_app(
        settings.model_copy(
            update={
                "stt_transport": "sarvam_realtime",
                "stt_model": "saaras:v3-realtime",
                "stt_language_code": "hi-IN",
                "endpointing_strategy": "sarvam",
            }
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        speech = (await c.get("/capabilities")).json()["speech"]
    assert speech["configured_language"] == "hi-IN"
    assert speech["language_detection"] is False
    assert speech["experimental"] is True


@pytest.mark.parametrize(
    "value,expected",
    [(0.8, 0.8), ("0.8", 0.8), (True, None), ("NaN", None), (2, None), (None, None)],
)
async def test_language_confidence_uses_documented_optional_number(realtime_rig, value, expected):
    r = realtime_rig
    await r.recognizer.connect()
    await r.incoming.put(
        {
            "event": "transcript.final",
            "utterance_idx": 0,
            "text": "hello",
            "language_confidence": value,
        }
    )
    stream = r.recognizer.events()
    assert (await anext(stream)).confidence == expected
    await stream.aclose()


async def test_late_speech_start_cannot_reopen_an_ended_utterance(realtime_rig):
    r = realtime_rig
    await r.recognizer.connect()
    for kind in ["vad.speech_end", "vad.speech_start", "vad.speech_end"]:
        await r.incoming.put({"event": kind, "utterance_idx": 0})
    stream = r.recognizer.events()
    assert [(await anext(stream)).kind for _ in range(3)] == ["speech_end", "other", "other"]
    await stream.aclose()


@pytest.mark.parametrize("missing_first_signals", [False, True])
async def test_overlapping_utterances_keep_their_finals_and_timers(
    realtime_rig, session_policy, missing_first_signals
):
    r = realtime_rig
    await r.recognizer.connect()
    voice = session_policy([]).voice
    voice._stt, voice._endpointing_strategy = r.recognizer, "sarvam"
    voice._stt_ready.set()
    loop = asyncio.create_task(voice._stt_loop())
    messages = [
        {"event": "vad.speech_start", "utterance_idx": 0},
        {"event": "vad.speech_end", "utterance_idx": 0},
        {"event": "vad.speech_start", "utterance_idx": 1},
        {"event": "vad.speech_end", "utterance_idx": 1},
        {"event": "transcript.final", "utterance_idx": 0, "text": "set a reminder"},
        {"event": "transcript.partial", "utterance_idx": 1, "text": "tomorrow"},
        {"event": "transcript.final", "utterance_idx": 1, "text": "tomorrow at five"},
    ]
    if missing_first_signals:
        messages = messages[2:]
    try:
        for message in messages:
            await r.incoming.put(message)
        first = await asyncio.wait_for(voice._response_queue.get(), 0.5)
        second = await asyncio.wait_for(voice._response_queue.get(), 0.5)
        assert [first[1], second[1]] == ["set a reminder", "tomorrow at five"]
        assert first[0].turn_index < second[0].turn_index
        assert first[0].turn_id != second[0].turn_id
        if missing_first_signals:
            assert first[0].dimensions["speech_anchor_source"] == "final_receive_fallback"
        else:
            assert (
                first[0].server_timestamps["t_endpoint_decision"]
                <= second[0].server_timestamps["t_speech_start_server"]
            )
        assert r.state.opened == 1 and not r.state.end_sent.is_set()
        assert voice._stt_ready.is_set()
    finally:
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop


async def test_readiness_rejects_a_different_echoed_language(realtime_rig):
    r = realtime_rig
    r.state.echoed_language = "hi-IN"
    with pytest.raises(ProviderFailure) as failed:
        await r.recognizer.connect()
    assert failed.value.category == "invalid_output"
    assert r.connection.closed


async def test_cancelled_receiver_can_reconcile_a_later_authoritative_total(realtime_rig):
    r = realtime_rig
    await r.recognizer.connect()
    await r.recognizer.send_audio(b"\x00\x00" * 16000)
    r.state.waiting.clear()
    receiver = asyncio.create_task(anext(r.recognizer.events()))
    await r.state.waiting.wait()
    receiver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await receiver
    await r.recognizer.close()
    async with r.costs.database.engine.connect() as conn:
        row = (await conn.execute(text("SELECT state, usage_json FROM cost_attempts"))).one()
    assert row.state == "settled"
    assert json.loads(row.usage_json)["units"] == {"audio_ms": 2000}


async def test_conflicting_billing_summaries_are_not_reconciled(realtime_rig):
    r = realtime_rig
    await r.recognizer.connect()
    for request_id in ["other-socket", "r1"]:
        await r.incoming.put(
            {"event": "session.end", "request_id": request_id, "audio_duration_s": 1.25}
        )
    stream = r.recognizer.events()
    await anext(stream)
    await anext(stream)
    await stream.aclose()
    await r.recognizer.close()
    async with r.costs.database.engine.connect() as conn:
        row = (await conn.execute(text("SELECT state FROM cost_attempts"))).one()
    assert row.state == "unknown"


async def test_known_pending_final_survives_newer_final_and_duplicates_stay_ignored(realtime_rig):
    r = realtime_rig
    await r.recognizer.connect()
    messages = [
        {"event": "vad.speech_start", "utterance_idx": 0},
        {"event": "vad.speech_end", "utterance_idx": 0},
        {"event": "vad.speech_start", "utterance_idx": 1},
        {"event": "vad.speech_end", "utterance_idx": 1},
        {"event": "transcript.final", "utterance_idx": 1, "text": "second"},
        {"event": "transcript.final", "utterance_idx": 0, "text": "first"},
        {"event": "transcript.final", "utterance_idx": 1, "text": "duplicate"},
        {"event": "transcript.partial", "utterance_idx": 0, "text": "stale"},
        {"event": "transcript.final", "utterance_idx": 0, "text": "duplicate"},
    ]
    for message in messages:
        await r.incoming.put(message)
    stream = r.recognizer.events()
    try:
        events = [await anext(stream) for _ in messages]
        assert [event.text for event in events if event.kind == "final"] == ["second", "first"]
        assert [event.kind for event in events[-3:]] == ["other"] * 3
    finally:
        await stream.aclose()


async def test_realtime_normalizer_bounds_known_pending_utterances(realtime_rig):
    r = realtime_rig
    await r.recognizer.connect()
    for index in range(17):
        await r.incoming.put({"event": "vad.speech_start", "utterance_idx": index})
    stream = r.recognizer.events()
    try:
        for _ in range(16):
            assert (await anext(stream)).kind == "speech_start"
        with pytest.raises(ProviderFailure, match="Too many pending"):
            await anext(stream)
    finally:
        await stream.aclose()
