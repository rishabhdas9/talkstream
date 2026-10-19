from __future__ import annotations

import asyncio
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.voice.lifecycle import bounded_cleanup
from app.voice.metrics import TurnTimer
from app.voice.session import VoiceSession


@pytest.mark.asyncio
async def test_cancelling_while_thinking_does_not_use_playback_interrupt_path() -> None:
    session = VoiceSession.__new__(VoiceSession)
    session._session_id = "session"
    session._supervised_tasks = set()
    session._cleanup = partial(bounded_cleanup, supervised_tasks=session._supervised_tasks)
    timer = TurnTimer(turn_id="session:1", turn_index=1, endpointing_strategy="local_vad")
    response = asyncio.create_task(asyncio.sleep(60))
    tts = SimpleNamespace(connection_id="tts-1", close_and_discard=AsyncMock())
    session._active_response_task = response
    session._active_response_timer = timer
    session._active_tts = tts
    session._active_tts_turn_id = timer.turn_id
    session._agent_playing_audio = False
    session._playback_turn_id = None
    session._interrupted_turns = set()
    session._cancelled_response_turns = set()
    session._history_ready = asyncio.Event()
    session._history_ready.set()
    session._todo_proxy = SimpleNamespace(cancel_pending=Mock(return_value=0))
    session._agent = SimpleNamespace(reconcile_interrupted_history=AsyncMock(return_value={}))
    session._send_json = AsyncMock()
    session._persist_timer = AsyncMock()

    await session._cancel_thinking_response(timer)

    assert response.cancelled()
    tts.close_and_discard.assert_awaited_once()
    session._agent.reconcile_interrupted_history.assert_awaited_once_with("")
    assert timer.dimensions["cancelled_while_thinking"] is True
    assert timer.dimensions.get("interrupted") is not True
    assert timer.turn_id not in session._interrupted_turns
    assert timer.turn_id in session._cancelled_response_turns
    assert session._history_ready.is_set()
    sent_types = [call.args[0]["type"] for call in session._send_json.await_args_list]
    assert "interrupt" not in sent_types
