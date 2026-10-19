from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import anyio
from websockets.asyncio.client import ClientConnection, connect

from app.costs.models import CostScope, Usage, WorkBound
from app.costs.service import CostService
from app.models import new_id
from app.voice.admission import recognition_bound_ms


class SarvamSttError(RuntimeError):
    pass


@dataclass(frozen=True)
class SttConfig:
    api_key: str
    model: str
    language_code: str
    sample_rate_hz: int
    input_audio_codec: str
    audio_encoding: str


def check_recognition_scope(scope: CostScope, evaluation_cap_micro_inr: int | None) -> None:
    """Recognition is paid by a call, or by a live-eval job with an explicit allowance."""
    if scope.session_id is None:
        if (
            not scope.job_id
            or not scope.job_id.startswith("live-eval:")
            or type(evaluation_cap_micro_inr) is not int
            or evaluation_cap_micro_inr <= 0
        ):
            raise ValueError("Recognition evaluation requires a live-eval job scope and allowance")
    elif evaluation_cap_micro_inr is not None:
        raise ValueError("Recognition evaluation allowance requires a live-eval job scope")


class SarvamSttClient:
    # Subclasses change only the wire protocol; reservation, deadlines and settlement stay here.
    provider = "sarvam"

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
        if config.sample_rate_hz != 16000 or config.input_audio_codec not in {
            "pcm_s16le",
            "pcm_l16",
            "pcm_raw",
        }:
            raise SarvamSttError("Metered recognition requires 16 kHz PCM16 input")
        self._config = config
        self._costs = costs
        self._scope = scope
        self._evaluation_cap_micro_inr = evaluation_cap_micro_inr
        self._expires_at = expires_at
        self._monotonic_deadline = costs.clock.monotonic() + max(
            0, (expires_at - costs.clock.utc_now()).total_seconds()
        )
        self._connection: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._attempt_id: str | None = None
        self._bound_ms = 0
        self._sent_bytes = 0
        self._reported: tuple[int, str, int] | None = None
        self._uncertain = False
        self._expiry_task: asyncio.Task | None = None

    def _remaining(self) -> float:
        return max(
            0,
            min(
                (self._expires_at - self._costs.clock.utc_now()).total_seconds(),
                self._monotonic_deadline - self._costs.clock.monotonic(),
            ),
        )

    def _ensure_live(self) -> None:
        if self._remaining() <= 0:
            raise SarvamSttError("Recognition deadline expired")
        if self._closed:
            raise SarvamSttError("Recognition client is closed")

    async def connect(self) -> None:
        async with self._send_lock:
            self._ensure_live()
            if self._attempt_id is not None:
                raise SarvamSttError("Recognition already started; use reconnect")
            await self._connect_locked()

    async def reconnect(self) -> None:
        async with self._send_lock:
            self._ensure_live()
            await self._finish_locked()
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        self._ensure_live()
        attempt_id = new_id()
        bound_ms = recognition_bound_ms(self._expires_at, self._costs.clock.utc_now())
        await self._costs.reserve(
            self._scope,
            attempt_id,
            self.provider,
            self._config.model,
            WorkBound(units={"audio_ms": bound_ms}),
            evaluation_cap_micro_inr=self._evaluation_cap_micro_inr,
        )
        self._attempt_id = attempt_id
        self._bound_ms = bound_ms
        self._sent_bytes = 0
        self._reported = None
        self._uncertain = False
        try:
            self._ensure_live()
        except SarvamSttError:
            await self._costs.settle(attempt_id, Usage(units={"audio_ms": 0}), "cancelled")
            self._attempt_id = None
            raise
        try:
            async with asyncio.timeout(
                min(self._remaining(), self._costs.policy.initialization_seconds)
            ):
                self._connection = await self._open_connection()
            self._ensure_live()
        except BaseException:
            self._uncertain = True
            with anyio.CancelScope(shield=True):
                await self._finish_locked()
            raise
        if self._expiry_task is None:
            self._expiry_task = asyncio.create_task(self._expire(), name="stt-deadline")

    async def _expire(self) -> None:
        await asyncio.sleep(self._remaining())
        try:
            await self.close()
        except Exception as exc:
            logging.getLogger("talkstream.voice").error(
                "stt_expiry_cleanup_failed:%s", type(exc).__name__
            )

    async def _open_connection(self) -> ClientConnection:
        query = urlencode(
            {
                "model": self._config.model,
                "mode": "transcribe",
                "language-code": self._config.language_code,
                "sample_rate": str(self._config.sample_rate_hz),
                "input_audio_codec": self._config.input_audio_codec,
                "flush_signal": "true",
                "vad_signals": "true",
            }
        )
        return await connect(
            f"wss://api.sarvam.ai/speech-to-text/ws?{query}",
            additional_headers={"Api-Subscription-Key": self._config.api_key},
            max_size=8 * 1024 * 1024,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=1,
        )

    async def send_audio(self, pcm: bytes) -> dict[str, Any]:
        message = {
            "audio": {
                "data": base64.b64encode(pcm).decode("ascii"),
                "sample_rate": str(self._config.sample_rate_hz),
                "encoding": self._config.audio_encoding,
            }
        }
        await self._send_json(message, audio_bytes=len(pcm))
        return {
            "audio": {
                "data": f"<base64:{len(message['audio']['data'])} chars>",
                "sample_rate": message["audio"]["sample_rate"],
                "encoding": message["audio"]["encoding"],
            }
        }

    async def flush(self) -> dict[str, str]:
        message = {"type": "flush"}
        await self._send_json(message)
        return message

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        connection = self._require_connection()
        try:
            async for raw_message in connection:
                if not isinstance(raw_message, str):
                    raise SarvamSttError("Saaras returned a non-JSON message")
                parsed = json.loads(raw_message)
                if not isinstance(parsed, dict):
                    raise SarvamSttError("Saaras returned a non-object JSON message")
                self._record_duration(parsed)
                yield parsed
        except asyncio.CancelledError:
            # Every teardown cancels the receiver; that hides nothing billable if no audio went.
            if self._sent_bytes:
                self._uncertain = True
            raise
        except Exception:
            self._uncertain = True
            raise

    def _record_duration(self, message: dict) -> None:
        data = message.get("data")
        if message.get("type") != "data" or not isinstance(data, dict):
            return
        metrics = data.get("metrics")
        request_id = data.get("request_id")
        value = metrics.get("audio_duration") if isinstance(metrics, dict) else None
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isinstance(request_id, str)
            or not request_id
        ):
            return
        try:
            duration = Decimal(str(value)) * 1000
            if not duration.is_finite() or duration < 0 or duration > 10**12:
                return
            milliseconds = int(duration.to_integral_value(rounding=ROUND_CEILING))
        except InvalidOperation:
            return
        # This pinned legacy profile accepts one covered result, not a billed session total.
        if self._reported is not None and self._reported[1] != request_id:
            self._uncertain = True
        self._reported = (milliseconds, request_id, self._sent_bytes)

    async def close(self) -> None:
        self._closed = True
        task = self._expiry_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._expiry_task = None
        async with self._send_lock:
            await self._finish_locked()

    async def _finish_locked(self) -> None:
        connection = self._connection
        self._connection = None
        clean = False
        cancellation = None
        if connection is not None:
            try:
                async with asyncio.timeout(1):
                    await connection.close()
                clean = getattr(connection, "close_code", None) in {1000, 1001}
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    cancellation = exc
                self._uncertain = True
                transport = getattr(connection, "transport", None)
                if transport is not None:
                    transport.abort()
        if self._attempt_id is None:
            if cancellation is not None:
                raise cancellation
            return
        usage = None
        reported = self._reported
        sent_ms = (self._sent_bytes * 1000 + 31999) // 32000
        if (
            clean
            and not self._uncertain
            and reported is not None
            and reported[2] == self._sent_bytes
            # Sarvam reports seconds to 0.01, so coverage is checked at 10-ms resolution.
            and reported[0] >= sent_ms // 10 * 10
        ):
            billed_ms = max(1000, ((max(reported[0], sent_ms) + 999) // 1000) * 1000)
            usage = Usage(units={"audio_ms": billed_ms}, provider_request_id=reported[1])
        elif clean and not self._uncertain and reported is None and self._sent_bytes == 0:
            # No audio was submitted (e.g. a rotation nobody spoke into): at most the minimum.
            usage = Usage(units={"audio_ms": 1000})
        outcome = "succeeded" if usage else "unknown"
        if usage is None:
            # _sent_bytes counts every frame before its send, so it bounds what Sarvam received;
            # one extra second covers provider rounding. Never the whole remaining call.
            sent_bound = max(1000, (sent_ms + 999) // 1000 * 1000 + 1000)
            usage = Usage(units={"audio_ms": min(self._bound_ms, sent_bound)})
        with anyio.CancelScope(shield=True):
            await self._costs.settle(self._attempt_id, usage, outcome)
            self._attempt_id = None
        if cancellation is not None:
            raise cancellation

    async def _send_json(self, message: dict[str, Any], *, audio_bytes: int = 0) -> None:
        await self._send_frame(json.dumps(message, separators=(",", ":")), audio_bytes=audio_bytes)

    async def _send_frame(self, frame: str | bytes, *, audio_bytes: int = 0) -> None:
        async with self._send_lock:
            self._ensure_live()
            connection = self._require_connection()
            if audio_bytes % 2 or self._sent_bytes + audio_bytes > self._bound_ms * 32:
                raise SarvamSttError("Audio exceeds reserved PCM duration")
            self._sent_bytes += audio_bytes
            try:
                async with asyncio.timeout(self._remaining()):
                    await connection.send(frame)
            except BaseException:
                self._uncertain = True
                raise

    def _require_connection(self) -> ClientConnection:
        if self._connection is None:
            raise SarvamSttError("Saaras WebSocket is not connected")
        return self._connection
