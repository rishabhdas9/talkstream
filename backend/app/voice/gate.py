from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.voice.logging import log_voice_event
from app.voice.vad import (
    FRAME_SAMPLES,
    SAMPLE_RATE_HZ,
    EnergyVad,
    IVad,
    SileroOnnxVad,
    VadInitializationError,
    default_silero_model_path,
)

FRAME_DURATION_MS = FRAME_SAMPLES * 1000 / SAMPLE_RATE_HZ


def build_vad_gate(settings: Settings, *, session_id: str | None = None) -> VadGate:
    vad = None
    if settings.vad_provider == "silero":
        model_path = (
            Path(settings.vad_model_path)
            if settings.vad_model_path
            else default_silero_model_path()
        )
        try:
            vad = SileroOnnxVad(model_path)
        except VadInitializationError:
            log_voice_event(
                logging.WARNING,
                "vad_fallback",
                session_id=session_id,
                turn_id=None,
                requested="silero_onnx",
                selected="energy",
                reason="vad_initialization_failed",
            )
    if vad is None:
        vad = EnergyVad(rms_threshold=settings.vad_energy_threshold)
    return VadGate(
        vad,
        end_silence_ms=settings.vad_end_silence_ms,
        pre_roll_ms=settings.vad_pre_roll_ms,
        speech_threshold=settings.vad_speech_threshold,
        pre_roll_threshold=settings.vad_pre_roll_threshold,
    )


@dataclass(frozen=True)
class VadFrame:
    capture_seq: int
    capture_time_ms: float
    server_received_at: float
    pcm: bytes


@dataclass(frozen=True)
class VadGateStats:
    received_frames: int
    forwarded_frames: int
    gated_silent_frames: int
    pre_roll_frames: int


@dataclass(frozen=True)
class VadGateDecision:
    forward_frames: tuple[VadFrame, ...] = ()
    speech_started: bool = False
    speech_resumed: bool = False
    silence_started: bool = False
    endpoint_fired: bool = False
    last_speech_frame: VadFrame | None = None
    completed_stats: VadGateStats | None = None
    speech_probability: float = 0.0


@dataclass(frozen=True)
class BargeInDecision:
    candidate_started: bool = False
    detected: bool = False
    candidate_cancelled: bool = False
    onset_server_time: float | None = None


class BargeInDetector:
    def __init__(self, *, sustained_speech_ms: int) -> None:
        self._required_frames = max(
            2,
            math.ceil(sustained_speech_ms / FRAME_DURATION_MS) + 1,
        )
        self._speech_frames = 0
        self._onset_server_time: float | None = None
        self._detected = False

    def observe_speech(self, frame: VadFrame) -> BargeInDecision:
        if self._detected:
            return BargeInDecision(
                detected=True,
                onset_server_time=self._onset_server_time,
            )
        candidate_started = self._speech_frames == 0
        if candidate_started:
            self._onset_server_time = frame.server_received_at
        self._speech_frames += 1
        self._detected = self._speech_frames >= self._required_frames
        return BargeInDecision(
            candidate_started=candidate_started,
            detected=self._detected,
            onset_server_time=self._onset_server_time,
        )

    def observe_silence(self) -> BargeInDecision:
        was_candidate = self._speech_frames > 0 and not self._detected
        onset = self._onset_server_time
        self.reset()
        return BargeInDecision(
            candidate_cancelled=was_candidate,
            onset_server_time=onset,
        )

    def reset(self) -> None:
        self._speech_frames = 0
        self._onset_server_time = None
        self._detected = False


class VadGate:
    def __init__(
        self,
        vad: IVad,
        *,
        end_silence_ms: int,
        pre_roll_ms: int,
        speech_threshold: float,
        pre_roll_threshold: float,
    ) -> None:
        if pre_roll_threshold > speech_threshold:
            raise ValueError("pre-roll threshold cannot exceed speech threshold")
        self._vad = vad
        self._end_silence_frames = max(1, round(end_silence_ms / FRAME_DURATION_MS))
        self._pre_roll = deque[VadFrame](maxlen=max(0, round(pre_roll_ms / FRAME_DURATION_MS)))
        self._speech_threshold = speech_threshold
        self._pre_roll_threshold = pre_roll_threshold
        self._speech_active = False
        self._silence_frames = 0
        self._last_speech_frame: VadFrame | None = None
        self._received_frames = 0
        self._forwarded_frames = 0
        self._gated_silent_frames = 0
        self._pre_roll_frames = 0

    @property
    def vad_name(self) -> str:
        return self._vad.name

    def process(self, frame: VadFrame) -> VadGateDecision:
        probability = self._vad.speech_probability(frame.pcm)
        self._received_frames += 1
        is_speech = probability >= self._speech_threshold

        if not self._speech_active:
            if not is_speech:
                if self._pre_roll.maxlen and probability >= self._pre_roll_threshold:
                    self._pre_roll.append(frame)
                else:
                    self._gated_silent_frames += 1 + len(self._pre_roll)
                    self._pre_roll.clear()
                return VadGateDecision(speech_probability=probability)

            buffered = tuple(self._pre_roll)
            self._pre_roll.clear()
            forward = (*buffered, frame)
            self._speech_active = True
            self._last_speech_frame = frame
            self._silence_frames = 0
            self._forwarded_frames += len(forward)
            self._pre_roll_frames += len(buffered)
            return VadGateDecision(
                forward_frames=forward,
                speech_started=True,
                speech_probability=probability,
            )

        if is_speech:
            resumed = self._silence_frames > 0
            self._silence_frames = 0
            self._last_speech_frame = frame
            self._forwarded_frames += 1
            return VadGateDecision(
                forward_frames=(frame,),
                speech_resumed=resumed,
                speech_probability=probability,
            )

        self._gated_silent_frames += 1
        self._silence_frames += 1
        silence_started = self._silence_frames == 1
        if self._silence_frames < self._end_silence_frames:
            return VadGateDecision(
                silence_started=silence_started,
                speech_probability=probability,
            )

        last_speech = self._last_speech_frame
        stats = self._stats()
        self._reset_turn()
        return VadGateDecision(
            endpoint_fired=True,
            last_speech_frame=last_speech,
            completed_stats=stats,
            speech_probability=probability,
        )

    def reset(self) -> None:
        self._reset_turn()

    def _stats(self) -> VadGateStats:
        return VadGateStats(
            received_frames=self._received_frames,
            forwarded_frames=self._forwarded_frames,
            gated_silent_frames=self._gated_silent_frames,
            pre_roll_frames=self._pre_roll_frames,
        )

    def _reset_turn(self) -> None:
        self._speech_active = False
        self._silence_frames = 0
        self._last_speech_frame = None
        self._pre_roll.clear()
        self._received_frames = 0
        self._forwarded_frames = 0
        self._gated_silent_frames = 0
        self._pre_roll_frames = 0
        self._vad.reset()
