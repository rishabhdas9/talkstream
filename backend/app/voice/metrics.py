from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

SERVER_TIMESTAMPS = {
    "t_audio_frame_server_receive",
    "t_speech_start_server",
    "t_last_speech_frame_server",
    "t_endpoint_decision",
    "t_stt_flush_sent",
    "t_stt_final",
    "t_llm_request_start",
    "t_llm_first_reasoning_token",
    "t_llm_first_visible_token",
    "t_llm_first_speakable_chunk",
    "t_llm_complete",
    "t_tts_connection_acquire_start",
    "t_tts_connection_acquire_end",
    "t_tts_text_submitted",
    "t_tts_first_chunk",
    "t_tts_complete",
    "t_audio_sent_server",
    "t_response_cue_sent_server",
    "t_barge_speech_onset_server",
    "t_barge_speech_detected_server",
    "t_interrupt_sent_server",
    "t_interrupt_ack_received_server",
}

CLIENT_TIMESTAMPS = {
    "t_client_audio_received_ms",
    "t_client_decode_complete_ms",
    "t_client_audio_scheduled_ms",
    "t_client_playback_start_ms",
    "t_client_playback_end_ms",
    "t_client_response_cue_start_ms",
    "t_interrupt_received_client_ms",
    "t_playback_queue_cleared_client_ms",
}

BARGE_SERVER_TIMESTAMPS = {
    "t_barge_speech_onset_server",
    "t_barge_speech_detected_server",
    "t_interrupt_sent_server",
    "t_interrupt_ack_received_server",
}

BARGE_CLIENT_TIMESTAMPS = {
    "t_interrupt_received_client_ms",
    "t_playback_queue_cleared_client_ms",
}

OPTIONAL_SERVER_TIMESTAMPS = {
    "t_llm_first_reasoning_token",
    "t_response_cue_sent_server",
    *BARGE_SERVER_TIMESTAMPS,
}

OPTIONAL_CLIENT_TIMESTAMPS = {
    "t_client_response_cue_start_ms",
    *BARGE_CLIENT_TIMESTAMPS,
}

SERVER_ORDER_CONSTRAINTS = (
    ("t_last_speech_frame_server", "t_endpoint_decision"),
    ("t_stt_flush_sent", "t_stt_final"),
    ("t_llm_request_start", "t_llm_first_reasoning_token"),
    ("t_llm_request_start", "t_llm_first_visible_token"),
    ("t_llm_request_start", "t_llm_first_speakable_chunk"),
    ("t_llm_request_start", "t_llm_complete"),
    ("t_tts_connection_acquire_start", "t_tts_connection_acquire_end"),
    ("t_tts_text_submitted", "t_tts_first_chunk"),
    ("t_tts_text_submitted", "t_tts_complete"),
    ("t_tts_first_chunk", "t_audio_sent_server"),
    ("t_barge_speech_onset_server", "t_barge_speech_detected_server"),
    ("t_barge_speech_detected_server", "t_interrupt_sent_server"),
    ("t_interrupt_sent_server", "t_interrupt_ack_received_server"),
)

CLIENT_ORDER_CONSTRAINTS = (
    ("t_client_audio_received_ms", "t_client_decode_complete_ms"),
    ("t_client_decode_complete_ms", "t_client_audio_scheduled_ms"),
    ("t_client_audio_scheduled_ms", "t_client_playback_start_ms"),
    ("t_client_playback_start_ms", "t_client_playback_end_ms"),
    ("t_interrupt_received_client_ms", "t_playback_queue_cleared_client_ms"),
)


