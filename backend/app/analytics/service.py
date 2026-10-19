from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.schemas import (
    AnalyticsBucket,
    AnalyticsResponse,
    AnalyticsTotals,
    AnalyticsWindow,
    LatencyRollup,
    SessionDetailResponse,
    SessionSummary,
    SessionTurn,
    StagePercentiles,
)
from app.models import TurnMetric, UsageSession
from app.voice.metrics import percentile

_WINDOWS: dict[AnalyticsWindow, tuple[timedelta, timedelta]] = {
    "hour": (timedelta(hours=1), timedelta(minutes=5)),
    "day": (timedelta(days=1), timedelta(hours=1)),
    "week": (timedelta(days=7), timedelta(days=1)),
}

# p99 is withheld below this sample count (requirement §3a: never quoted
# from fewer than 100 valid turns).
_P99_MIN_SAMPLES = 100

_SESSION_LIST_LIMIT = 50


class AnalyticsError(RuntimeError):
    pass


class SessionNotFoundError(AnalyticsError):
    pass


def _stage_values(metric: TurnMetric) -> dict[str, float | None]:
    stt_ms = (
        metric.stt_flush_to_final_ms
        if metric.stt_flush_to_final_ms is not None
        else metric.stt_eot_ms
    )
    first_speakable_ms: float | None = None
    if metric.llm_first_speakable_ms is not None and metric.llm_visible_ttft_ms is not None:
        first_speakable_ms = metric.llm_first_speakable_ms - metric.llm_visible_ttft_ms
    return {
        "endpoint_window_ms": metric.endpoint_decision_ms,
        "stt_ms": stt_ms,
        "llm_ttft_ms": metric.llm_visible_ttft_ms,
        "first_speakable_ms": first_speakable_ms,
        # Same derived first-text wait as the live waterfall; socket setup is reported separately.
        "tts_connection_wait_ms": (metric.dimensions or {}).get("tts_connection_wait_ms"),
        "tts_socket_acquire_ms": metric.tts_connection_acquire_ms,
        "tts_ttfb_ms": metric.tts_ttfb_ms,
        "transport_playback_ms": metric.downstream_to_playback_ms,
        "e2e_voice_to_voice_ms": metric.e2e_voice_to_voice_ms,
    }


class AnalyticsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # user_id=None reads every user's calls (administrator view).
    async def overview(self, user_id: str | None, window: AnalyticsWindow) -> AnalyticsResponse:
        span, bucket_size = _WINDOWS[window]
        now = datetime.now(UTC)
        window_start = self._align(now - span, bucket_size)

        sessions = await self._sessions_in_window(user_id, window_start)
        metrics = await self._metrics_in_window(user_id, window_start)

        turns_by_session: dict[str, list[TurnMetric]] = {}
        for metric in metrics:
            turns_by_session.setdefault(metric.usage_session_id, []).append(metric)

        summaries = [self._summarize(item, turns_by_session.get(item.id, [])) for item in sessions]

        valid = [m for m in metrics if not m.censored and m.exclusion_reason is None]
        excluded = len(metrics) - len(valid)

        return AnalyticsResponse(
            window=window,
            window_start=window_start,
            bucket_seconds=int(bucket_size.total_seconds()),
            totals=AnalyticsTotals(
                sessions=len(sessions),
                users=len({item.user_id for item in sessions}),
                billed_seconds=sum(item.billed_seconds for item in sessions),
                cost_paise=sum(item.cost_paise for item in sessions),
                avg_session_seconds=(
                    sum(item.billed_seconds for item in sessions) / len(sessions)
                    if sessions
                    else None
                ),
                interrupted_turns=sum(1 for m in metrics if m.interrupted),
                total_turns=len(metrics),
            ),
            buckets=self._bucketize(sessions, window_start, bucket_size, now),
            sessions=summaries[:_SESSION_LIST_LIMIT],
            latency=self._rollup(valid, excluded),
        )

    async def session_detail(self, user_id: str | None, session_id: str) -> SessionDetailResponse:
        result = await self._session.execute(
            self._owned(select(UsageSession).where(UsageSession.id == session_id), user_id)
        )
        usage = result.scalar_one_or_none()
        if usage is None:
            raise SessionNotFoundError(session_id)
        metrics_result = await self._session.execute(
            select(TurnMetric)
            .where(TurnMetric.usage_session_id == session_id)
            .order_by(TurnMetric.turn_index)
        )
        metrics = list(metrics_result.scalars())
        return SessionDetailResponse(
            session=self._summarize(usage, metrics),
            turns=[
                SessionTurn(
                    turn_index=metric.turn_index,
                    created_at=metric.created_at,
                    language_code=metric.language_code,
                    interrupted=metric.interrupted,
                    tool_names=metric.tool_names or [],
                    e2e_voice_to_voice_ms=metric.e2e_voice_to_voice_ms,
                    stages=_stage_values(metric),
                    barge_in_stop_ack_ms=metric.barge_in_stop_ack_ms,
                )
                for metric in metrics
            ],
        )

    @staticmethod
    def _owned(query, user_id: str | None):
        return query if user_id is None else query.where(UsageSession.user_id == user_id)

    async def _sessions_in_window(
        self, user_id: str | None, window_start: datetime
    ) -> list[UsageSession]:
        result = await self._session.execute(
            self._owned(
                select(UsageSession)
                .where(UsageSession.started_at >= window_start)
                .order_by(UsageSession.started_at.desc()),
                user_id,
            )
        )
        return list(result.scalars())

    async def _metrics_in_window(
        self, user_id: str | None, window_start: datetime
    ) -> list[TurnMetric]:
        result = await self._session.execute(
            self._owned(
                select(TurnMetric)
                .join(UsageSession, TurnMetric.usage_session_id == UsageSession.id)
                .where(UsageSession.started_at >= window_start),
                user_id,
            )
        )
        return list(result.scalars())

    @staticmethod
    def _align(moment: datetime, bucket_size: timedelta) -> datetime:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        bucket_seconds = int(bucket_size.total_seconds())
        aligned = int((moment - epoch).total_seconds()) // bucket_seconds * bucket_seconds
        return epoch + timedelta(seconds=aligned)

    @staticmethod
    def _summarize(usage: UsageSession, metrics: list[TurnMetric]) -> SessionSummary:
        languages = sorted({metric.language_code for metric in metrics if metric.language_code})
        return SessionSummary(
            id=usage.id,
            started_at=usage.started_at,
            ended_at=usage.ended_at,
            billed_seconds=usage.billed_seconds,
            cost_paise=usage.cost_paise,
            end_reason=usage.end_reason,
            languages=languages,
            turns=len(metrics),
            interrupted_turns=sum(1 for metric in metrics if metric.interrupted),
        )

    def _bucketize(
        self,
        sessions: list[UsageSession],
        window_start: datetime,
        bucket_size: timedelta,
        now: datetime,
    ) -> list[AnalyticsBucket]:
        buckets: list[AnalyticsBucket] = []
        cursor = window_start
        while cursor <= now:
            buckets.append(
                AnalyticsBucket(start=cursor, billed_seconds=0, cost_paise=0, sessions=0)
            )
            cursor += bucket_size
        for item in sessions:
            started_at = (
                item.started_at
                if item.started_at.tzinfo is not None
                else item.started_at.replace(tzinfo=UTC)
            )
            index = int((started_at - window_start) // bucket_size)
            if 0 <= index < len(buckets):
                bucket = buckets[index]
                buckets[index] = bucket.model_copy(
                    update={
                        "billed_seconds": bucket.billed_seconds + item.billed_seconds,
                        "cost_paise": bucket.cost_paise + item.cost_paise,
                        "sessions": bucket.sessions + 1,
                    }
                )
        return buckets

    @staticmethod
    def _rollup(valid: list[TurnMetric], excluded: int) -> LatencyRollup:
        stage_keys = [
            "endpoint_window_ms",
            "stt_ms",
            "llm_ttft_ms",
            "first_speakable_ms",
            "tts_connection_wait_ms",
            "tts_socket_acquire_ms",
            "tts_ttfb_ms",
            "transport_playback_ms",
            "e2e_voice_to_voice_ms",
        ]
        samples: dict[str, list[float]] = {key: [] for key in stage_keys}
        barge_ack: list[float] = []
        for metric in valid:
            for key, value in _stage_values(metric).items():
                if value is not None:
                    samples[key].append(value)
            if metric.barge_in_stop_ack_ms is not None:
                barge_ack.append(metric.barge_in_stop_ack_ms)
        samples["barge_in_stop_ack_ms"] = barge_ack

        stages = [
            StagePercentiles(
                key=key,
                count=len(values),
                p50=percentile(values, 50),
                p95=percentile(values, 95),
                p99=percentile(values, 99) if len(values) >= _P99_MIN_SAMPLES else None,
            )
            for key, values in samples.items()
        ]
        return LatencyRollup(stages=stages, valid_turns=len(valid), excluded_turns=excluded)
