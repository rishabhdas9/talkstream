from __future__ import annotations

import asyncio
import io
import wave
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from test_stt import metered_stt as metered_stt

from app.config import Settings
from app.costs.models import BudgetExceeded, CostScope
from app.costs.service import CostService
from app.voice.providers import groq_stt as module
from app.voice.providers.groq_stt import GroqRecognizer, GroqSttClient
from app.voice.stt import SarvamSttError


@pytest.fixture
async def groq_rig(metered_stt, monkeypatch):
    original, old_costs, _, clock = metered_stt
    rates = old_costs.rate_card
    rates.unit_prices["groq"] = {"whisper-large-v3-turbo": rates.unit_prices["sarvam"]["saaras:v3"]}
    costs = CostService(old_costs.database, clock, old_costs.policy, rates)
    state = SimpleNamespace(requests=[], reply={"text": " namaste ", "language": "hindi"})
    state.delays, state.status, state.started = [], 200, asyncio.Event()

    async def handler(request):
        state.requests.append(request)
        state.started.set()
        if state.delays:
            await asyncio.sleep(state.delays.pop(0))
        reply = state.reply if not callable(state.reply) else state.reply(len(state.requests))
        return httpx.Response(state.status, json=reply, headers={"x-request-id": "req-1"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    def build(scope=None, **kwargs):
        config = replace(original._config, model="whisper-large-v3-turbo", language_code="unknown")
        return GroqRecognizer(
            GroqSttClient(
                config,
                costs=costs,
                scope=scope or original._scope,
                expires_at=original._expires_at,
                **kwargs,
            )
        )

    async def rows():
        async with costs.database.engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT provider,state,bound_json FROM cost_attempts ORDER BY created_at")
                )
            ).all()

    return SimpleNamespace(build=build, state=state, rows=rows, scope=original._scope)


async def first_event(recognizer, kind="final"):
    async for event in recognizer.events():
        if event.kind == kind:
            return event


async def test_one_flush_uploads_one_wav_and_bills_the_minimum(groq_rig):
    r = groq_rig
    recognizer = r.build()
    await recognizer.connect()
    await recognizer.send_audio(b"\1\0" * 16_000)  # one second
    await recognizer.flush()
    event = await asyncio.wait_for(first_event(recognizer), 1)
    assert (event.text, event.language, event.confidence) == ("namaste", "hi-IN", None)
    (request,) = r.state.requests
    assert request.headers["Authorization"].startswith("Bearer ")
    body = request.read()
    assert b'name="response_format"\r\n\r\nverbose_json' in body
    assert b'name="language"' not in body  # Detection, not a claimed language.
    wav_bytes = body[body.index(b"RIFF") :]
    with wave.open(io.BytesIO(wav_bytes), "rb") as audio:
        assert (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) == (1, 16000, 2)
    await recognizer.close()
    ((provider, state, bound),) = await r.rows()
    assert (provider, state) == ("groq", "settled") and '"audio_ms":10000' in bound


async def test_long_utterance_bills_whole_seconds(groq_rig):
    assert module.billed_ms(32_000 * 12 + 2) == 13_000
    assert module.billed_ms(32_000) == 10_000


async def test_finals_keep_utterance_order_when_responses_race(groq_rig):
    r = groq_rig
    r.state.delays = [0.05, 0]
    r.state.reply = lambda count: {"text": f"utterance {count}"}
    recognizer = r.build()
    await recognizer.connect()
    for _ in range(2):
        await recognizer.send_audio(b"\1\0" * 320)
        await recognizer.flush()
    events = recognizer.events()
    texts = [(await anext(events)).text, (await anext(events)).text]
    assert texts == ["utterance 1", "utterance 2"]
    await recognizer.close()


async def test_failed_request_is_an_error_and_keeps_the_hold(groq_rig):
    r = groq_rig
    r.state.status = 500
    recognizer = r.build()
    await recognizer.connect()
    await recognizer.send_audio(b"\1\0" * 320)
    await recognizer.flush()
    with pytest.raises(SarvamSttError):
        await asyncio.wait_for(first_event(recognizer), 1)
    await recognizer.close()
    assert [row.state for row in await r.rows()] == ["unknown"]


async def test_exhausted_allowance_makes_no_request(groq_rig):
    r = groq_rig
    scope = CostScope(user_id="eval-owner", job_id="live-eval:groq")
    recognizer = r.build(scope=scope, evaluation_cap_micro_inr=1)
    await recognizer.connect()
    await recognizer.send_audio(b"\1\0" * 320)
    await recognizer.flush()
    with pytest.raises(BudgetExceeded):
        await asyncio.wait_for(first_event(recognizer), 1)
    assert r.state.requests == [] and await r.rows() == []
    await recognizer.close()


async def test_close_cancels_an_inflight_request_and_keeps_the_hold(groq_rig):
    r = groq_rig
    r.state.delays = [10]
    recognizer = r.build()
    await recognizer.connect()
    await recognizer.send_audio(b"\1\0" * 320)
    await recognizer.flush()
    await r.state.started.wait()
    await recognizer.close()
    assert [row.state for row in await r.rows()] == ["unknown"]


async def test_empty_flush_and_oversized_utterance(groq_rig):
    r = groq_rig
    recognizer = r.build()
    await recognizer.connect()
    await recognizer.flush()
    with pytest.raises(SarvamSttError, match="120-second"):
        await recognizer.send_audio(b"\0" * (module.MAX_UTTERANCE_BYTES + 2))
    await recognizer.close()
    assert r.state.requests == []


def test_groq_configuration_needs_local_endpointing():
    values = {
        "_env_file": None,
        "jwt_secret": "x" * 32,
        "stt_transport": "groq",
        "stt_model": "whisper-large-v3-turbo",
        "stt_language_code": "unknown",
    }
    assert Settings(**values).endpointing_strategy == "local_vad"
    with pytest.raises(ValidationError):
        Settings(**values, endpointing_strategy="sarvam")
