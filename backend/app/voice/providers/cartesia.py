"""Opt-in Cartesia Sonic synthesis; the metered Bulbul client owns reservation and discard.

One client serves one spoken generation, so its connection ID is the Cartesia context ID and
the turn's recorded `tts_connection_id` maps the context to the application's generation.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from app.voice.tts import SarvamTtsClient, SarvamTtsError

API_VERSION = "2026-08-14"
MODELS = frozenset({"sonic-3.6"})


class CartesiaTtsClient(SarvamTtsClient):
    provider = "cartesia"

    def _endpoint(self) -> tuple[str, dict[str, str]]:
        if self._config.model not in MODELS or self._config.output_audio_codec != "linear16":
            raise SarvamTtsError("Cartesia needs a pinned Sonic model and linear16 output")
        query = urlencode({"cartesia_version": API_VERSION})
        return f"wss://api.cartesia.ai/tts/websocket?{query}", {"X-API-Key": self._config.api_key}

    def _request(self) -> dict[str, Any]:
        return {
            "model_id": self._config.model,
            "voice": {"id": self._config.speaker},
            # Cartesia takes the bare language (en, hi) for the app's en-IN/hi-IN codes.
            "language": self._config.target_language_code.split("-")[0],
            "context_id": self.connection_id,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self._config.sample_rate_hz,
            },
        }

    def _opening_messages(self) -> list[dict[str, Any]]:
        return []

    def _text_message(self, text: str) -> dict[str, Any]:
        return self._request() | {"transcript": text, "continue": True}

    def _closing_messages(self) -> list[dict[str, Any]]:
        return [self._request() | {"transcript": "", "continue": False}]

    def _parse_message(self, message: dict[str, Any]) -> tuple[str, Any, dict[str, Any]]:
        if message.get("context_id") not in (None, self.connection_id):
            return "error", None, {"title": "Audio for a different generation"}
        kind = message.get("type")
        if kind == "chunk":
            return "audio", message.get("data"), {"request_id": message.get("request_id")}
        if kind == "done":
            return "final", None, {}
        if kind == "error":
            return "error", None, {key: message.get(key) for key in ("title", "status_code")}
        return "other", None, {}
