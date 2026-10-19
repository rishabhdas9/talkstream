from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Clock(Protocol):
    def utc_now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class RuntimePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False, strict=True)

    profile: Literal["owner_hosted", "limited_vps"] = "owner_hosted"
    version: str = Field(default="v1", min_length=1, max_length=64)
    session_seconds: int = Field(default=120, gt=0, le=120)
    user_active_calls: int = Field(default=1, ge=1, le=1)
    global_active_calls: int = Field(default=2, ge=1, le=2)
    user_calls_per_day: int = Field(default=3, ge=1, le=3)
    session_micro_inr: int = Field(default=15_000_000, gt=0, le=15_000_000)
    user_day_micro_inr: int = Field(default=30_000_000, gt=0, le=30_000_000)
    reserve_micro_inr: int = Field(default=500_000_000, ge=500_000_000, le=500_000_000)

    initialization_seconds: float = Field(default=15, gt=0, le=15)
    stt_final_seconds: float = Field(default=5, gt=0, le=5)
    first_answer_seconds: float = Field(default=10, gt=0, le=10)
    tts_progress_seconds: float = Field(default=5, gt=0, le=5)
    # Sarvam speech streams at about real time, so 20 s cut answers longer than ~15 s.
    turn_work_seconds: float = Field(default=60, gt=0, le=60)
    router_seconds: float = Field(default=0.3, gt=0, le=0.3)
    max_provider_failures: int = Field(default=2, ge=1, le=2)
    max_readonly_retries: int = Field(default=1, ge=0, le=1)

    input_tokens: int = Field(default=8000, gt=0, le=8000)
    output_tokens: int = Field(default=512, gt=0, le=512)
    spoken_characters: int = Field(default=1000, gt=0, le=1000)
    transcript_characters: int = Field(default=4000, gt=0, le=4000)
    tool_calls_per_turn: int = Field(default=3, ge=1, le=3)
    searches_per_turn: int = Field(default=1, ge=0, le=1)

    otp_cooldown_seconds: int = Field(default=60, ge=60)
    otp_email_per_hour: int = Field(default=3, ge=1, le=3)
    otp_ip_per_hour: int = Field(default=30, ge=1, le=30)
    otp_global_per_day: int = Field(default=80, ge=1, le=80)
    otp_ttl_seconds: int = Field(default=600, ge=1, le=600)
    otp_max_attempts: int = Field(default=5, ge=1, le=5)
    access_ttl_seconds: int = Field(default=86400, ge=1, le=86400)
    ticket_ttl_seconds: int = Field(default=30, ge=1, le=30)

    upload_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=5 * 1024 * 1024)
    pdf_pages: int = Field(default=50, ge=1, le=50)
    source_characters: int = Field(default=100_000, ge=1, le=100_000)
    sources_per_user: int = Field(default=10, ge=1, le=10)
    uploads_per_user_day: int = Field(default=2, ge=1, le=2)
    stored_chunks: int = Field(default=2500, ge=1, le=2500)
    extracted_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)
    expired_otp_retention_days: int = Field(default=1, ge=1)
    diagnostic_retention_days: int = Field(default=30, ge=1, le=30)
    backup_retention_days: int = Field(default=7, ge=1, le=7)

    @property
    def variable_month_micro_inr(self) -> int:
        return 1_000_000_000 if self.profile == "owner_hosted" else 500_000_000

    @property
    def global_day_micro_inr(self) -> int:
        return 30_000_000 if self.profile == "owner_hosted" else 15_000_000

    @property
    def hosting_month_micro_inr(self) -> int:
        return 500_000_000 if self.profile == "owner_hosted" else 1_000_000_000
