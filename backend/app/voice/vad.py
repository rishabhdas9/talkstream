from __future__ import annotations

import math
import sys
from array import array
from pathlib import Path
from typing import Protocol

FRAME_SAMPLES = 512
SAMPLE_RATE_HZ = 16_000


class VadInitializationError(RuntimeError):
    pass


class IVad(Protocol):
    name: str

    def speech_probability(self, pcm: bytes) -> float: ...

    def reset(self) -> None: ...


class EnergyVad:
    name = "energy"

    def __init__(self, *, rms_threshold: float = 500.0) -> None:
        self._rms_threshold = rms_threshold

    def speech_probability(self, pcm: bytes) -> float:
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        if len(samples) != FRAME_SAMPLES:
            raise ValueError(f"VAD frames must contain exactly {FRAME_SAMPLES} samples")
        rms = math.sqrt(sum(float(sample) ** 2 for sample in samples) / len(samples))
        return min(1.0, 0.5 * rms / self._rms_threshold)

    def reset(self) -> None:
        return None


class SileroOnnxVad:
    name = "silero_onnx"

    def __init__(self, model_path: Path) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as exc:
            raise VadInitializationError("Silero requires numpy and onnxruntime") from exc
        if not model_path.is_file():
            raise VadInitializationError(f"Silero ONNX model not found: {model_path}")
        self._np = np
        try:
            self._session = ort.InferenceSession(
                str(model_path),
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise VadInitializationError(f"Silero ONNX model failed to load: {exc}") from exc
        self.reset()

    def speech_probability(self, pcm: bytes) -> float:
        np = self._np
        samples = np.frombuffer(pcm, dtype="<i2")
        if samples.size != FRAME_SAMPLES:
            raise ValueError(f"VAD frames must contain exactly {FRAME_SAMPLES} samples")
        normalized = samples.astype(np.float32).reshape(1, -1) / 32768.0
        model_input = np.concatenate((self._context, normalized), axis=1)
        output, state = self._session.run(
            None,
            {
                "input": model_input,
                "state": self._state,
                "sr": np.array(SAMPLE_RATE_HZ, dtype=np.int64),
            },
        )
        self._state = state
        self._context = model_input[:, -64:]
        return float(output.reshape(-1)[0])

    def reset(self) -> None:
        np = self._np
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 64), dtype=np.float32)


def default_silero_model_path() -> Path:
    return Path(__file__).with_name("assets") / "silero_vad.onnx"
