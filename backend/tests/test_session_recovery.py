from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from app.runtime import RuntimePolicy, SystemClock
from app.voice.session import VoiceSession, _is_transient_network_error
from app.voice.tts import SarvamTtsError


@pytest.mark.asyncio
async def test_response_loop_continues_after_tts_turn_failure() -> None:
    session = VoiceSession.__new__(VoiceSession)
    session._policy = RuntimePolicy()
    session._clock = SystemClock()
    session._turn_deadlines = {}
    session._consecutive_provider_failures = 0
    session._supervised_tasks = set()
    session._response_queue = asyncio.Queue()
    session._history_ready = asyncio.Event()
    session._history_ready.set()
    session._pending_history_timer = None
    session._active_response_task = None
    session._active_response_timer = None
    session._recover_tts_turn = AsyncMock()

    first_timer = SimpleNamespace(turn_index=1, turn_id="first")
    second_timer = SimpleNamespace(turn_index=2, turn_id="second")
    processed_second = asyncio.Event()

    async def process_turn(*, timer: object, transcript: str) -> None:
        del transcript
        if timer is first_timer:
            raise SarvamTtsError("vendor rejected text")
        processed_second.set()

    session._process_turn = process_turn
    await session._response_queue.put((first_timer, "first"))
    await session._response_queue.put((second_timer, "second"))

    worker = asyncio.create_task(session._response_loop())
    await asyncio.wait_for(processed_second.wait(), timeout=1)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    session._recover_tts_turn.assert_awaited_once_with(first_timer, ANY)


@pytest.mark.asyncio
async def test_response_loop_recovers_after_network_retries_are_exhausted() -> None:
    session = VoiceSession.__new__(VoiceSession)
    session._policy = RuntimePolicy()
    session._clock = SystemClock()
    session._turn_deadlines = {}
    session._consecutive_provider_failures = 0
    session._supervised_tasks = set()
    session._response_queue = asyncio.Queue()
    session._history_ready = asyncio.Event()
    session._history_ready.set()
    session._pending_history_timer = None
    session._active_response_task = None
    session._active_response_timer = None
    session._recover_network_turn = AsyncMock()

    first_timer = SimpleNamespace(turn_index=1, turn_id="first")
    second_timer = SimpleNamespace(turn_index=2, turn_id="second")
    attempts = 0
    processed_second = asyncio.Event()

    async def process_turn(*, timer: object, transcript: str) -> None:
        nonlocal attempts
        del transcript
        if timer is first_timer:
            attempts += 1
            raise socket.gaierror(8, "temporary DNS failure")
        processed_second.set()

    session._process_turn = process_turn
    await session._response_queue.put((first_timer, "first"))
    await session._response_queue.put((second_timer, "second"))

    worker = asyncio.create_task(session._response_loop())
    await asyncio.wait_for(processed_second.wait(), timeout=2)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert attempts == 1
    session._recover_network_turn.assert_awaited_once_with(first_timer, ANY)


def test_transient_network_detection_follows_wrapped_causes() -> None:
    wrapped = RuntimeError("SDK request failed")
    wrapped.__cause__ = socket.gaierror(8, "temporary DNS failure")

    assert _is_transient_network_error(wrapped) is True
    assert _is_transient_network_error(ValueError("bad application state")) is False


@pytest.mark.asyncio
async def test_response_loop_drops_a_turn_superseded_by_new_user_speech() -> None:
    session = VoiceSession.__new__(VoiceSession)
    session._policy = RuntimePolicy()
    session._clock = SystemClock()
    session._turn_deadlines = {}
    session._consecutive_provider_failures = 0
    session._supervised_tasks = set()
    session._response_queue = asyncio.Queue()
    session._history_ready = asyncio.Event()
    session._history_ready.set()
    session._pending_history_timer = None
    session._active_response_task = None
    session._active_response_timer = None
    session._latest_user_turn_index = 2
    session._persist_timer = AsyncMock()
    session._process_turn = AsyncMock()

    stale_timer = SimpleNamespace(turn_index=1, turn_id="stale", dimensions={})
    await session._response_queue.put((stale_timer, "stale"))

    worker = asyncio.create_task(session._response_loop())
    await asyncio.wait_for(session._response_queue.join(), timeout=1)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert stale_timer.dimensions["superseded_before_response"] is True
    session._process_turn.assert_not_awaited()
