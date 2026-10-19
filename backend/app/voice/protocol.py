from __future__ import annotations

import io
import json
import math
import struct
import wave
from dataclasses import dataclass
from typing import Any

from app.voice.feedback import CUE_LANGUAGES

CAPTURE_MAGIC = b"TLKC"
CAPTURE_VERSION = 1
CAPTURE_HEADER = struct.Struct("!4sBQd")
CAPTURE_HEADER_BYTES = CAPTURE_HEADER.size
OUTPUT_PREFIX = struct.Struct("!4sBI")
MAX_AUDIO_HEADER_BYTES = 4096
MAX_AUDIO_PAYLOAD_BYTES = 256 * 1024
AUDIO_SAMPLE_RATES = frozenset({8000, 16000, 22050, 24000, 44100, 48000})
OUTPUT_FIELDS = frozenset(
    {"turn_id", "generation_id", "sequence", "codec", "sample_rate_hz", "kind"}
)


class ProtocolError(ValueError):
    pass


def encode_output_audio(header: dict[str, Any], payload: bytes) -> bytes:
    if not isinstance(header, dict) or header.keys() != OUTPUT_FIELDS:
        raise ProtocolError("Audio header requires exactly the version2 fields")
    for field in ("turn_id", "generation_id"):
        if not isinstance(header[field], str) or not 1 <= len(header[field]) <= 128:
            raise ProtocolError(f"Invalid audio {field}")
    if type(header["sequence"]) is not int or not 0 <= header["sequence"] < 2**32:
        raise ProtocolError("Invalid audio sequence")
    if header["codec"] != "pcm_s16le" or header["kind"] not in ("answer", "cue"):
        raise ProtocolError("Unsupported audio codec or kind")
    if (
        type(header["sample_rate_hz"]) is not int
        or header["sample_rate_hz"] not in AUDIO_SAMPLE_RATES
    ):
        raise ProtocolError("Unsupported audio sample rate")
    if (
        not isinstance(payload, bytes)
        or not payload
        or len(payload) % 2
        or len(payload) > MAX_AUDIO_PAYLOAD_BYTES
    ):
        raise ProtocolError("Audio payload must be bounded complete PCM16 samples")
    try:
        encoded = json.dumps(
            header, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("Invalid audio header encoding") from exc
    if len(encoded) > MAX_AUDIO_HEADER_BYTES:
        raise ProtocolError("Audio header exceeds 4 KiB")
    return OUTPUT_PREFIX.pack(b"TLKS", 2, len(encoded)) + encoded + payload


def normalize_audio_payload(codec: str, payload: bytes, sample_rate_hz: int) -> bytes:
    if type(sample_rate_hz) is not int or sample_rate_hz not in AUDIO_SAMPLE_RATES:
        raise ProtocolError("Unsupported audio sample rate")
    if not isinstance(payload, bytes) or not payload or len(payload) > 16 * 1024 * 1024:
        raise ProtocolError("Invalid vendor audio payload")
    if codec == "wav":
        try:
            if payload[:4] != b"RIFF" or int.from_bytes(payload[4:8], "little") + 8 != len(payload):
                raise ProtocolError("WAV must be a complete container")
            with wave.open(io.BytesIO(payload), "rb") as reader:
                if (
                    reader.getnchannels(),
                    reader.getsampwidth(),
                    reader.getframerate(),
                    reader.getcomptype(),
                ) != (1, 2, sample_rate_hz, "NONE"):
                    raise ProtocolError("WAV must match mono PCM16 and the configured sample rate")
                frames = reader.getnframes()
                decoded = reader.readframes(frames)
                if len(decoded) != frames * 2:
                    raise ProtocolError("Truncated WAV audio")
                payload = decoded
        except (wave.Error, EOFError, struct.error) as exc:
            raise ProtocolError("Invalid WAV audio") from exc
    elif codec not in {"linear16", "pcm"}:
        raise ProtocolError("Unsupported vendor streaming codec")
    elif payload[:4] == b"RIFF" and payload[8:12] == b"WAVE":
        raise ProtocolError("WAV container does not match the raw PCM profile")
    if not payload or len(payload) % 2:
        raise ProtocolError("Audio requires complete PCM16 samples")
    return payload


@dataclass(frozen=True)
class CaptureFrame:
    capture_seq: int
    capture_time_ms: float
    pcm: bytes


@dataclass(frozen=True)
class ProbeOptions:
    enabled: bool = False
    stt_input_audio_codec: str | None = None
    stt_audio_encoding: str | None = None
    tts_output_audio_codec: str | None = None
    endpointing_strategy: str | None = None


@dataclass(frozen=True)
class ClientToolResult:
    call_id: str
    result: Any


CLIENT_MESSAGE_TYPES = frozenset(
    {
        "end_call",
        "interrupt_ack",
        "playback_finished",
        "playback_started",
        "probe_flush",
        "response_cue_started",
        "tool_result",
    }
)

SERVER_MESSAGE_TYPES = frozenset(
    {
        "agent_state",
        "agent_text",
        "audio_start",
        "billing",
        "call_ended",
        "error",
        "final_transcript",
        "interrupt",
        "interrupt_resolved",
        "ready",
        "response_cue",
        "response_cue_cancel",
        "tool_request",
        "turn_complete",
        "turn_metrics",
    }
)

_CLIENT_REQUIRED_FIELDS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "end_call": {},
    "interrupt_ack": {
        "turn_id": str,
        "interrupt_received_perf_ms": (int, float),
        "queue_cleared_perf_ms": (int, float),
        "audio_queue_cleared": bool,
    },
    "playback_finished": {
        "turn_id": str,
        "playback_end_perf_ms": (int, float),
        "played_audio_ms": (int, float),
    },
    "playback_started": {
        "turn_id": str,
        "last_speech_capture_seq": int,
        "audio_received_perf_ms": (int, float),
        "scheduled_perf_ms": (int, float),
        "playback_start_perf_ms": (int, float),
        "e2e_voice_to_voice_ms": (int, float),
    },
    "probe_flush": {},
    "response_cue_started": {
        "turn_id": str,
        "cue_id": str,
        "cue_start_perf_ms": (int, float),
        "feedback_voice_to_voice_ms": (int, float),
    },
    "tool_result": {"call_id": str, "result": object},
}

