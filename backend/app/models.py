from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class TransactionKind(StrEnum):
    TOPUP = "topup"
    USAGE = "usage"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    wallet_transactions: Mapped[list[WalletTransaction]] = relationship(back_populates="user")
    usage_sessions: Mapped[list[UsageSession]] = relationship(back_populates="user")


class OtpChallenge(Base):
    __tablename__ = "otp_challenges"
    __table_args__ = (Index("ix_otp_challenges_email_created", "email", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class UsageSession(Base):
    __tablename__ = "usage_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billed_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_reason: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="usage_sessions")
    wallet_transactions: Mapped[list[WalletTransaction]] = relationship(
        back_populates="usage_session"
    )
    turn_metrics: Mapped[list[TurnMetric]] = relationship(back_populates="usage_session")


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[TransactionKind] = mapped_column(
        Enum(
            TransactionKind,
            native_enum=False,
            validate_strings=True,
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
    )
    usage_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("usage_sessions.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    user: Mapped[User] = relationship(back_populates="wallet_transactions")
    usage_session: Mapped[UsageSession | None] = relationship(back_populates="wallet_transactions")


class TurnMetric(Base):
    __tablename__ = "turn_metrics"
    __table_args__ = (
        UniqueConstraint("usage_session_id", "turn_index", name="uq_turn_session_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    usage_session_id: Mapped[str] = mapped_column(ForeignKey("usage_sessions.id"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    # Browser speech anchor and server-side pipeline timestamps.
    last_speech_capture_seq: Mapped[int | None] = mapped_column(BigInteger)
    last_speech_capture_time_ms: Mapped[float | None] = mapped_column(Float)
    t_audio_frame_server_receive: Mapped[float | None] = mapped_column(Float)
    t_speech_start_server: Mapped[float | None] = mapped_column(Float)
    t_last_speech_frame_server: Mapped[float | None] = mapped_column(Float)
    t_endpoint_decision: Mapped[float | None] = mapped_column(Float)
    t_stt_flush_sent: Mapped[float | None] = mapped_column(Float)
    t_stt_final: Mapped[float | None] = mapped_column(Float)
    t_llm_request_start: Mapped[float | None] = mapped_column(Float)
    t_llm_first_reasoning_token: Mapped[float | None] = mapped_column(Float)
    t_llm_first_visible_token: Mapped[float | None] = mapped_column(Float)
    t_llm_first_speakable_chunk: Mapped[float | None] = mapped_column(Float)
    t_llm_complete: Mapped[float | None] = mapped_column(Float)
    t_tts_connection_acquire_start: Mapped[float | None] = mapped_column(Float)
    t_tts_connection_acquire_end: Mapped[float | None] = mapped_column(Float)
    t_tts_text_submitted: Mapped[float | None] = mapped_column(Float)
    t_tts_first_chunk: Mapped[float | None] = mapped_column(Float)
    t_tts_complete: Mapped[float | None] = mapped_column(Float)
    t_audio_sent_server: Mapped[float | None] = mapped_column(Float)

    # Browser-local playback timestamps, sent back through the application WS.
    t_client_audio_received_ms: Mapped[float | None] = mapped_column(Float)
    t_client_decode_complete_ms: Mapped[float | None] = mapped_column(Float)
    t_client_audio_scheduled_ms: Mapped[float | None] = mapped_column(Float)
    t_client_playback_start_ms: Mapped[float | None] = mapped_column(Float)
    t_client_playback_end_ms: Mapped[float | None] = mapped_column(Float)

    # Barge-in timestamps remain separated by clock domain.
    t_barge_speech_onset_server: Mapped[float | None] = mapped_column(Float)
    t_barge_speech_detected_server: Mapped[float | None] = mapped_column(Float)
    t_interrupt_sent_server: Mapped[float | None] = mapped_column(Float)
    t_interrupt_received_client_ms: Mapped[float | None] = mapped_column(Float)
    t_playback_queue_cleared_client_ms: Mapped[float | None] = mapped_column(Float)
    t_interrupt_ack_received_server: Mapped[float | None] = mapped_column(Float)

    # Derived headline metrics. Missing values stay NULL, never zero-filled.
    upstream_audio_transport_ms: Mapped[float | None] = mapped_column(Float)
    endpoint_decision_ms: Mapped[float | None] = mapped_column(Float)
    stt_flush_to_final_ms: Mapped[float | None] = mapped_column(Float)
    stt_eot_ms: Mapped[float | None] = mapped_column(Float)
    orchestrator_queue_ms: Mapped[float | None] = mapped_column(Float)
    llm_visible_ttft_ms: Mapped[float | None] = mapped_column(Float)
    llm_first_speakable_ms: Mapped[float | None] = mapped_column(Float)
    tts_ttfb_ms: Mapped[float | None] = mapped_column(Float)
    tts_connection_acquire_ms: Mapped[float | None] = mapped_column(Float)
    client_decode_ms: Mapped[float | None] = mapped_column(Float)
    client_schedule_ms: Mapped[float | None] = mapped_column(Float)
    downstream_to_playback_ms: Mapped[float | None] = mapped_column(Float)
    e2e_voice_to_voice_ms: Mapped[float | None] = mapped_column(Float, index=True)
    barge_detection_ms: Mapped[float | None] = mapped_column(Float)
    barge_client_flush_ms: Mapped[float | None] = mapped_column(Float)
    barge_in_stop_ack_ms: Mapped[float | None] = mapped_column(Float)
    audio_queue_depth_ms_at_first_playback: Mapped[float | None] = mapped_column(Float)
    realtime_factor: Mapped[float | None] = mapped_column(Float)
    interrupted_audio_generated_ms: Mapped[float | None] = mapped_column(Float)
    interrupted_audio_played_ms: Mapped[float | None] = mapped_column(Float)

    # Turn outcomes and data-quality state.
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    turn_false_endpoint: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    turn_false_continuation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    balance_cutoff: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    censored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    exclusion_reason: Mapped[str | None] = mapped_column(String(128))

    # Queryable dimensions plus JSON for spans/provider extensions.
    stt_model: Mapped[str | None] = mapped_column(String(128))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    tts_model: Mapped[str | None] = mapped_column(String(128))
    llm_reasoning_effort: Mapped[str | None] = mapped_column(String(64))
    language_code: Mapped[str | None] = mapped_column(String(32), index=True)
    language_confidence: Mapped[float | None] = mapped_column(Float)
    audio_codec: Mapped[str | None] = mapped_column(String(64))
    input_sample_rate_hz: Mapped[int | None] = mapped_column(Integer)
    output_sample_rate_hz: Mapped[int | None] = mapped_column(Integer)
    frame_size_ms: Mapped[float | None] = mapped_column(Float)
    input_duration_ms: Mapped[float | None] = mapped_column(Float)
    provider_region: Mapped[str | None] = mapped_column(String(64))
    application_region: Mapped[str | None] = mapped_column(String(64))
    tts_connection_state: Mapped[str | None] = mapped_column(String(32))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    close_reason: Mapped[str | None] = mapped_column(String(128))
    stt_request_id: Mapped[str | None] = mapped_column(String(128))
    llm_request_id: Mapped[str | None] = mapped_column(String(128))
    tts_request_id: Mapped[str | None] = mapped_column(String(128))
    tool_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tool_names: Mapped[list[str] | None] = mapped_column(JSON)
    software_version: Mapped[str | None] = mapped_column(String(64))
    configuration_hash: Mapped[str | None] = mapped_column(String(128))
    replay_corpus_id: Mapped[str | None] = mapped_column(String(128))
    tool_spans: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    missing_timestamps: Mapped[list[str] | None] = mapped_column(JSON)
    dimensions: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    raw_provider_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)

    usage_session: Mapped[UsageSession] = relationship(back_populates="turn_metrics")
