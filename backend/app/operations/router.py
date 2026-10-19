from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, select, text

from app.analytics.schemas import AnalyticsResponse, AnalyticsWindow, SessionDetailResponse
from app.analytics.service import AnalyticsService, SessionNotFoundError
from app.auth.dependencies import AdministratorDependency, SessionDependency
from app.models import UsageSession

router = APIRouter(prefix="/operations", tags=["operations"])

# Enforced in shared code on every call; none of these can be switched off at runtime.
ENFORCEMENT_POINTS = [
    {"id": "admission", "description": "Identity, call slots, quotas and reserved spending"},
    {"id": "input", "description": "Transcript length and content policy before agent work"},
    {"id": "tool_proposal", "description": "Allowlisted tools, strict arguments, owned references"},
    {"id": "tool_execution", "description": "Stable call IDs, durable receipts, unknown outcomes"},
    {"id": "retrieval", "description": "Owner and active-generation filters rechecked in SQL"},
    {"id": "output", "description": "Answers checked before speech; claims need receipts"},
]
LIMIT_FIELDS = (
    "session_seconds",
    "user_active_calls",
    "global_active_calls",
    "user_calls_per_day",
    "session_micro_inr",
    "user_day_micro_inr",
    "searches_per_turn",
    "tool_calls_per_turn",
    "uploads_per_user_day",
    "sources_per_user",
    "stored_chunks",
)


def _local_day(now: datetime) -> tuple[str, str, datetime]:
    local = now.astimezone(ZoneInfo("Asia/Kolkata"))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local.strftime("%Y-%m-%d"), local.strftime("%Y-%m"), midnight.astimezone(UTC)


async def _period_spend(session, kind: str, period: str, limit: int) -> dict[str, int]:
    spent, held = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(CASE WHEN period=:period THEN spent_micro_inr ELSE 0 END),0),"
                " COALESCE(SUM(held_micro_inr),0) FROM cost_periods WHERE kind=:kind AND owner='*'"
            ),
            {"kind": kind, "period": period},
        )
    ).one()
    return {"settled_micro_inr": spent, "held_micro_inr": held, "limit_micro_inr": limit}


@router.get("/status")
async def operations_status(
    request: Request, _: AdministratorDependency, session: SessionDependency
) -> dict:
    state = request.app.state
    settings, policy = state.settings, state.runtime_policy
    rates = state.cost_service.rate_card
    sarvam = settings.sarvam_api_key is not None
    components = [
        ("speech_recognition", "sarvam", settings.stt_model, sarvam),
        ("text", "sarvam", settings.llm_model, sarvam),
        ("speech_synthesis", "sarvam", settings.tts_model, sarvam),
        ("web_search", "exa", "fast", settings.exa_api_key is not None),
        (
            "email",
            "resend" if settings.resend_api_key is not None else "console",
            None,
            settings.resend_api_key is not None,
        ),
        ("knowledge", "qdrant", None, settings.knowledge_rag_enabled),
    ]
    now = state.clock.utc_now()
    day, month, day_start = _local_day(now)
    observed = (
        await session.execute(
            text(
                "SELECT provider, model, COUNT(*) AS attempts,"
                " SUM(outcome='succeeded') AS succeeded, SUM(outcome='failed') AS failed,"
                " SUM(outcome='cancelled') AS cancelled, SUM(outcome='unknown') AS unknown,"
                " MAX(created_at) AS last_attempt_at,"
                " MAX(CASE WHEN outcome='succeeded' THEN created_at END) AS last_success_at,"
                " (SELECT latest.outcome FROM cost_attempts AS latest"
                "  WHERE latest.provider=a.provider AND latest.model=a.model"
                "  ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1) AS last_outcome"
                " FROM cost_attempts AS a WHERE created_at >= :since"
                " GROUP BY provider, model ORDER BY provider, model"
            ),
            {"since": (now - timedelta(days=1)).isoformat()},
        )
    ).mappings()
    active_calls = await session.scalar(
        text("SELECT COUNT(*) FROM call_leases WHERE state='active'")
    )
    # ORM columns format the bound datetime exactly as SQLAlchemy stored it.
    wallet_today = await session.scalar(
        select(func.coalesce(func.sum(UsageSession.cost_paise), 0)).where(
            UsageSession.started_at >= day_start
        )
    )
    disputed = await session.scalar(
        text("SELECT COUNT(*) FROM cost_attempts WHERE state='disputed'")
    )
    return {
        "runtime_profile": policy.profile,
        "components": [
            {"id": id_, "provider": provider, "model": model, "configured": configured}
            for id_, provider, model, configured in components
        ],
        "observed": [dict(row) for row in observed],
        "rates": {
            "configured": rates is not None,
            "version": rates.version if rates else None,
            "qualified": rates.qualified if rates else None,
            "valid_until": rates.valid_until if rates else None,
        },
        "paid_work_enabled": state.call_admission is not None,
        "active_calls": active_calls,
        "spend": {
            "provider_today": await _period_spend(
                session, "global_day", day, policy.global_day_micro_inr
            ),
            "provider_month": await _period_spend(
                session, "global_month", month, policy.variable_month_micro_inr
            ),
            "disputed_attempts": disputed,
            "wallet_today_paise": wallet_today,
        },
    }


