"""The one place a speech provider setting picks its metered client, adapter and key."""

from __future__ import annotations

from pydantic import SecretStr

from app.config import Settings
from app.voice.providers.cartesia import CartesiaTtsClient
from app.voice.providers.deepgram import DeepgramRecognizer, DeepgramSttClient
from app.voice.providers.groq_stt import GroqRecognizer, GroqSttClient
from app.voice.providers.sarvam import SarvamRecognizer
from app.voice.providers.sarvam_realtime import SarvamRealtimeClient, SarvamRealtimeRecognizer
from app.voice.stt import SarvamSttClient
from app.voice.tts import SarvamTtsClient

RECOGNITION = {
    "sarvam_legacy": (SarvamSttClient, SarvamRecognizer),
    "sarvam_realtime": (SarvamRealtimeClient, SarvamRealtimeRecognizer),
    "deepgram": (DeepgramSttClient, DeepgramRecognizer),
    "groq": (GroqSttClient, GroqRecognizer),
}


def recognition_key(settings: Settings, transport: str) -> SecretStr | None:
    keys = {"deepgram": settings.deepgram_api_key, "groq": settings.groq_api_key}
    return keys.get(transport, settings.sarvam_api_key)


SYNTHESIS = {"sarvam": SarvamTtsClient, "cartesia": CartesiaTtsClient}


def synthesis_key(settings: Settings) -> SecretStr | None:
    return (
        settings.cartesia_api_key
        if settings.tts_provider == "cartesia"
        else settings.sarvam_api_key
    )
