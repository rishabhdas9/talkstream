"""Opt-in Deepgram Nova-3 streaming recognition; the metered baseline client owns spending."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from decimal import ROUND_CEILING, Decimal
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from app.voice.providers.contracts import ProviderFailure, SttEvent
from app.voice.providers.sarvam import SarvamRecognizer, _number
from app.voice.stt import SarvamSttClient, SarvamSttError

MODELS = frozenset({"nova-3"})
# "multi" is Nova-3 code-switching (English and Hindi among its languages).
LANGUAGES = frozenset({"multi", "en", "en-IN", "hi"})
_APP_LANGUAGES = {"en": "en-IN", "hi": "hi-IN"}


class DeepgramSttClient(SarvamSttClient):
    provider = "deepgram"

    async def _open_connection(self):
        if self._config.model not in MODELS or self._config.language_code not in LANGUAGES:
            raise SarvamSttError("Unsupported Deepgram model or language")
        self._summary_received = False
        query = urlencode(
            {
                "model": self._config.model,
                "language": self._config.language_code,
                "encoding": "linear16",
                "sample_rate": 16000,
                "channels": 1,
                "interim_results": "true",
                "vad_events": "true",
                # Deepgram recommends 100 ms endpointing for code-switching.
                "endpointing": 100,
                "utterance_end_ms": 1000,
                "smart_format": "true",
            }
        )
        return await connect(
            f"wss://api.deepgram.com/v1/listen?{query}",
            additional_headers={"Authorization": f"Token {self._config.api_key}"},
            max_size=8 * 1024 * 1024,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=1,
        )

    async def send_audio(self, pcm: bytes) -> dict:
        await self._send_frame(pcm, audio_bytes=len(pcm))
        return {"audio": f"<pcm:{len(pcm)} bytes>"}

    async def flush(self) -> dict:
        message = {"type": "Finalize"}
        await self._send_json(message)
        return message

    def _record_duration(self, message: dict) -> None:
        if message.get("type") != "Metadata":
            return
        if self._summary_received:
            self._reported = None
            self._uncertain = True
            return
        self._summary_received = True
        request_id = message.get("request_id")
        duration = _number(message.get("duration"))
        if not isinstance(request_id, str) or not request_id or duration is None:
            return
        milliseconds = int(
            (Decimal(str(duration)) * 1000).to_integral_value(rounding=ROUND_CEILING)
        )
        self._reported = (milliseconds, request_id, self._sent_bytes)
        # The closing summary is the socket's billed total, so it settles a cancelled receiver.
        self._uncertain = False

    async def _finish_locked(self) -> None:
        cancelled = None
        if self._connection is not None and not self._summary_received:
            try:
                async with asyncio.timeout(2):
                    await self._connection.send('{"type":"CloseStream"}')
                    while not self._summary_received:
                        raw = await self._connection.recv()
                        self._record_duration(json.loads(raw) if isinstance(raw, str) else {})
            except (Exception, asyncio.CancelledError) as exc:
                self._uncertain = True
                if isinstance(exc, asyncio.CancelledError):
                    cancelled = exc
        await super()._finish_locked()
        if cancelled is not None:
            raise cancelled


class DeepgramRecognizer(SarvamRecognizer):
    """One final per utterance: is_final segments are joined until Deepgram's endpoint."""

    rotate_after_final = False

    async def connect(self, context=None) -> None:
        await super().connect(context)
        self._reset()

    async def reconnect(self) -> None:
        await super().reconnect()
        self._reset()

    def _reset(self) -> None:
        self._utterance = 0
        self._started = False
        self._segments: list[str] = []
        self._language: str | None = None
        self._seen_until = 0.0

    def _commit(self, now: float, message: dict) -> list[SttEvent]:
        if not self._segments:
            return []
        utterance, text, language = self._utterance, " ".join(self._segments), self._language
        self._utterance += 1
        self._started, self._segments, self._language = False, [], None
        request_id = (message.get("metadata") or {}).get("request_id")
        return [
            SttEvent("speech_end", now, raw=message, utterance_id=utterance),
            SttEvent(
                "final",
                now,
                text=text,
                language=language,
                confidence=None,  # Deepgram gives no language probability.
                provider_request_id=request_id if isinstance(request_id, str) else None,
                raw=message,
                utterance_id=utterance,
            ),
        ]

    async def events(self) -> AsyncIterator[SttEvent]:
        async for message in self.client.messages():
            now = time.monotonic()
            kind = message.get("type")
            if kind == "Error":
                yield SttEvent("error", now, raw=message)
                continue
            if kind == "UtteranceEnd":
                for event in self._commit(now, message):
                    yield event
                continue
            if kind != "Results":
                # SpeechStarted fires on noise too; a turn starts with its first words instead.
                yield SttEvent("other", now, raw=message)
                continue
            start, duration = _number(message.get("start")), _number(message.get("duration"))
            alternatives = (message.get("channel") or {}).get("alternatives") or [{}]
            transcript = alternatives[0].get("transcript")
            if start is None or duration is None or not isinstance(transcript, str):
                raise ProviderFailure("invalid_output", "deepgram", "Invalid recognition result")
            transcript = transcript.strip()
            if start + duration <= self._seen_until:
                yield SttEvent("other", now, raw=message)  # A repeated or stale segment.
                continue
            if transcript and not self._started:
                self._started = True
                yield SttEvent("speech_start", now, raw=message, utterance_id=self._utterance)
            if not message.get("is_final"):
                if transcript:
                    yield SttEvent(
                        "partial", now, text=transcript, raw=message, utterance_id=self._utterance
                    )
                continue
            self._seen_until = start + duration
            if transcript:
                self._segments.append(transcript)
                languages = alternatives[0].get("languages")
                if isinstance(languages, list) and languages and isinstance(languages[0], str):
                    tag = languages[0].split("-")[0]
                    self._language = self._language or _APP_LANGUAGES.get(tag)
            if message.get("speech_final") or message.get("from_finalize"):
                for event in self._commit(now, message):
                    yield event
