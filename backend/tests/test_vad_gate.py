from __future__ import annotations

from collections import deque

import pytest

from app.voice.gate import BargeInDetector, VadFrame, VadGate
from app.voice.vad import FRAME_SAMPLES


class ScriptedVad:
    name = "scripted"

    def __init__(self, probabilities: list[float]) -> None:
        self.probabilities = deque(probabilities)
        self.reset_count = 0

    def speech_probability(self, pcm: bytes) -> float:
        assert len(pcm) == FRAME_SAMPLES * 2
        return self.probabilities.popleft()

    def reset(self) -> None:
        self.reset_count += 1


def frame(index: int) -> VadFrame:
    return VadFrame(
        capture_seq=index,
        capture_time_ms=index * 32.0,
        server_received_at=index * 0.032,
        pcm=b"\0\0" * FRAME_SAMPLES,
    )


def test_shared_gate_builder_uses_configured_energy_threshold(settings):
    from app.voice.gate import build_vad_gate

    settings.vad_provider = "energy"
    settings.vad_energy_threshold = 2000
    subject = build_vad_gate(settings)
    pcm = b"\xe8\x03" * FRAME_SAMPLES  # RMS 1000, below the configured speech threshold.
    assert subject.vad_name == "energy"
    assert not subject.process(VadFrame(0, 0, 0, pcm)).forward_frames


def test_shared_gate_builder_preserves_silero_fallback_and_private_logging(
    settings, monkeypatch, caplog
):
    import logging

    from app.voice.gate import build_vad_gate
    from app.voice.vad import VadInitializationError

    settings.vad_provider = "silero"

    def unavailable(path):
        raise VadInitializationError("private-model-path")

    monkeypatch.setattr(logging.getLogger("talkstream"), "propagate", True)
    monkeypatch.setattr("app.voice.gate.SileroOnnxVad", unavailable)
    with caplog.at_level(logging.WARNING, logger="talkstream.voice"):
        subject = build_vad_gate(settings, session_id="fixture-session")
    assert subject.vad_name == "energy"
    assert "vad_fallback" in caplog.text
    assert "private-model-path" not in caplog.text


def gate(probabilities: list[float], *, end_silence_ms: int = 500) -> VadGate:
    return VadGate(
        ScriptedVad(probabilities),
        end_silence_ms=end_silence_ms,
        pre_roll_ms=64,
        speech_threshold=0.5,
        pre_roll_threshold=0.2,
    )


def test_silence_is_never_forwarded() -> None:
    subject = gate([0.0, 0.1, 0.0])

    decisions = [subject.process(frame(index)) for index in range(3)]

    assert all(not decision.forward_frames for decision in decisions)


def test_speech_onset_includes_pre_roll() -> None:
    subject = gate([0.3, 0.4, 0.9])

    subject.process(frame(0))
    subject.process(frame(1))
    decision = subject.process(frame(2))

    assert decision.speech_started
    assert [item.capture_seq for item in decision.forward_frames] == [0, 1, 2]


def test_endpoint_fires_after_500_ms_trailing_silence_and_resets_counters() -> None:
    silence_frames = 16
    scripted = ScriptedVad([0.9, *([0.0] * silence_frames), 0.9])
    subject = VadGate(
        scripted,
        end_silence_ms=500,
        pre_roll_ms=0,
        speech_threshold=0.5,
        pre_roll_threshold=0.2,
    )

    subject.process(frame(0))
    decisions = [subject.process(frame(index)) for index in range(1, silence_frames + 1)]

    assert not any(item.endpoint_fired for item in decisions[:-1])
    completed = decisions[-1]
    assert completed.endpoint_fired
    assert completed.last_speech_frame == frame(0)
    assert completed.completed_stats is not None
    assert completed.completed_stats.forwarded_frames == 1
    assert completed.completed_stats.gated_silent_frames == silence_frames
    assert subject.process(frame(99)).speech_started


def test_mid_utterance_pause_is_gated_then_speech_resumes() -> None:
    subject = gate([0.9, 0.0, 0.0, 0.9])

    subject.process(frame(0))
    assert not subject.process(frame(1)).forward_frames
    assert not subject.process(frame(2)).forward_frames
    resumed = subject.process(frame(3))

    assert resumed.speech_resumed
    assert resumed.forward_frames == (frame(3),)


def test_pre_roll_threshold_cannot_exceed_speech_threshold() -> None:
    with pytest.raises(ValueError, match="pre-roll"):
        VadGate(
            ScriptedVad([]),
            end_silence_ms=500,
            pre_roll_ms=0,
            speech_threshold=0.4,
            pre_roll_threshold=0.5,
        )


def test_barge_in_requires_sustained_speech() -> None:
    detector = BargeInDetector(sustained_speech_ms=200)

    decisions = [detector.observe_speech(frame(index)) for index in range(8)]

    assert not any(item.detected for item in decisions[:-1])
    assert decisions[-1].detected
    assert decisions[-1].onset_server_time == 0.0


def test_single_noisy_frame_is_rejected_as_barge_in() -> None:
    detector = BargeInDetector(sustained_speech_ms=200)

    candidate = detector.observe_speech(frame(1))
    rejected = detector.observe_silence()

    assert candidate.candidate_started and not candidate.detected
    assert rejected.candidate_cancelled and not rejected.detected
    assert not detector.observe_speech(frame(3)).detected