@router.get("/policy")
async def operations_policy(request: Request, _: AdministratorDependency) -> dict:
    content, runtime = request.app.state.content_policy, request.app.state.runtime_policy
    examples = {
        language: {"language": language, "allowed": 0, "blocked": 0}
        for language in ("en", "hi", "hinglish")
    }
    for example in content.examples:
        examples[example.language]["allowed" if example.allowed else "blocked"] += 1
    return {
        "content_policy_version": content.version,
        "runtime_policy_version": runtime.version,
        "runtime_profile": runtime.profile,
        "enforcement_points": ENFORCEMENT_POINTS,
        "classifier_timeout_seconds": content.classifier_timeout_seconds,
        "max_text_bytes": content.max_text_bytes,
        "examples": list(examples.values()),
        "limits": {
            **{field: getattr(runtime, field) for field in LIMIT_FIELDS},
            "global_day_micro_inr": runtime.global_day_micro_inr,
            "variable_month_micro_inr": runtime.variable_month_micro_inr,
        },
    }


@router.get("/events")
async def operations_events(
    _: AdministratorDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[str | None, Query(pattern=r"^[^|]{1,64}\|[^|]{1,64}$")] = None,
) -> dict:
    created_before, id_before = before.split("|") if before else ("~", "~")
    # Owner-safe projection: no user, session, job, request hash, bounds or usage payloads.
    attempts = list(
        (
            await session.execute(
                text(
                    "SELECT id, created_at, provider, model, state, outcome,"
                    " CASE WHEN session_id IS NULL THEN 'job' ELSE 'call' END AS scope,"
                    " maximum_micro_inr, actual_micro_inr FROM cost_attempts"
                    " WHERE (created_at, id) < (:created, :id)"
                    " ORDER BY created_at DESC, id DESC LIMIT :limit"
                ),
                {"created": created_before, "id": id_before, "limit": limit + 1},
            )
        ).mappings()
    )
    more = len(attempts) > limit
    attempts = [dict(row) for row in attempts[:limit]]
    events = "FROM turn_metrics, json_each(turn_metrics.dimensions, '$.policy_events') AS event"
    counts = await session.execute(
        text(
            "SELECT json_extract(event.value,'$.stage') AS stage,"
            " json_extract(event.value,'$.category') AS category, COUNT(*) AS count "
            f"{events} GROUP BY stage, category ORDER BY stage, category"
        )
    )
    recent = await session.execute(
        text(
            "SELECT json_extract(event.value,'$.occurred_at') AS occurred_at,"
            " json_extract(event.value,'$.stage') AS stage,"
            " json_extract(event.value,'$.category') AS category,"
            " json_extract(event.value,'$.allowed') = 1 AS allowed,"
            " json_extract(event.value,'$.duration_ms') AS duration_ms,"
            " json_extract(event.value,'$.policy_version') AS policy_version "
            f"{events} ORDER BY occurred_at DESC LIMIT 20"
        )
    )
    return {
        "attempts": attempts,
        "next_before": f"{attempts[-1]['created_at']}|{attempts[-1]['id']}" if more else None,
        "policy_counts": [dict(row) for row in counts.mappings()],
        "policy_recent": [{**row, "allowed": bool(row["allowed"])} for row in recent.mappings()],
    }


@router.get("/analytics", response_model=AnalyticsResponse)
async def operations_analytics(
    _: AdministratorDependency,
    session: SessionDependency,
    window: Annotated[AnalyticsWindow, Query()] = "day",
) -> AnalyticsResponse:
    return await AnalyticsService(session).overview(None, window)


@router.get("/analytics/sessions/{session_id}", response_model=SessionDetailResponse)
async def operations_session_detail(
    session_id: str, _: AdministratorDependency, session: SessionDependency
) -> SessionDetailResponse:
    try:
        return await AnalyticsService(session).session_detail(None, session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
