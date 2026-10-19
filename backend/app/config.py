from __future__ import annotations

import os
import uuid
from functools import cached_property
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import EmailStr, Field, SecretStr, TypeAdapter, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.knowledge.crypto import CredentialCipher, CredentialCipherError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "production", "public", "staging"] = "development"
    dev_login: bool = False
    runtime_profile: Literal["owner_hosted", "limited_vps"] = "owner_hosted"
    backend_workers: int = Field(default=1, ge=1, le=1)
    live_evaluation_cap_micro_inr: int = Field(default=0, ge=0)
    public_origin: str | None = None
    admin_emails: str = ""
    credential_encryption_key: SecretStr | None = None
    backup_encryption_key: SecretStr | None = None
    # Deletion tombstones on an owner-controlled location outside database backups.
    recovery_journal_file: Path | None = None
    policy_file: Path | None = None
    content_policy_file: Path | None = None
    rate_card_file: Path | None = None
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    sarvam_api_key: SecretStr | None = None
    sarvam_chat_base_url: str = "https://api.sarvam.ai/v1"
    exa_api_key: SecretStr | None = None
    deepgram_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    cartesia_api_key: SecretStr | None = None
    exa_search_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    client_tool_timeout_seconds: float = Field(default=12.0, gt=0, le=120)
    resend_api_key: SecretStr | None = None
    resend_from: str = "Talkstream <onboarding@resend.dev>"

    knowledge_rag_enabled: bool = False
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    openrouter_embedding_provider: str | None = None
    openrouter_embedding_data_collection: Literal["deny"] | None = None
    qdrant_url: str = "http://127.0.0.1:6333"
    knowledge_max_sources_per_user: int = Field(default=50, ge=1, le=1000)
    knowledge_max_source_characters: int = Field(default=500_000, ge=1)
    knowledge_max_upload_bytes: int = Field(default=20_000_000, ge=1)
    knowledge_max_query_characters: int = Field(default=1000, ge=1, le=10_000)
    knowledge_chunk_characters: int = Field(default=1200, ge=100, le=10_000)
    knowledge_chunk_overlap: int = Field(default=150, ge=0, le=2000)
    knowledge_embedding_batch_size: int = Field(default=32, ge=1, le=256)
    knowledge_retrieval_mode: Literal["vector", "hybrid"] = "vector"

    jwt_secret: SecretStr = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_seconds: int = Field(default=86_400, gt=0)
    jwt_issuer: str = "talkstream"
    jwt_audience: str = "talkstream-api"
    otp_ttl_seconds: int = Field(default=600, gt=0)
    otp_max_attempts: int = Field(default=5, ge=1, le=20)

    database_url: str = "sqlite+aiosqlite:///./voiceagent.db"
    database_echo: bool = False
    price_per_minute_paise: int = Field(default=200, gt=0)
    low_balance_warn_paise: int = Field(default=100, ge=0)
    min_voice_balance_paise: int = Field(default=1, ge=1)
    billing_tick_seconds: int = Field(default=1, ge=1, le=30)
    max_recharge_paise: int = Field(default=1_000_000, gt=0)

    vad_end_silence_ms: int = Field(default=650, gt=0)
    vad_barge_in_ms: int = Field(default=300, gt=0)
    vad_pre_roll_ms: int = Field(default=160, ge=0, le=1000)
    vad_speech_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    vad_pre_roll_threshold: float = Field(default=0.2, ge=0.0, le=1.0)
    vad_energy_threshold: float = Field(default=500.0, gt=0)
    vad_provider: Literal["silero", "energy"] = "silero"
    vad_model_path: str | None = None
    endpointing_strategy: Literal["local_vad", "sarvam"] = "local_vad"
    metrics_hud: bool = True
    phrase_chunking_enabled: bool = True
    response_cues_enabled: bool = False
    response_cue_delay_ms: int = Field(default=350, ge=0, le=5_000)
    response_cue_cooldown_turns: int = Field(default=1, ge=0, le=20)
    llm_model: str = "sarvam-105b"
    llm_reasoning_effort: str | None = None
    stt_transport: Literal["sarvam_legacy", "sarvam_realtime", "deepgram", "groq"] = "sarvam_legacy"
    stt_model: str = "saaras:v3"
    stt_language_code: str = "unknown"
    stt_input_audio_codec: str = "pcm_s16le"
    stt_audio_encoding: str = "audio/wav"
    tts_provider: Literal["sarvam", "cartesia"] = "sarvam"
    tts_model: str = "bulbul:v4-flash"
    tts_sample_rate_hz: int = Field(default=22_050, gt=0)
    tts_speaker: str = "roopa_en_conversational"
    tts_hindi_speaker: str = "roopa_hi_conversational"
    tts_language_code: str = "en-IN"
    tts_pace: float = Field(default=1.05, ge=0.5, le=2.0)
    tts_output_audio_codec: str = "linear16"

    def tts_speaker_for_language(self, language_code: str) -> str:
        if (
            self.tts_provider == "sarvam"
            and self.tts_model == "bulbul:v4-flash"
            and language_code == "hi-IN"
        ):
            return self.tts_hindi_speaker
        return self.tts_speaker

    @model_validator(mode="after")
    def flash_synthesis_needs_a_persona(self) -> Settings:
        # bulbul:v4-flash rejects v3 speaker names; its personas are <name>_<language>_<style>.
        if self.tts_model == "bulbul:v4-flash" and (
            self.tts_speaker.count("_") < 2 or self.tts_hindi_speaker.count("_") < 2
        ):
            raise ValueError(
                "bulbul:v4-flash needs persona speakers such as roopa_en_conversational"
            )
        if self.tts_provider == "cartesia":
            from app.voice.providers.cartesia import MODELS

            try:
                uuid.UUID(self.tts_speaker)
            except ValueError:
                raise ValueError("Cartesia needs a pinned Sonic model and a voice ID") from None
            if self.tts_model not in MODELS or self.tts_output_audio_codec != "linear16":
                raise ValueError("Cartesia needs a pinned Sonic model and a voice ID")
        return self

    @model_validator(mode="after")
    def realtime_configuration_is_explicit(self) -> Settings:
        if self.stt_transport != "sarvam_legacy":
            from app.voice.providers import deepgram, groq_stt, sarvam_realtime

            # Streaming transports endpoint at the provider; Groq transcribes local utterances.
            module, strategy = {
                "sarvam_realtime": (sarvam_realtime, "sarvam"),
                "deepgram": (deepgram, "sarvam"),
                "groq": (groq_stt, "local_vad"),
            }[self.stt_transport]
            if (
                self.stt_model not in module.MODELS
                or self.stt_language_code not in module.LANGUAGES
                or self.endpointing_strategy != strategy
            ):
                raise ValueError(
                    "This STT transport needs a supported model, language and its endpointing"
                )
        return self

    @property
    def dev_login_enabled(self) -> bool:
        # Explicit opt-in, and only where the public checks below are skipped.
        return self.dev_login and self.app_env in {"development", "test"}

    @model_validator(mode="after")
    def public_configuration_is_explicit(self) -> Settings:
        if self.app_env in {"development", "test"}:
            return self
        try:
            admins = TypeAdapter(list[EmailStr]).validate_python(
                [email.strip() for email in self.admin_emails.split(",") if email.strip()]
            )
        except ValueError:
            admins = []

        def valid_key(key: SecretStr | None) -> bool:
            return key is not None and len(key.get_secret_value()) >= 32

        try:
            CredentialCipher(
                self.credential_encryption_key.get_secret_value()
                if self.credential_encryption_key is not None
                else None
            )
            credential_key_valid = True
        except CredentialCipherError:
            credential_key_valid = False

        required = {
            "delivered email": self.resend_api_key is not None
            and "@resend.dev" not in self.resend_from,
            "administrator identity": bool(admins),
            "encryption key": credential_key_valid,
            "backup encryption key": valid_key(self.backup_encryption_key),
            "recovery journal": self.recovery_journal_file is not None
            and self.recovery_journal_file.parent.is_dir(),
            "policy file": self.policy_file is not None and self.policy_file.is_file(),
            "rate card": self.rate_card_file is not None and self.rate_card_file.is_file(),
        }
        origin = urlsplit(self.public_origin or "")
        required["HTTPS origin"] = (
            origin.scheme == "https"
            and bool(origin.netloc)
            and origin.username is None
            and not origin.query
            and not origin.fragment
            and origin.path in {"", "/"}
            and self.cors_origin_list == [(self.public_origin or "").rstrip("/")]
        )
        secret = self.jwt_secret.get_secret_value()
        required["new JWT secret"] = (
            len(secret) >= 32
            and len(set(secret)) >= 8
            and not any(marker in secret.casefold() for marker in ("replace", "test-secret"))
        )
        missing = [name for name, valid in required.items() if not valid]
        if missing:
            raise ValueError("Missing valid public configuration: " + ", ".join(missing))
        return self

    @field_validator(
        "sarvam_api_key",
        "exa_api_key",
        "deepgram_api_key",
        "groq_api_key",
        "cartesia_api_key",
        "resend_api_key",
        "openai_api_key",
        "gemini_api_key",
        "openrouter_api_key",
        "openrouter_embedding_provider",
        "openrouter_embedding_data_collection",
        "credential_encryption_key",
        "backup_encryption_key",
        "policy_file",
        "content_policy_file",
        "rate_card_file",
        "public_origin",
        "llm_reasoning_effort",
        "vad_model_path",
        mode="before",
    )
    @classmethod
    def empty_string_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("jwt_algorithm")
    @classmethod
    def only_hs256_is_supported(cls, value: str) -> str:
        if value != "HS256":
            raise ValueError("only HS256 is supported")
        return value

    @model_validator(mode="before")
    @classmethod
    def load_runtime_secret_files(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        resolved = dict(values)
        for field_name, env_name in (
            ("jwt_secret", "JWT_SECRET"),
            ("sarvam_api_key", "SARVAM_API_KEY"),
            ("exa_api_key", "EXA_API_KEY"),
            ("deepgram_api_key", "DEEPGRAM_API_KEY"),
            ("groq_api_key", "GROQ_API_KEY"),
            ("cartesia_api_key", "CARTESIA_API_KEY"),
            ("resend_api_key", "RESEND_API_KEY"),
            ("openai_api_key", "OPENAI_API_KEY"),
            ("gemini_api_key", "GEMINI_API_KEY"),
            ("openrouter_api_key", "OPENROUTER_API_KEY"),
            ("credential_encryption_key", "CREDENTIAL_ENCRYPTION_KEY"),
            ("backup_encryption_key", "BACKUP_ENCRYPTION_KEY"),
        ):
            if resolved.get(field_name) not in (None, ""):
                continue
            secret_path = os.getenv(f"{env_name}_FILE")
            if not secret_path:
                continue
            resolved[field_name] = Path(secret_path).read_text(encoding="utf-8").strip()
        return resolved

    @cached_property
    def jwt_secret_value(self) -> str:
        return self.jwt_secret.get_secret_value()

    @cached_property
    def openai_api_key_value(self) -> str | None:
        if self.openai_api_key is None:
            return None
        if isinstance(self.openai_api_key, SecretStr):
            return self.openai_api_key.get_secret_value()
        return str(self.openai_api_key)

    @cached_property
    def gemini_api_key_value(self) -> str | None:
        if self.gemini_api_key is None:
            return None
        if isinstance(self.gemini_api_key, SecretStr):
            return self.gemini_api_key.get_secret_value()
        return str(self.gemini_api_key)

    @cached_property
    def openrouter_api_key_value(self) -> str | None:
        if self.openrouter_api_key is None:
            return None
        if isinstance(self.openrouter_api_key, SecretStr):
            return self.openrouter_api_key.get_secret_value()
        return str(self.openrouter_api_key)

    @cached_property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
