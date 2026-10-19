from __future__ import annotations

import math

import pytest

from app.voice.protocol import (
    CLIENT_MESSAGE_TYPES,
    SERVER_MESSAGE_TYPES,
    CaptureFrame,
    ProtocolError,
    decode_capture_frame,
    encode_capture_frame,
    validate_client_message,
    validate_server_message,
)

CLIENT_EXAMPLES = {
    "end_call": {"type": "end_call"},
    "interrupt_ack": {
        "type": "interrupt_ack",
        "turn_id": "turn-1",
        "interrupt_received_perf_ms": 10.0,
        "queue_cleared_perf_ms": 11.0,
        "audio_queue_cleared": True,
    },
    "playback_finished": {
        "type": "playback_finished",
        "turn_id": "turn-1",
        "playback_end_perf_ms": 20.0,
        "played_audio_ms": 500.0,
    },
    "playback_started": {
        "type": "playback_started",
        "turn_id": "turn-1",
        "last_speech_capture_seq": 7,
        "audio_received_perf_ms": 10.0,
        "scheduled_perf_ms": 11.0,
        "playback_start_perf_ms": 12.0,
        "e2e_voice_to_voice_ms": 800.0,
    },
    "probe_flush": {"type": "probe_flush"},
    "response_cue_started": {
        "type": "response_cue_started",
        "turn_id": "turn-1",
        "cue_id": "cue-1",
        "cue_start_perf_ms": 12.0,
        "feedback_voice_to_voice_ms": 812.0,
    },
    "tool_result": {"type": "tool_result", "call_id": "call-1", "result": {"ok": True}},
}

SERVER_EXAMPLES = {
    "agent_state": {"type": "agent_state", "state": "thinking"},
    "agent_text": {"type": "agent_text", "text": "Hello."},
    "audio_start": {
        "type": "audio_start",
        "turn_id": "turn-1",
        "last_speech_capture_seq": 7,
        "last_speech_capture_time_ms": 20.0,
    },
    "billing": {"type": "billing", "seconds": 1, "cost_paise": 3, "balance_paise": 97},
    "call_ended": {"type": "call_ended", "reason": "user"},
    "error": {"type": "error", "code": "failed", "message": "Failed"},
    "final_transcript": {"type": "final_transcript", "turn_id": "turn-1", "text": "Hello"},
    "interrupt": {"type": "interrupt", "turn_id": "turn-1"},
    "interrupt_resolved": {"type": "interrupt_resolved", "turn_id": "turn-1"},
    "ready": {
        "type": "ready",
        "session_id": "session-1",
        "balance_paise": 500,
        "price_per_minute_paise": 200,
        "tts_sample_rate_hz": 24000,
    },
    "response_cue": {
        "type": "response_cue",
        "turn_id": "turn-1",
        "cue_id": "cue-1",
        "cue_key": "neutral_ack",
        "language_code": "hi-IN",
        "delay_ms": 350,
        "last_speech_capture_time_ms": 20.0,
    },
    "response_cue_cancel": {
        "type": "response_cue_cancel",
        "turn_id": "turn-1",
        "cue_id": "cue-1",
        "reason": "answer_started",
    },
    "tool_request": {
        "type": "tool_request",
        "call_id": "call-1",
        "name": "todo_add",
        "arguments": {"text": "buy milk"},
    },
    "turn_complete": {"type": "turn_complete", "turn_id": "turn-1"},
    "turn_metrics": {"type": "turn_metrics", "turn_id": "turn-1", "stages": {}},
}


def test_every_public_json_message_type_has_a_validated_example() -> None:
    assert set(CLIENT_EXAMPLES) == set(CLIENT_MESSAGE_TYPES)
    assert set(SERVER_EXAMPLES) == set(SERVER_MESSAGE_TYPES)
    for message in CLIENT_EXAMPLES.values():
        assert validate_client_message(message) == message
    for message in SERVER_EXAMPLES.values():
        assert validate_server_message(message) == message


def test_protocol_rejects_unknown_missing_and_invalid_fields() -> None:
    with pytest.raises(ProtocolError, match="unknown client"):
        validate_client_message({"type": "mystery"})
    with pytest.raises(ProtocolError, match="requires turn_id"):
        validate_server_message({"type": "interrupt"})
    with pytest.raises(ProtocolError, match="invalid type"):
        validate_client_message(
            {**CLIENT_EXAMPLES["playback_started"], "last_speech_capture_seq": True}
        )
    with pytest.raises(ProtocolError, match="confirm"):
        validate_client_message({**CLIENT_EXAMPLES["interrupt_ack"], "audio_queue_cleared": False})
    with pytest.raises(ProtocolError, match="must not follow"):
        validate_client_message(
            {
                **CLIENT_EXAMPLES["interrupt_ack"],
                "interrupt_received_perf_ms": 12.0,
                "queue_cleared_perf_ms": 11.0,
            }
        )
    with pytest.raises(ProtocolError, match="state is invalid"):
        validate_server_message({"type": "agent_state", "state": "unknown"})
    with pytest.raises(ProtocolError, match="cue_key is invalid"):
        validate_server_message({**SERVER_EXAMPLES["response_cue"], "cue_key": "remote_url"})
    with pytest.raises(ProtocolError, match="language_code is invalid"):
        validate_server_message({**SERVER_EXAMPLES["response_cue"], "language_code": "fr-FR"})


def test_capture_frame_round_trip() -> None:
    frame = CaptureFrame(capture_seq=5, capture_time_ms=123.5, pcm=b"\x01\x00" * 512)
    assert decode_capture_frame(encode_capture_frame(frame)) == frame


@pytest.mark.parametrize(
    "frame",
    [
        CaptureFrame(capture_seq=-1, capture_time_ms=1.0, pcm=b"\0\0"),
        CaptureFrame(capture_seq=1, capture_time_ms=math.nan, pcm=b"\0\0"),
        CaptureFrame(capture_seq=1, capture_time_ms=1.0, pcm=b"\0"),
    ],
)
def test_capture_frame_rejects_invalid_metadata(frame: CaptureFrame) -> None:
    with pytest.raises(ProtocolError):
        encode_capture_frame(frame)