_SERVER_REQUIRED_FIELDS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "agent_state": {"state": str},
    "agent_text": {"text": str},
    "audio_start": {
        "turn_id": str,
        "last_speech_capture_seq": int,
        "last_speech_capture_time_ms": (int, float),
    },
    "billing": {
        "seconds": int,
        "cost_paise": int,
        "balance_paise": int,
    },
    "call_ended": {"reason": str},
    "error": {"code": str, "message": str},
    "final_transcript": {"turn_id": str, "text": str},
    "interrupt": {"turn_id": str},
    "interrupt_resolved": {"turn_id": str},
    "ready": {
        "session_id": str,
        "balance_paise": int,
        "price_per_minute_paise": int,
        "tts_sample_rate_hz": int,
    },
    "response_cue": {
        "turn_id": str,
        "cue_id": str,
        "cue_key": str,
        "language_code": str,
        "delay_ms": int,
        "last_speech_capture_time_ms": (int, float),
    },
    "response_cue_cancel": {
        "turn_id": str,
        "cue_id": str,
        "reason": str,
    },
    "tool_request": {"call_id": str, "name": str, "arguments": dict},
    "turn_complete": {"turn_id": str},
    "turn_metrics": {"turn_id": str, "stages": dict},
}


def encode_capture_frame(frame: CaptureFrame) -> bytes:
    if frame.capture_seq < 0:
        raise ProtocolError("capture_seq must be non-negative")
    if not math.isfinite(frame.capture_time_ms):
        raise ProtocolError("capture_time_ms must be finite")
    if not frame.pcm or len(frame.pcm) % 2:
        raise ProtocolError("PCM payload must contain complete 16-bit samples")
    return (
        CAPTURE_HEADER.pack(
            CAPTURE_MAGIC,
            CAPTURE_VERSION,
            frame.capture_seq,
            frame.capture_time_ms,
        )
        + frame.pcm
    )


def decode_capture_frame(message: bytes) -> CaptureFrame:
    if len(message) <= CAPTURE_HEADER_BYTES:
        raise ProtocolError("binary frame is shorter than its header")
    magic, version, capture_seq, capture_time_ms = CAPTURE_HEADER.unpack_from(message)
    if magic != CAPTURE_MAGIC:
        raise ProtocolError("binary frame has an invalid magic value")
    if version != CAPTURE_VERSION:
        raise ProtocolError(f"unsupported capture frame version: {version}")
    if not math.isfinite(capture_time_ms):
        raise ProtocolError("capture_time_ms must be finite")
    pcm = message[CAPTURE_HEADER_BYTES:]
    if len(pcm) % 2:
        raise ProtocolError("PCM payload must contain complete 16-bit samples")
    return CaptureFrame(capture_seq=capture_seq, capture_time_ms=capture_time_ms, pcm=pcm)


