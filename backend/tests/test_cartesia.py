from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.config import Settings
from app.costs.models import CostScope
from app.costs.rates import PriceAdjustment, RateCard, UnitPrice
from app.costs.service import CostService
from app.runtime import RuntimePolicy, SystemClock
from app.voice import tts as tts_module
from app.voice.providers.cartesia import CartesiaTtsClient
from app.voice.tts import SarvamTtsError, TtsConfig

VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"


@pytest.fixture
def cartesia_rig(database, monkeypatch):
    state = SimpleNamespace(sent=[], url=None, headers=None, replies=None, ended=asyncio.Event())

    class Connection:
        async def send(self, raw):
            state.sent.append(json.loads(raw))
            if state.sent[-1].get("continue") is False:
                state.ended.set()

        async def recv(self):
            await state.ended.wait()
            return json.dumps(state.replies.pop(0))

        async def close(self, **_kwargs):
            pass

    class Connect:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    def connect(url, *, additional_headers, **_kwargs):
        state.url, state.headers = url, additional_headers
        return Connect()

    monkeypatch.setattr(tts_module, "connect", connect)
    now = datetime.now(UTC)
    rates = RateCard(
        version="cartesia-fixture",
        captured_at=now - timedelta(minutes=1),
        valid_until=now + timedelta(days=1),
        source="https://example.com/fixture",
        currency="INR",
        fx_micro_inr=1_000_000,
        qualified=True,
        taxes_and_fees=PriceAdjustment(numerator=1, denominator=1, note="Synthetic fixture"),
        unit_prices={
            "cartesia": {"sonic-3.6": {"characters": UnitPrice(amount_micros=1, per_units=1)}}
        },
    )
    costs = CostService(database, SystemClock(), RuntimePolicy(), rates)

    def client(language="hi-IN"):
        return CartesiaTtsClient(
            TtsConfig(
                api_key="cartesia-key",
                model="sonic-3.6",
                speaker=VOICE,
                target_language_code=language,
                sample_rate_hz=24000,
                output_audio_codec="linear16",
            ),
            costs=costs,
            scope=CostScope(user_id="user", job_id="live-eval:cartesia"),
            expires_at=now + timedelta(minutes=1),
            evaluation_cap_micro_inr=10_000_000,
        )

    async def run(synthesizer, *chunks):
        async def text_chunks():
            for chunk in chunks:
                yield chunk

        async def ignore(*_args):
            return None

        return [
            audio
            async for audio in synthesizer.synthesize(text_chunks(), observer=ignore, probe=ignore)
        ]

    async def rows():
        async with database.engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT provider,state,actual_micro_inr FROM cost_attempts")
                )
            ).all()

    return SimpleNamespace(state=state, client=client, run=run, rows=rows)


def chunk(context_id, pcm=b"\1\0" * 240):
    return {"type": "chunk", "context_id": context_id, "data": base64.b64encode(pcm).decode()}


async def test_generation_streams_one_context_and_settles_characters(cartesia_rig):
    r = cartesia_rig
    synthesizer = r.client()
    context = synthesizer.connection_id
    r.state.replies = [chunk(context), {"type": "done", "context_id": context}]
    audio = await r.run(synthesizer, "नमस्ते", " दोस्त")
    assert audio == [b"\1\0" * 240]
    query = parse_qs(urlsplit(r.state.url).query)
    assert query["cartesia_version"] == ["2026-08-14"]
    assert r.state.headers == {"X-API-Key": "cartesia-key"}
    first, second, last = r.state.sent
    assert first["model_id"] == "sonic-3.6" and first["voice"] == {"id": VOICE}
    assert first["language"] == "hi" and first["context_id"] == context
    assert first["output_format"] == {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": 24000,
    }
    assert (first["transcript"], first["continue"]) == ("नमस्ते", True)
    assert (second["transcript"], last["transcript"], last["continue"]) == (" दोस्त", "", False)
    rows = await r.rows()
    assert [(row.provider, row.state) for row in rows] == [("cartesia", "settled")] * 2


async def test_audio_for_another_context_never_escapes(cartesia_rig):
    r = cartesia_rig
    synthesizer = r.client()
    r.state.replies = [chunk("someone-else"), {"type": "done", "context_id": "someone-else"}]
    with pytest.raises(SarvamTtsError, match="different generation"):
        await r.run(synthesizer, "hello")
    assert {row.state for row in await r.rows()} == {"unknown"}  # Partial work may be billed.


async def test_provider_error_fails_the_generation(cartesia_rig):
    r = cartesia_rig
    synthesizer = r.client("en-IN")
    r.state.replies = [{"type": "error", "title": "Invalid voice", "status_code": 400}]
    with pytest.raises(SarvamTtsError, match="Invalid voice"):
        await r.run(synthesizer, "hello")
    assert r.state.sent[0]["language"] == "en"


def test_cartesia_needs_a_pinned_model_and_voice_id():
    values = {"_env_file": None, "jwt_secret": "x" * 32, "tts_provider": "cartesia"}
    assert Settings(**values, tts_model="sonic-3.6", tts_speaker=VOICE).tts_provider == "cartesia"
    for override in (
        {"tts_model": "sonic-latest", "tts_speaker": VOICE},
        {"tts_model": "sonic-3.6"},
    ):
        with pytest.raises(ValidationError, match="voice ID"):
            Settings(**values, **override)
