"""Opt-in Sarvam realtime protocol; the baseline client still owns spending and deadlines."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from app.voice.providers.contracts import ProviderFailure, SttEvent
from app.voice.providers.sarvam import SarvamRecognizer, _number
from app.voice.stt import SarvamSttClient, SarvamSttError

LANGUAGES = frozenset(
    {
        "auto",
        "en-IN",
        "hi-IN",
        "bn-IN",
        "kn-IN",
        "ml-IN",
        "mr-IN",
        "or-IN",
        "pa-IN",
        "ta-IN",
        "te-IN",
        "gu-IN",
        "as-IN",
        "ur-IN",
        "ne-IN",
        "kok-IN",
        "ks-IN",
        "sd-IN",
        "sa-IN",
        "sat-IN",
        "mni-IN",
        "brx-IN",
        "mai-IN",
        "doi-IN",
    }
)
MODELS = {"saaras:v3-realtime", "saaras:v4"}


class SarvamRealtimeClient(SarvamSttClient):
    async def _open_connection(self):
        if self._config.model not in MODELS or self._config.language_code not in LANGUAGES:
            raise SarvamSttError("Unsupported Sarvam realtime model or language")
        self.request_id = None
        self._summary_received = False
        query = urlencode(
            {
                "model": self._config.model,
                "language_code": self._config.language_code,
                "sample_rate": 16000,
                "encoding": "linear16",
                "endpointing": "vad",
                "stream_type": "fast",
                "mode": "transcribe",
                "return_timestamps": "true",
            }
        )
        # Retain the connection before checking readiness so failed initialization closes it.
        self._connection = await connect(
            f"wss://api.sarvam.ai/speech-to-text-realtime/ws?{query}",
            additional_headers={"Api-Subscription-Key": self._config.api_key},
            max_size=8 * 1024 * 1024,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=1,
        )
        message = self._parse(await self._connection.recv())
        if message.get("event") != "session.begin" or not isinstance(
            message.get("request_id"), str
        ):
            raise ProviderFailure("invalid_output", "sarvam", "Realtime initialization failed")
        self.request_id = message["request_id"]
        if not self.request_id:
            raise ProviderFailure("invalid_output", "sarvam", "Missing realtime request identity")
        config = message.get("config", {})
        if not isinstance(config, dict) or any(
            key in config and config[key] != expected
            for key, expected in {
                "model": self._config.model,
                "language_code": self._config.language_code,
                "encoding": "linear16",
                "sample_rate": 16000,
                "turn_detection": "vad",
            }.items()
        ):
            raise ProviderFailure("invalid_output", "sarvam", "Realtime configuration mismatch")
        return self._connection

    @staticmethod
    def _parse(raw):
        try:
            message = json.loads(raw) if isinstance(raw, str) else None
        except (ValueError, TypeError):
            message = None
        if not isinstance(message, dict):
            raise ProviderFailure("invalid_output", "sarvam", "Invalid realtime response")
        return message

    async def send_audio(self, pcm: bytes) -> dict:
        encoded = base64.b64encode(pcm).decode("ascii")
        await self._send_json({"event": "audio_input", "audio": encoded}, audio_bytes=len(pcm))
        return {"event": "audio_input", "audio": f"<base64:{len(encoded)} chars>"}

    async def flush(self) -> dict:
        # Manual flush is not part of the selected provider-VAD protocol.
        raise SarvamSttError("Realtime provider endpointing does not accept manual flush")

    def _record_duration(self, message: dict) -> None:
        if message.get("event") != "session.end":
            return
        if self._summary_received:
            self._reported = None
            self._uncertain = True
            return
        self._summary_received = True
        if message.get("request_id") != self.request_id:
            self._uncertain = True
            return
        value = message.get("audio_duration_s")
        if type(value) not in (str, int, float):
            return
        try:
            duration = Decimal(str(value)) * 1000
            if not duration.is_finite() or duration < 0 or duration > 10**12:
                return
            milliseconds = int(duration.to_integral_value(rounding=ROUND_CEILING))
        except InvalidOperation:
            return
        self._reported = (milliseconds, self.request_id, self._sent_bytes)
        # A same-socket billed total reconciles uncertainty from cancelling its receiver.
        self._uncertain = False

    async def _finish_locked(self) -> None:
        cancelled = None
        if (
            self._connection is not None
            and getattr(self, "request_id", None)
            and not self._summary_received
        ):
            try:
                async with asyncio.timeout(1):
                    await self._connection.send('{"event":"end"}')
                    while not self._summary_received:
                        self._record_duration(self._parse(await self._connection.recv()))
            except (Exception, asyncio.CancelledError) as exc:
                self._uncertain = True
                if isinstance(exc, asyncio.CancelledError):
                    cancelled = exc
        await super()._finish_locked()
        if cancelled is not None:
            raise cancelled


class SarvamRealtimeRecognizer(SarvamRecognizer):
    rotate_after_final = False

    async def connect(self, context=None) -> None:
        await super().connect(context)
        self._reset_events()

    async def reconnect(self) -> None:
        await super().reconnect()
        self._reset_events()

    def _reset_events(self) -> None:
        self._last_final = -1
        self._latest_utterance = -1
        self._signals: set[tuple[int, str]] = set()
        self._pending_utterances: set[int] = set()

    async def events(self) -> AsyncIterator[SttEvent]:
        async for message in self.client.messages():
            kind = message.get("event")
            now = time.monotonic()
            event = SttEvent("other", now, raw=message)
            if kind == "error":
                yield SttEvent("error", now, raw=message)
                continue
            if kind not in {
                "vad.speech_start",
                "vad.speech_end",
                "transcript.partial",
                "transcript.final",
            }:
                yield event
                continue
            index = message.get("utterance_idx")
            if type(index) is not int or index < 0:
                raise ProviderFailure("invalid_output", "sarvam", "Invalid utterance identity")
            if (index <= self._last_final and index not in self._pending_utterances) or (
                index < self._latest_utterance and kind != "transcript.final"
            ):
                yield event
                continue
            if index > self._latest_utterance:
                self._signals.clear()
            self._latest_utterance = max(index, self._latest_utterance)
            if kind != "transcript.final":
                self._pending_utterances.add(index)
                if len(self._pending_utterances) > 16:
                    raise ProviderFailure("invalid_output", "sarvam", "Too many pending utterances")
            if kind.startswith("vad."):
                if kind == "vad.speech_start" and (index, "vad.speech_end") in self._signals:
                    yield event
                    continue
                if (index, kind) not in self._signals:
                    self._signals.add((index, kind))
                    event = SttEvent(
                        kind.removeprefix("vad."), now, raw=message, utterance_id=index
                    )
            else:
                final = kind == "transcript.final"
                if final:
                    self._pending_utterances.discard(index)
                    self._last_final = max(self._last_final, index)
                    if index == self._latest_utterance:
                        self._signals.clear()
                text = message.get("text")
                if isinstance(text, str) and text.strip():
                    language = message.get("language")
                    confidence = message.get("language_confidence")
                    if isinstance(confidence, str):
                        try:
                            confidence = float(confidence)
                        except ValueError:
                            confidence = None
                    event = SttEvent(
                        "final" if final else "partial",
                        now,
                        text=text.strip(),
                        language=language if isinstance(language, str) else None,
                        confidence=_number(confidence, upper=1) if final else None,
                        provider_request_id=self.client.request_id,
                        raw=message,
                        utterance_id=index,
                    )
            yield event
