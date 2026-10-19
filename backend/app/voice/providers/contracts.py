"""Provider contracts that let one voice component be replaced at a time.

Adapters wrap the metered baseline clients; cost reservation, deadlines and cancellation
stay inside those clients. Canonical tool calls and history stay with the agent (S09).
"""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

import httpx
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.costs.models import CostScope

FailureCategory = Literal["authentication", "rate_limit", "timeout", "transport", "invalid_output"]


@dataclass(frozen=True)
class ProviderContext:
    scope: CostScope
    deadline: datetime
    turn_id: str | None = None
    generation_id: str | None = None

    def __post_init__(self) -> None:
        if self.deadline.utcoffset() is None:
            raise ValueError("Provider deadlines must be aware UTC times")


class ProviderFailure(RuntimeError):
    def __init__(self, category: FailureCategory, provider: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.provider = provider


@dataclass(frozen=True)
class SttEvent:
    kind: Literal["speech_start", "speech_end", "partial", "final", "error", "other"]
    received_at: float
    text: str | None = None
    language: str | None = None
    # None means the provider gave no confidence; it is never defaulted to a number.
    confidence: float | None = None
    provider_request_id: str | None = None
    audio_duration_s: float | None = None
    raw: dict[str, Any] | None = None
    utterance_id: int | None = None


@dataclass(frozen=True)
class TextEvent:
    kind: Literal["text", "done"]
    text: str
    generation_id: str | None


@dataclass(frozen=True)
class AudioChunk:
    generation_id: str | None
    data: bytes
    codec: str
    sample_rate_hz: int


class SpeechRecognizer(Protocol):
    # "provider" emits speech_start/speech_end; "none" relies on local speech gating.
    endpointing: Literal["provider", "none"]

    async def connect(self, context: ProviderContext) -> None: ...

    async def send_audio(self, pcm: bytes) -> dict[str, Any]: ...

    async def flush(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...

    def events(self) -> AsyncIterator[SttEvent]: ...


class TextGenerator(Protocol):
    def stream(self, transcript: str, context: ProviderContext) -> AsyncIterator[TextEvent]: ...


class SpeechSynthesizer(Protocol):
    def synthesize(
        self, text: AsyncIterator[str], context: ProviderContext
    ) -> AsyncIterator[AudioChunk]: ...

    async def close_and_discard(self, reason: str = "interrupted") -> None: ...


def check_endpointing(strategy: str, recognizer: SpeechRecognizer) -> None:
    """Provider endpointing and local gating are chosen explicitly, never stacked."""
    if strategy == "sarvam" and recognizer.endpointing != "provider":
        raise ValueError("Provider endpointing needs a recognizer that emits speech events")


def _status_category(status: int) -> FailureCategory | None:
    if status in {401, 403}:
        return "authentication"
    if status == 429:
        return "rate_limit"
    return "transport" if status >= 500 else None


def classify_failure(exc: BaseException, provider: str) -> ProviderFailure | None:
    """Map an exception (or anything it wraps) to a provider failure category."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        category: FailureCategory | None = None
        if isinstance(current, InvalidStatus | httpx.HTTPStatusError):
            category = _status_category(current.response.status_code)
        elif isinstance(current, TimeoutError | httpx.TimeoutException):
            category = "timeout"
        elif isinstance(
            current, ConnectionClosed | ConnectionError | socket.gaierror | httpx.TransportError
        ):
            category = "transport"
        elif isinstance(current, json.JSONDecodeError):
            category = "invalid_output"
        if category is not None:
            failure = ProviderFailure(category, provider, f"{provider} {category} failure")
            failure.__cause__ = exc
            return failure
        current = current.__cause__ or current.__context__
    return None