def decode_tool_result(message: dict[str, Any]) -> ClientToolResult:
    call_id = message.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise ProtocolError("tool_result requires a non-empty call_id")
    if "result" not in message:
        raise ProtocolError("tool_result requires a result field")
    return ClientToolResult(call_id=call_id, result=message["result"])


def validate_client_message(message: dict[str, Any]) -> dict[str, Any]:
    """Validate one client control message from the public WebSocket protocol."""

    validated = _validate_json_message(message, _CLIENT_REQUIRED_FIELDS, "client")
    message_type = validated["type"]
    if message_type == "interrupt_ack" and validated["audio_queue_cleared"] is not True:
        raise ProtocolError("interrupt_ack must confirm audio_queue_cleared")
    if message_type == "interrupt_ack":
        _require_order(validated, "interrupt_received_perf_ms", "queue_cleared_perf_ms")
    if message_type == "playback_started":
        ordered_fields = [
            "audio_received_perf_ms",
            "decode_complete_perf_ms",
            "scheduled_perf_ms",
            "playback_start_perf_ms",
        ]
        present = [field for field in ordered_fields if field in validated]
        for start, end in zip(present, present[1:], strict=False):
            _require_order(validated, start, end)
    if message_type == "response_cue_started" and (
        validated["cue_start_perf_ms"] < 0 or validated["feedback_voice_to_voice_ms"] < 0
    ):
        raise ProtocolError("response_cue_started timestamps must be non-negative")
    return validated


def validate_server_message(message: dict[str, Any]) -> dict[str, Any]:
    """Validate one server event from the public WebSocket protocol."""

    validated = _validate_json_message(message, _SERVER_REQUIRED_FIELDS, "server")
    message_type = validated["type"]
    if message_type == "agent_state" and validated["state"] not in {
        "interrupted",
        "listening",
        "speaking",
        "thinking",
        "user_speaking",
    }:
        raise ProtocolError("agent_state.state is invalid")
    if message_type == "call_ended" and validated["reason"] not in {
        "balance_exhausted",
        "error",
        "user",
    }:
        raise ProtocolError("call_ended.reason is invalid")
    if message_type == "response_cue":
        if validated["cue_key"] != "neutral_ack":
            raise ProtocolError("response_cue.cue_key is invalid")
        if validated["language_code"] not in {*CUE_LANGUAGES, "neutral"}:
            raise ProtocolError("response_cue.language_code is invalid")
        if validated["delay_ms"] < 0:
            raise ProtocolError("response_cue.delay_ms must be non-negative")
        if validated["last_speech_capture_time_ms"] < 0:
            raise ProtocolError("response_cue speech anchor must be non-negative")
    if message_type == "response_cue_cancel" and validated["reason"] not in {
        "answer_started",
        "call_ended",
        "new_user_speech",
        "turn_cancelled",
    }:
        raise ProtocolError("response_cue_cancel.reason is invalid")
    return validated


def _validate_json_message(
    message: dict[str, Any],
    schemas: dict[str, dict[str, type | tuple[type, ...]]],
    direction: str,
) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise ProtocolError(f"{direction} message must be a JSON object")
    message_type = message.get("type")
    if not isinstance(message_type, str) or message_type not in schemas:
        raise ProtocolError(f"unknown {direction} message type: {message_type!r}")
    for field_name, expected_type in schemas[message_type].items():
        if field_name not in message:
            raise ProtocolError(f"{message_type} requires {field_name}")
        value = message[field_name]
        if expected_type is object:
            continue
        if expected_type in {(int, float), int} and isinstance(value, bool):
            raise ProtocolError(f"{message_type}.{field_name} has an invalid type")
        if not isinstance(value, expected_type):
            raise ProtocolError(f"{message_type}.{field_name} has an invalid type")
        if isinstance(value, str) and not value:
            raise ProtocolError(f"{message_type}.{field_name} cannot be empty")
        if isinstance(value, int | float) and not math.isfinite(value):
            raise ProtocolError(f"{message_type}.{field_name} must be finite")
    return dict(message)


def _require_order(message: dict[str, Any], start: str, end: str) -> None:
    if float(message[end]) < float(message[start]):
        raise ProtocolError(f"{start} must not follow {end}")


def sanitize_vendor_message(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise ProtocolError("vendor message must be a JSON object")
    kind = message.get("type")
    known_types = {"audio", "config", "data", "error", "event", "events", "flush", "text"}
    return {
        "type": kind if isinstance(kind, str) and kind in known_types else "unknown",
        "field_count": len(message),
    }