@dataclass
class TurnTimer:
    turn_id: str
    turn_index: int
    endpointing_strategy: str
    server_timestamps: dict[str, float] = field(default_factory=dict)
    client_timestamps_ms: dict[str, float] = field(default_factory=dict)
    last_speech_capture_seq: int | None = None
    last_speech_capture_time_ms: float | None = None
    dimensions: dict[str, Any] = field(default_factory=dict)
    raw_provider_metrics: dict[str, Any] | None = None
    tool_spans: list[dict[str, Any]] = field(default_factory=list)

    def mark_server(self, name: str, timestamp: float | None = None) -> float:
        if name not in SERVER_TIMESTAMPS:
            raise ValueError(f"unknown server timestamp: {name}")
        value = time.monotonic() if timestamp is None else timestamp
        self._validate_finite(value)
        self._validate_order(
            self.server_timestamps,
            name,
            value,
            SERVER_ORDER_CONSTRAINTS,
            clock="server",
        )
        self.server_timestamps.setdefault(name, value)
        return self.server_timestamps[name]

    def mark_client(self, name: str, timestamp_ms: float) -> None:
        if name not in CLIENT_TIMESTAMPS:
            raise ValueError(f"unknown client timestamp: {name}")
        self._validate_finite(timestamp_ms)
        self._validate_order(
            self.client_timestamps_ms,
            name,
            timestamp_ms,
            CLIENT_ORDER_CONSTRAINTS,
            clock="client",
        )
        self.client_timestamps_ms[name] = timestamp_ms

    def set_speech_anchor(self, *, capture_seq: int, capture_time_ms: float) -> None:
        if capture_seq < 0:
            raise ValueError("capture sequence must be non-negative")
        self._validate_finite(capture_time_ms)
        self.last_speech_capture_seq = capture_seq
        self.last_speech_capture_time_ms = capture_time_ms

    def record_tool_span(self, span: dict[str, Any]) -> None:
        required = {"name", "call_id", "start_server", "end_server", "duration_ms", "outcome"}
        missing = required - span.keys()
        if missing:
            raise ValueError(f"tool span is missing fields: {sorted(missing)}")
        start = span["start_server"]
        end = span["end_server"]
        duration = span["duration_ms"]
        if not all(isinstance(value, int | float) for value in (start, end, duration)):
            raise ValueError("tool span timestamps and duration must be numeric")
        self._validate_finite(float(start))
        self._validate_finite(float(end))
        self._validate_finite(float(duration))
        if end < start or duration < 0:
            raise ValueError("tool span must have a non-negative duration")
        self.tool_spans.append(dict(span))

    def derived_metrics(self) -> dict[str, float | None]:
        server = self.server_timestamps
        client = self.client_timestamps_ms
        endpoint_to_final = self._difference_ms(server, "t_stt_final", "t_endpoint_decision")
        flush_to_final = self._difference_ms(server, "t_stt_final", "t_stt_flush_sent")
        visible_to_speakable = self._difference_ms(
            server,
            "t_llm_first_speakable_chunk",
            "t_llm_first_visible_token",
        )
        tts_connection_wait = self._difference_ms(
            server,
            "t_tts_text_submitted",
            "t_llm_first_speakable_chunk",
        )
        e2e = None
        playback_start = client.get("t_client_playback_start_ms")
        if playback_start is not None and self.last_speech_capture_time_ms is not None:
            e2e = playback_start - self.last_speech_capture_time_ms
        feedback_start = client.get("t_client_response_cue_start_ms")
        feedback_e2e = None
        if feedback_start is not None and self.last_speech_capture_time_ms is not None:
            feedback_e2e = feedback_start - self.last_speech_capture_time_ms
        return {
            "upstream_audio_transport_ms": None,
            "endpoint_decision_ms": self._difference_ms(
                server, "t_endpoint_decision", "t_last_speech_frame_server"
            ),
            "stt_flush_to_final_ms": flush_to_final,
            "stt_endpoint_to_final_ms": endpoint_to_final,
            "stt_eot_ms": self._difference_ms(server, "t_stt_final", "t_last_speech_frame_server"),
            "orchestrator_queue_ms": self._difference_ms(
                server, "t_llm_request_start", "t_stt_final"
            ),
            "llm_visible_ttft_ms": self._difference_ms(
                server, "t_llm_first_visible_token", "t_llm_request_start"
            ),
            "llm_first_speakable_ms": self._difference_ms(
                server, "t_llm_first_speakable_chunk", "t_llm_request_start"
            ),
            "llm_visible_to_speakable_ms": visible_to_speakable,
            "tts_ttfb_ms": self._difference_ms(server, "t_tts_first_chunk", "t_tts_text_submitted"),
            "tts_connection_acquire_ms": self._difference_ms(
                server,
                "t_tts_connection_acquire_end",
                "t_tts_connection_acquire_start",
            ),
            "tts_connection_wait_ms": (
                max(0.0, tts_connection_wait) if tts_connection_wait is not None else None
            ),
            "client_decode_ms": self._difference(
                client,
                "t_client_decode_complete_ms",
                "t_client_audio_received_ms",
            ),
            "client_schedule_ms": self._difference(
                client,
                "t_client_audio_scheduled_ms",
                "t_client_decode_complete_ms",
            ),
            "downstream_to_playback_ms": self._difference(
                client, "t_client_playback_start_ms", "t_client_audio_received_ms"
            ),
            "e2e_voice_to_voice_ms": e2e,
            "answer_voice_to_voice_ms": e2e,
            "feedback_voice_to_voice_ms": feedback_e2e,
            "answer_after_feedback_ms": (
                playback_start - feedback_start
                if playback_start is not None and feedback_start is not None
                else None
            ),
            "response_cue_dispatch_ms": self._difference_ms(
                server, "t_response_cue_sent_server", "t_stt_final"
            ),
            "barge_detection_ms": self._difference_ms(
                server,
                "t_barge_speech_detected_server",
                "t_barge_speech_onset_server",
            ),
            "barge_client_flush_ms": self._difference(
                client,
                "t_playback_queue_cleared_client_ms",
                "t_interrupt_received_client_ms",
            ),
            "barge_in_stop_ack_ms": self._difference_ms(
                server,
                "t_interrupt_ack_received_server",
                "t_barge_speech_detected_server",
            ),
        }

    def waterfall(self) -> dict[str, float | None]:
        derived = self.derived_metrics()
        return {
            "endpoint_window_ms": derived["endpoint_decision_ms"],
            "stt_ms": derived["stt_flush_to_final_ms"]
            if derived["stt_flush_to_final_ms"] is not None
            else derived["stt_endpoint_to_final_ms"],
            "llm_ttft_ms": derived["llm_visible_ttft_ms"],
            "first_speakable_ms": derived["llm_visible_to_speakable_ms"],
            "tts_connection_wait_ms": derived["tts_connection_wait_ms"],
            "tts_ttfb_ms": derived["tts_ttfb_ms"],
            "transport_playback_ms": derived["downstream_to_playback_ms"],
            "e2e_voice_to_voice_ms": derived["e2e_voice_to_voice_ms"],
            "feedback_voice_to_voice_ms": derived["feedback_voice_to_voice_ms"],
            "answer_after_feedback_ms": derived["answer_after_feedback_ms"],
        }

    def missing_timestamps(self) -> list[str]:
        required_server = SERVER_TIMESTAMPS - OPTIONAL_SERVER_TIMESTAMPS
        required_client = CLIENT_TIMESTAMPS - OPTIONAL_CLIENT_TIMESTAMPS
        if self.endpointing_strategy == "sarvam":
            required_server.discard("t_stt_flush_sent")
        if self.dimensions.get("interrupted") is True:
            required_server.update(BARGE_SERVER_TIMESTAMPS)
            required_client.update(BARGE_CLIENT_TIMESTAMPS)
            required_client.discard("t_client_playback_end_ms")
        missing = sorted(required_server - self.server_timestamps.keys())
        missing.extend(sorted(required_client - self.client_timestamps_ms.keys()))
        return missing

    @property
    def censored(self) -> bool:
        """A turn is censored when a required stage timestamp was not observed."""

        return bool(self.missing_timestamps())

    @staticmethod
    def _difference_ms(values: dict[str, float], end: str, start: str) -> float | None:
        difference = TurnTimer._difference(values, end, start)
        return difference * 1000 if difference is not None else None

    @staticmethod
    def _difference(values: dict[str, float], end: str, start: str) -> float | None:
        if end not in values or start not in values:
            return None
        return values[end] - values[start]

    @staticmethod
    def _validate_finite(value: float) -> None:
        if not math.isfinite(value):
            raise ValueError("timestamp must be finite")

    @staticmethod
    def _validate_order(
        values: dict[str, float],
        name: str,
        value: float,
        constraints: tuple[tuple[str, str], ...],
        *,
        clock: str,
    ) -> None:
        candidate = {**values, name: value}
        for start, end in constraints:
            if start in candidate and end in candidate and candidate[end] < candidate[start]:
                raise ValueError(f"{clock} timestamp order violated: {start} must precede {end}")


def percentile(values: list[float], percentile_value: float) -> float | None:
    if not values:
        return None
    if not 0 <= percentile_value <= 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile_value / 100
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight
