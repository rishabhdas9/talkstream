from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import anyio
from websockets.asyncio.client import ClientConnection, connect

from app.costs.models import CostScope, Usage, WorkBound
from app.costs.service import CostService
from app.models import new_id

TtsObservationCallback = Callable[[str, float, dict[str, Any]], Awaitable[None]]
TtsProbeCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class SarvamTtsError(RuntimeError):
    pass


@dataclass(frozen=True)
class TtsConfig:
    api_key: str
    model: str
    speaker: str
    target_language_code: str
    sample_rate_hz: int
    output_audio_codec: str
    pace: float = 1.0


class SarvamTtsClient:
    # Subclasses change only the wire protocol; reservation, deadlines and discard stay here.
    provider = "sarvam"

    def __init__(
        self,
        config: TtsConfig,
        *,
        costs: CostService,
        scope: CostScope,
        expires_at: datetime,
        evaluation_cap_micro_inr: int | None = None,
    ) -> None:
        self._config = config
        self._costs = costs
        self._scope = scope
        self._evaluation_cap_micro_inr = evaluation_cap_micro_inr
        self._expires_at = expires_at
        self._monotonic_deadline = costs.clock.monotonic() + max(
            0, (expires_at - costs.clock.utc_now()).total_seconds()
        )
        self._connection: ClientConnection | None = None
        self._discarded = False
        self._connection_id = str(uuid.uuid4())
        self._started = False
        self._completed = False

    @property
    def connection_id(self) -> str:
        return self._connection_id

    async def close_and_discard(self, reason: str = "interrupted") -> None:
        self._discarded = True
        connection = self._connection
        if connection is not None:
            async with asyncio.timeout(1):
                await connection.close(code=1000, reason=reason[:123])

    async def synthesize(
        self,
        text_chunks: AsyncIterator[str],
        *,
        observer: TtsObservationCallback,
        probe: TtsProbeCallback,
    ) -> AsyncIterator[bytes]:
        if self._started:
            raise SarvamTtsError("Synthesis client already used")
        self._started = True
        attempts: list[tuple[str, int]] = []
        outcome = "unknown"
        stream = None

        async def reserved_chunks():
            characters = 0
            async for text in text_chunks:
                if self._discarded:
                    return
                if not isinstance(text, str):
                    raise SarvamTtsError("Synthesis text must be a string")
                if not text:
                    continue
                characters += len(text)
                if characters > 1000:
                    raise SarvamTtsError("Synthesis exceeds 1,000 spoken characters")
                # UTF-8 bytes conservatively bound billed characters across Indic text.
                bound = len(text.encode("utf-8"))
                attempt_id = new_id()
                await self._costs.reserve(
                    self._scope,
                    attempt_id,
                    self.provider,
                    self._config.model,
                    WorkBound(units={"characters": bound}),
                    evaluation_cap_micro_inr=self._evaluation_cap_micro_inr,
                )
                if self._remaining() <= 0 or self._discarded:
                    with anyio.CancelScope(shield=True):
                        await self._costs.settle(
                            attempt_id, Usage(units={"characters": 0}), "cancelled"
                        )
                    raise SarvamTtsError("Synthesis deadline expired or output discarded")
                attempts.append((attempt_id, bound))
                yield text

        try:
            remaining = self._remaining()
            if remaining <= 0 or self._discarded:
                raise SarvamTtsError("Synthesis deadline expired or output discarded")
            async with asyncio.timeout(remaining):
                chunks = reserved_chunks()
                first = await anext(chunks, None)
                if first is None:
                    return

                async def submitted_chunks():
                    yield first
                    async for chunk in chunks:
                        yield chunk

                stream = self._stream(submitted_chunks(), observer=observer, probe=probe)
                async for audio in stream:
                    yield audio
            outcome = "succeeded" if self._completed else "unknown"
        except (asyncio.CancelledError, GeneratorExit):
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            if self._discarded:
                outcome = "cancelled"
            with anyio.CancelScope(shield=True):
                try:
                    if stream is not None:
                        await stream.aclose()
                finally:
                    for attempt_id, bound in attempts:
                        # Completed input bounds are local estimates, not invoice evidence.
                        usage = Usage(units={"characters": bound}) if self._completed else None
                        await self._costs.settle(attempt_id, usage, outcome)

    def _remaining(self) -> float:
        return min(
            (self._expires_at - self._costs.clock.utc_now()).total_seconds(),
            self._monotonic_deadline - self._costs.clock.monotonic(),
        )

    def _endpoint(self) -> tuple[str, dict[str, str]]:
        query = urlencode({"model": self._config.model, "send_completion_event": "true"})
        return (
            f"wss://api.sarvam.ai/text-to-speech/ws?{query}",
            {"Api-Subscription-Key": self._config.api_key},
        )

    def _opening_messages(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "config",
                "data": {
                    "speaker": self._config.speaker,
                    "target_language_code": self._config.target_language_code,
                    "speech_sample_rate": self._config.sample_rate_hz,
                    "output_audio_codec": self._config.output_audio_codec,
                    "pace": self._config.pace,
                    "temperature": 0.6,
                    "min_buffer_size": 50,
                    "max_chunk_length": 200,
                },
            }
        ]

    def _text_message(self, text: str) -> dict[str, Any]:
        return {"type": "text", "data": {"text": text}}

    def _closing_messages(self) -> list[dict[str, Any]]:
        return [{"type": "flush"}]

    def _parse_message(self, message: dict[str, Any]) -> tuple[str, Any, dict[str, Any]]:
        """(audio|final|error|other, base64 audio, details) for one provider message."""
        data = message.get("data")
        if message.get("type") == "audio" and isinstance(data, dict):
            details = {
                "content_type": data.get("content_type"),
                "request_id": data.get("request_id"),
            }
            return "audio", data.get("audio"), details
        if message.get("type") == "event" and isinstance(data, dict):
            return ("final" if data.get("event_type") == "final" else "other"), None, {}
        if message.get("type") == "error":
            return "error", None, data if isinstance(data, dict) else {}
        return "other", None, {}

    async def _stream(
        self,
        text_chunks: AsyncIterator[str],
        *,
        observer: TtsObservationCallback,
        probe: TtsProbeCallback,
    ) -> AsyncIterator[bytes]:
        url, headers = self._endpoint()
        connection_details = {"state": "cold", "connection_id": self._connection_id}
        await observer("t_tts_connection_acquire_start", time.monotonic(), connection_details)
        try:
            async with connect(
                url,
                additional_headers=headers,
                max_size=16 * 1024 * 1024,
                open_timeout=15,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=1,
            ) as connection:
                self._connection = connection
                await observer(
                    "t_tts_connection_acquire_end",
                    time.monotonic(),
                    connection_details,
                )
                for opening in self._opening_messages():
                    await connection.send(json.dumps(opening, separators=(",", ":")))
                    await probe("sent", opening)

                async def send_text() -> None:
                    submitted = False
                    async for text in text_chunks:
                        if self._discarded:
                            return
                        if self._remaining() <= 0:
                            raise SarvamTtsError("Synthesis deadline expired")
                        message = self._text_message(text)
                        await connection.send(json.dumps(message, separators=(",", ":")))
                        await probe("sent", message)
                        if not submitted:
                            submitted = True
                            await observer("t_tts_text_submitted", time.monotonic(), {})
                    if self._discarded:
                        return
                    for closing in self._closing_messages():
                        await connection.send(json.dumps(closing, separators=(",", ":")))
                        await probe("sent", closing)

                sender = asyncio.create_task(send_text(), name="bulbul-text-sender")
                receive_task: asyncio.Task[str | bytes] | None = None
                first_audio = True
                final_received = False
                try:
                    while not self._discarded:
                        if sender.done():
                            sender.result()
                            raw_message = await connection.recv()
                        else:
                            receive_task = asyncio.create_task(
                                connection.recv(), name="bulbul-audio-receiver"
                            )
                            done, _ = await asyncio.wait(
                                {sender, receive_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if sender in done and sender.exception() is not None:
                                receive_task.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await receive_task
                                sender.result()
                            raw_message = await receive_task
                            receive_task = None
                        if not isinstance(raw_message, str):
                            raise SarvamTtsError("Synthesis returned a non-JSON message")
                        message = json.loads(raw_message)
                        if not isinstance(message, dict):
                            raise SarvamTtsError("Synthesis returned a non-object JSON message")
                        await probe("received", message)
                        kind, encoded_audio, details = self._parse_message(message)
                        if kind == "audio":
                            if not isinstance(encoded_audio, str):
                                raise SarvamTtsError("Synthesis audio message omitted audio data")
                            if first_audio:
                                first_audio = False
                                await observer(
                                    "t_tts_first_chunk",
                                    time.monotonic(),
                                    {**details, "connection_id": self._connection_id},
                                )
                            if not self._discarded:
                                yield base64.b64decode(encoded_audio)
                        elif kind == "final":
                            final_received = True
                            await observer("t_tts_complete", time.monotonic(), {})
                            break
                        elif kind == "error":
                            raise SarvamTtsError(f"Synthesis provider error: {details}")
                    await sender
                    self._completed = final_received and not self._discarded
                finally:
                    if receive_task is not None:
                        if not receive_task.done():
                            receive_task.cancel()
                        # Always retrieve completion, including a normal-close
                        # exception raised while the parent stream is cancelled.
                        await asyncio.gather(receive_task, return_exceptions=True)
                    if not sender.done():
                        sender.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await sender
        except asyncio.CancelledError:
            await observer("t_tts_complete", time.monotonic(), {"cancelled": True})
            raise
        finally:
            self._connection = None
