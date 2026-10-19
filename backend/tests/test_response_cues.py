from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.voice.feedback import ResponseCuePolicy
from app.voice.metrics import TurnTimer
from app.voice.session import VoiceSession


def test_response_cue_policy_is_deterministic_and_cooldown_limited() -> None:
    policy = ResponseCuePolicy(enabled=True, cooldown_turns=1)

    assert policy.reserve(turn_index=1) is True
    assert policy.reserve(turn_index=1) is False
    assert policy.reserve(turn_index=2) is False
    assert policy.reserve(turn_index=3) is True


def test_response_cue_policy_maps_supported_languages_and_fails_safe() -> None:
    policy = ResponseCuePolicy(enabled=True, cooldown_turns=0)

    assert policy.language_for("hi-IN") == "hi-IN"
    assert policy.language_for("ta-IN") == "ta-IN"
    assert policy.language_for("fr-FR") == "neutral"
    assert policy.language_for(None) == "neutral"


def test_disabled_response_cue_policy_never_reserves() -> None:
    policy = ResponseCuePolicy(enabled=False, cooldown_turns=0)

    assert policy.reserve(turn_index=1) is False


def test_answer_start_prepares_cue_cancel_without_websocket_io() -> None:
    timer = TurnTimer(turn_id="session:1", turn_index=1, endpointing_strategy="local_vad")
    timer.dimensions["generation_id"] = "g-1"
    timer.dimensions.update(
        {
            "response_cue_id": "session:1:cue",
            "response_cue_status": "started",
        }
    )

    message = VoiceSession._prepare_response_cue_cancel(timer, reason="answer_started")

    assert message == {
        "type": "response_cue_cancel",
        "turn_id": "session:1",
        "generation_id": "g-1",
        "cue_id": "session:1:cue",
        "reason": "answer_started",
    }
    assert timer.dimensions["response_cue_status"] == "cancelled"


@pytest.mark.asyncio
async def test_session_dispatches_one_scoped_cue_after_policy_delay() -> None:
    session = VoiceSession.__new__(VoiceSession)
    timer = TurnTimer(turn_id="session:1", turn_index=1, endpointing_strategy="local_vad")
    timer.dimensions["generation_id"] = "g-1"
    timer.set_speech_anchor(capture_seq=4, capture_time_ms=1000.0)
    timer.mark_server("t_stt_final", 2.0)
    session._settings = SimpleNamespace(response_cue_delay_ms=0)
    session._active_response_timer = timer
    session._agent_playing_audio = False
    session._interrupted_turns = set()
    session._cancelled_response_turns = set()
    session._response_cue_policy = ResponseCuePolicy(enabled=True, cooldown_turns=1)
    session._send_json = AsyncMock()
    session._session_id = "session"

    await session._dispatch_response_cue(timer, "hi-IN")

    message = session._send_json.await_args.args[0]
    assert message == {
        "type": "response_cue",
        "turn_id": "session:1",
        "generation_id": "g-1",
        "cue_id": "session:1:cue",
        "cue_key": "neutral_ack",
        "language_code": "hi-IN",
        "delay_ms": 0,
        "last_speech_capture_time_ms": 1000.0,
    }
    assert timer.dimensions["response_cue_status"] == "sent"


@pytest.mark.asyncio
async def test_session_does_not_dispatch_stale_response_cue() -> None:
    session = VoiceSession.__new__(VoiceSession)
    timer = TurnTimer(turn_id="session:1", turn_index=1, endpointing_strategy="local_vad")
    timer.dimensions["generation_id"] = "g-1"
    timer.set_speech_anchor(capture_seq=4, capture_time_ms=1000.0)
    session._settings = SimpleNamespace(response_cue_delay_ms=0)
    session._active_response_timer = None
    session._agent_playing_audio = False
    session._interrupted_turns = set()
    session._cancelled_response_turns = set()
    session._response_cue_policy = ResponseCuePolicy(enabled=True, cooldown_turns=0)
    session._send_json = AsyncMock()

    await session._dispatch_response_cue(timer, "hi-IN")

    session._send_json.assert_not_awaited()
