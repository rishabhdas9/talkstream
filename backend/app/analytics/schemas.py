from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, field_validator

AnalyticsWindow = Literal["hour", "day", "week"]


class UtcDatetimeModel(BaseModel):
    """SQLite returns naive datetimes; stamp them as UTC so JSON carries the
    offset and browsers render local time correctly."""

    @field_validator("*", mode="after")
    @classmethod
    def _naive_datetimes_are_utc(cls, value: object) -> object:
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class AnalyticsTotals(BaseModel):
    sessions: int
    billed_seconds: int
    cost_paise: int
    avg_session_seconds: float | None
    interrupted_turns: int
    total_turns: int


class AnalyticsBucket(UtcDatetimeModel):
    start: datetime
    billed_seconds: int
    cost_paise: int
    sessions: int


class SessionSummary(UtcDatetimeModel):
    id: str
    started_at: datetime
    ended_at: datetime | None
    billed_seconds: int
    cost_paise: int
    end_reason: str | None
    languages: list[str]
    turns: int
    interrupted_turns: int


class StagePercentiles(BaseModel):
    key: str
    count: int
    p50: float | None
    p95: float | None
    # Never quoted from fewer than 100 valid turns (requirement §3a).
    p99: float | None


class LatencyRollup(BaseModel):
    stages: list[StagePercentiles]
    valid_turns: int
    excluded_turns: int


class AnalyticsResponse(UtcDatetimeModel):
    window: AnalyticsWindow
    window_start: datetime
    bucket_seconds: int
    totals: AnalyticsTotals
    buckets: list[AnalyticsBucket]
    sessions: list[SessionSummary]
    latency: LatencyRollup


class SessionTurn(UtcDatetimeModel):
    turn_index: int
    created_at: datetime
    language_code: str | None
    interrupted: bool
    tool_names: list[str]
    e2e_voice_to_voice_ms: float | None
    stages: dict[str, float | None]
    barge_in_stop_ack_ms: float | None


class SessionDetailResponse(UtcDatetimeModel):
    session: SessionSummary
    turns: list[SessionTurn]
