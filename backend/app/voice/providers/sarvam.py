"""Contract adapters around the corrected Sarvam baseline clients (the default profile)."""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from app.voice.providers.contracts import AudioChunk, ProviderContext, SttEvent, TextEvent

_SPEECH_SIGNALS = {"START_SPEECH": "speech_start", "END_SPEECH": "speech_end"}


async def _ignore(*args: Any) -> None:
    pass


def _number(value: Any, *, upper: float | None = None) -> float | None:
    """A finite, non-negative vendor number; anything else is unknown, never coerced."""
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or number < 0 or (upper is not None and number > upper):
        return None
    return number


def _stt_event(message: dict[str, Any], received_at: float) -> SttEvent:
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    if message.get("type") == "events":
        signal = data.get("signal_type") or data.get("event_type")
        return SttEvent(_SPEECH_SIGNALS.get(signal, "other"), received_at, raw=message)
    if message.get("type") == "error":
        return SttEvent("error", received_at, raw=message)
    transcript = data.get("transcript")
    if message.get("type") != "data" or not isinstance(transcript, str) or not transcript.strip():
        return SttEvent("other", received_at, raw=message)
    language, request_id = data.get("language_code"), data.get("request_id")
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    return SttEvent(
        "final",
        received_at,
        text=transcript.strip(),
        language=language if isinstance(language, str) else None,
        confidence=_number(data.get("language_probability"), upper=1),
        provider_request_id=request_id if isinstance(request_id, str) and request_id else None,
        audio_duration_s=_number(metrics.get("audio_duration")),
        raw=message,
    )


class SarvamRecognizer:
    """Saaras streaming recognition; emits provider speech-start/end signals."""

    endpointing = "provider"
    rotate_after_final = True

    def __init__(self, client: Any) -> None:
        self.client = client
        self._heard = False

    @property
    def heard_speech(self) -> bool:
        """Saaras answers a flush only after its own START_SPEECH since the last final."""
        return self._heard

    async def connect(self, context: ProviderContext | None = None) -> None:
        # The metered client binds its call scope and lease deadline at construction.
        await self.client.connect()

    async def reconnect(self) -> None:
        await self.client.reconnect()
        self._heard = False

    async def send_audio(self, pcm: bytes) -> dict[str, Any]:
        return await self.client.send_audio(pcm)

    async def flush(self) -> dict[str, Any]:
        return await self.client.flush()

    async def close(self) -> None:
        await self.client.close()

    async def events(self) -> AsyncIterator[SttEvent]:
        async for message in self.client.messages():
            event = _stt_event(message, time.monotonic())
            if event.kind in {"speech_start", "final"}:
                self._heard = event.kind == "speech_start"
            yield event


class SarvamSynthesizer:
    """Bulbul streaming synthesis tagged with the requesting generation."""

    def __init__(
        self,
        client: Any,
        *,
        codec: str,
        sample_rate_hz: int,
        observer=_ignore,
        probe=_ignore,
    ) -> None:
        self.client = client
        self.codec = codec
        self.sample_rate_hz = sample_rate_hz
        self._observer = observer
        self._probe = probe
        self._discarded = False

    @property
    def connection_id(self) -> str:
        return self.client.connection_id

    async def close_and_discard(self, reason: str = "interrupted") -> None:
        self._discarded = True
        await self.client.close_and_discard(reason)

    async def synthesize(
        self, text: AsyncIterator[str], context: ProviderContext
    ) -> AsyncIterator[AudioChunk]:
        stream = self.client.synthesize(text, observer=self._observer, probe=self._probe)
        async with aclosing(stream) as audio:
            async for data in audio:
                if self._discarded:
                    return  # Late audio for a discarded generation never escapes.
                yield AudioChunk(context.generation_id, data, self.codec, self.sample_rate_hz)


class AgentTextGenerator:
    """Text events from the metered agent; tools and history stay with the agent (S09)."""

    def __init__(self, runner: Any, **reply_options: Any) -> None:
        self.runner = runner
        self.reply_options = reply_options

    async def stream(self, transcript: str, context: ProviderContext) -> AsyncIterator[TextEvent]:
        parts: list[str] = []
        options = {"observer": _ignore, "tool_observer": _ignore, **self.reply_options}
        async with aclosing(
            self.runner.stream_reply(
                transcript,
                turn_id=context.turn_id,
                generation_id=context.generation_id,
                **options,
            )
        ) as replies:
            async for text in replies:
                parts.append(text)
                yield TextEvent("text", text, context.generation_id)
        yield TextEvent("done", "".join(parts), context.generation_id)
