from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest
from test_paid_boundaries import paid_boundary as paid_boundary

from app.voice import tts as tts_module


@pytest.mark.parametrize(
    ("language_code", "speaker"),
    [
        ("en-IN", "roopa_en_conversational"),
        ("hi-IN", "roopa_hi_conversational"),
    ],
)
def test_sarvam_roopa_wire_profile(paid_boundary, language_code, speaker) -> None:
    from app.config import Settings

    settings = Settings(_env_file=None, jwt_secret="x" * 32)
    client = paid_boundary.client
    client._config = tts_module.TtsConfig(
        api_key="unused",
        model=settings.tts_model,
        speaker=settings.tts_speaker_for_language(language_code),
        target_language_code=language_code,
        sample_rate_hz=settings.tts_sample_rate_hz,
        output_audio_codec="linear16",
        pace=settings.tts_pace,
    )
    url, _headers = client._endpoint()
    assert "model=bulbul%3Av4-flash" in url
    config = client._opening_messages()[0]["data"]
    assert (config["speaker"], config["target_language_code"]) == (speaker, language_code)
    assert config["speech_sample_rate"] == 22050
    assert config["pace"] == 1.05


class FakeTtsConnection:
    def __init__(self) -> None:
        self.receive_started = asyncio.Event()
        self.receive_cancelled = asyncio.Event()
        self.receive_tasks: list[asyncio.Task[object]] = []

    async def send(self, _message: str) -> None:
        return

    async def recv(self) -> object:
        task = asyncio.current_task()
        assert task is not None
        self.receive_tasks.append(task)
        self.receive_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.receive_cancelled.set()
            raise

    async def close(self, **_kwargs) -> None:
        return


class FakeConnectContext:
    def __init__(self, connection: FakeTtsConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeTtsConnection:
        return self.connection

    async def __aexit__(self, *_args) -> None:
        return None


@pytest.mark.asyncio
async def test_cancelling_synthesis_awaits_audio_receiver(monkeypatch, paid_boundary) -> None:
    connection = FakeTtsConnection()
    monkeypatch.setattr(
        tts_module,
        "connect",
        lambda *_args, **_kwargs: FakeConnectContext(connection),
    )
    client = paid_boundary.client

    async def blocked_chunks():
        yield "hello"
        await asyncio.Future()
        yield "unreachable"

    async def observe(_name: str, _timestamp: float, _details: dict) -> None:
        return

    async def probe(_direction: str, _message: dict) -> None:
        return

    async def consume() -> None:
        async for _audio in client.synthesize(
            blocked_chunks(),
            observer=observe,
            probe=probe,
        ):
            pass

    consumer = asyncio.create_task(consume())
    await connection.receive_started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    try:
        assert connection.receive_cancelled.is_set()
        assert all(task.done() for task in connection.receive_tasks)
    finally:
        for task in connection.receive_tasks:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
