# Silero VAD model asset

`silero_vad.onnx` is the ONNX model distributed in the official
`silero-vad==6.2.1` Python wheel.

- Upstream: https://github.com/snakers4/silero-vad
- License: MIT
- SHA-256: `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`
- Runtime input: mono float32 audio, 512 samples per frame at 16 kHz, plus the
  model's recurrent state and 64-sample context.
