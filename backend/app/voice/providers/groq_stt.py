"""Opt-in Groq Whisper recognition of completed utterances after local endpointing.

Unlike the streaming profiles, audio is buffered until the local gate's endpoint and uploaded
once, so delay includes buffering and upload; there are no provider speech signals.
"""

from __future__ import annotations

import asyncio
import io
import time
import wave
from collections.abc import AsyncIterator
from datetime import datetime

import anyio
import httpx

from app.costs.models import BudgetExceeded, CostScope, Usage, WorkBound
from app.costs.service import CostService
from app.models import new_id
from app.voice.providers.contracts import SttEvent
from app.voice.providers.sarvam import SarvamRecognizer, _number
from app.voice.stt import SarvamSttError, SttConfig, check_recognition_scope

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODELS = frozenset({"whisper-large-v3-turbo"})
LANGUAGES = frozenset({"unknown", "auto", "en", "hi"})
MAX_UTTERANCE_BYTES = 120 * 32_000
MINIMUM_BILLED_MS = 10_000  # Groq bills every request for at least ten seconds.
_APP_LANGUAGES = {"en": "en-IN", "english": "en-IN", "hi": "hi-IN", "hindi": "hi-IN"}


def billed_ms(pcm_bytes: int) -> int:
    seconds = -(-pcm_bytes // 32_000)
    return max(MINIMUM_BILLED_MS, seconds * 1000)


class GroqSttClient:
    provider = "groq"

    def __init__(
        self,
        config: SttConfig,
        *,
        costs: CostService,
        scope: CostScope,
        expires_at: datetime,
        evaluation_cap_micro_inr: int | None = None,
    ) -> None:
        check_recognition_scope(scope, evaluation_cap_micro_inr)
        if config.sample_rate_hz != 16000:
            raise SarvamSttError("Metered recognition requires 16 kHz PCM16 input")
        if config.model not in MODELS or config.language_code not in LANGUAGES:
            raise SarvamSttError("Unsupported Groq model or language")
        self._config = config
        self._costs = costs
        self._scope = scope
        self._evaluation_cap_micro_inr = evaluation_cap_micro_inr
        self._expires_at = expires_at
        self._buffer = bytearray()
        self._results: asyncio.Queue[dict | BaseException] = asyncio.Queue()
        self._requests: set[asyncio.Task] = set()
        self._order = asyncio.Lock()  # Finals keep utterance order; asyncio locks are FIFO.
        self._http: httpx.AsyncClient | None = None
        self._closed = False

    def _remaining(self) -> float:
        return max(0.0, (self._expires_at - self._costs.clock.utc_now()).total_seconds())

    async def connect(self) -> None:
        if self._closed or self._remaining() <= 0:
            raise SarvamSttError("Recognition deadline expired or client closed")
        if self._http is None:
            self._http = httpx.AsyncClient()

    async def reconnect(self) -> None:
        # Nothing is connection-local: each utterance is its own request.
        await self.connect()

    async def send_audio(self, pcm: bytes) -> dict:
        if self._closed or len(pcm) % 2:
            raise SarvamSttError("Recognition client is closed or audio is not PCM16")
        if len(self._buffer) + len(pcm) > MAX_UTTERANCE_BYTES:
            raise SarvamSttError("Utterance exceeds the 120-second upload bound")
        self._buffer += pcm
        return {"audio": f"<pcm:{len(pcm)} bytes>"}

    async def flush(self) -> dict:
        pcm, self._buffer = bytes(self._buffer), bytearray()
        if pcm:
            task = asyncio.create_task(self._transcribe(pcm), name="groq-transcription")
            self._requests.add(task)
            task.add_done_callback(self._requests.discard)
        return {"type": "flush", "audio_bytes": len(pcm)}

    async def _transcribe(self, pcm: bytes) -> None:
        async with self._order:
            attempt_id, units = new_id(), billed_ms(len(pcm))
            try:
                await self._costs.reserve(
                    self._scope,
                    attempt_id,
                    self.provider,
                    self._config.model,
                    WorkBound(units={"audio_ms": units}),
                    evaluation_cap_micro_inr=self._evaluation_cap_micro_inr,
                )
            except Exception as exc:  # e.g. the allowance is exhausted: no request was made.
                await self._results.put(exc)
                return
            usage = None
            try:
                wav = io.BytesIO()
                with wave.open(wav, "wb") as audio:
                    audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    audio.writeframes(pcm)
                form = {"model": self._config.model, "response_format": "verbose_json"}
                if self._config.language_code in {"en", "hi"}:
                    form["language"] = self._config.language_code
                async with asyncio.timeout(min(self._remaining(), 15)):
                    response = await self._http.post(
                        URL,
                        headers={"Authorization": f"Bearer {self._config.api_key}"},
                        data=form | {"temperature": "0"},
                        files={"file": ("utterance.wav", wav.getvalue(), "audio/wav")},
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise SarvamSttError("Groq returned a non-object transcription")
                request_id = (payload.get("x_groq") or {}).get("id") or response.headers.get(
                    "x-request-id"
                )
                # The uploaded length is exact, so a completed request bills deterministically.
                usage = Usage(
                    units={"audio_ms": units},
                    provider_request_id=request_id if isinstance(request_id, str) else None,
                )
                await self._results.put(payload)
            except Exception as exc:
                await self._results.put(exc)
            finally:
                with anyio.CancelScope(shield=True):
                    await self._costs.settle(attempt_id, usage, "succeeded" if usage else "unknown")

    async def messages(self) -> AsyncIterator[dict]:
        while True:
            item = await self._results.get()
            if isinstance(item, BudgetExceeded):
                raise item
            if isinstance(item, BaseException):
                raise SarvamSttError("Groq transcription failed") from item
            yield item

    async def close(self) -> None:
        self._closed = True
        for task in list(self._requests):
            task.cancel()
        await asyncio.gather(*self._requests, return_exceptions=True)
        if self._http is not None:
            await self._http.aclose()
            self._http = None


class GroqRecognizer(SarvamRecognizer):
    """Final-only events; the local gate owns speech start and endpoint."""

    endpointing = "none"
    heard_speech = True  # No provider speech signals; every upload returns a result.

    async def events(self) -> AsyncIterator[SttEvent]:
        async for payload in self.client.messages():
            now = time.monotonic()
            text = payload.get("text")
            language = payload.get("language")
            raw = {key: payload[key] for key in ("language", "duration") if key in payload}
            if not isinstance(text, str) or not text.strip():
                yield SttEvent("other", now, raw=raw)
                continue
            yield SttEvent(
                "final",
                now,
                text=text.strip(),
                language=_APP_LANGUAGES.get(language.casefold())
                if isinstance(language, str)
                else None,
                confidence=None,
                audio_duration_s=_number(payload.get("duration")),
                raw=raw,
            )
