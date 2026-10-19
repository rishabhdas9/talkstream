from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from test_admin_authorization import login

from app.models import TurnMetric

ADMIN = "owner@example.com"
PATHS = ["/operations/status", "/operations/policy", "/operations/events"]


@pytest.fixture
async def operations(api_client):
    client, sender = api_client
    app = client._transport.app
    app.state.settings.admin_emails = ADMIN
    admin = {"Authorization": f"Bearer {await login(client, sender, ADMIN)}"}
    regular = {"Authorization": f"Bearer {await login(client, sender, 'person@example.com')}"}
    return client, app, admin, regular


async def insert_attempt(app, attempt_id, *, provider, model, state, outcome, created_at, actual):
    async with app.state.database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO cost_attempts (id,request_hash,user_id,job_id,provider,model,"
                "bound_json,rate_card_json,maximum_micro_inr,actual_micro_inr,state,outcome,"
                "usage_json,day_key,month_key,created_at) VALUES (:id,'secret-hash',"
                "'private-user','private-job',:provider,:model,'{}','{}',5000,:actual,:state,"
                ":outcome,'{\"input_tokens\": 7}','2026-10-18','2026-10',:created)"
            ),
            {
                "id": attempt_id,
                "provider": provider,
                "model": model,
                "state": state,
                "outcome": outcome,
                "actual": actual,
                "created": created_at.isoformat(),
            },
        )


@pytest.mark.parametrize("path", PATHS)
async def test_regular_user_cannot_read_operations(operations, path):
    client, _, _, regular = operations
    assert (await client.get(path, headers=regular)).status_code == 403
    assert (await client.get(path)).status_code == 401


async def test_status_separates_configuration_from_observed_outcomes(operations):
    client, app, admin, _ = operations
    app.state.settings.exa_api_key = "exa-secret-value-should-never-appear"
    empty = await client.get("/operations/status", headers=admin)
    assert empty.status_code == 200
    body = empty.json()
    assert "exa-secret-value" not in empty.text
    components = {item["id"]: item for item in body["components"]}
    assert components["web_search"]["configured"] is True
    assert components["speech_recognition"]["configured"] is False
    assert components["knowledge"]["configured"] is False
    assert body["observed"] == []
    assert body["rates"]["configured"] is False and body["paid_work_enabled"] is False
    assert body["spend"]["provider_today"]["settled_micro_inr"] == 0
    assert body["spend"]["wallet_today_paise"] == 0

    now = datetime.now(UTC)
    await insert_attempt(
        app,
        "a1",
        provider="exa",
        model="fast",
        state="settled",
        outcome="succeeded",
        created_at=now - timedelta(minutes=5),
        actual=700,
    )
    await insert_attempt(
        app,
        "a2",
        provider="exa",
        model="fast",
        state="unknown",
        outcome="unknown",
        created_at=now - timedelta(minutes=1),
        actual=None,
    )
    observed = (await client.get("/operations/status", headers=admin)).json()["observed"]
    assert observed == [
        {
            "provider": "exa",
            "model": "fast",
            "attempts": 2,
            "succeeded": 1,
            "failed": 0,
            "cancelled": 0,
            "unknown": 1,
            "last_outcome": "unknown",
            "last_attempt_at": observed[0]["last_attempt_at"],
            "last_success_at": observed[0]["last_success_at"],
        }
    ]
    assert observed[0]["last_success_at"] < observed[0]["last_attempt_at"]


async def test_policy_reports_effective_versions_and_limits(operations):
    client, _, admin, _ = operations
    body = (await client.get("/operations/policy", headers=admin)).json()
    assert body["content_policy_version"] == "v1"
    assert body["runtime_policy_version"] == "v1"
    assert {"input", "output"} <= {point["id"] for point in body["enforcement_points"]}
    languages = {item["language"]: item for item in body["examples"]}
    assert set(languages) == {"en", "hi", "hinglish"}
    assert all(item["allowed"] > 0 and item["blocked"] > 0 for item in languages.values())
    assert body["limits"]["session_seconds"] == 120
    assert body["limits"]["global_day_micro_inr"] == 30_000_000


async def test_events_are_metadata_only_and_paginated(operations):
    client, app, admin, _ = operations
    now = datetime.now(UTC)
    for index in range(3):
        await insert_attempt(
            app,
            f"attempt-{index}",
            provider="sarvam",
            model="sarvam-105b",
            state="settled",
            outcome="succeeded",
            created_at=now - timedelta(minutes=index),
            actual=10 + index,
        )
    event = {
        "allowed": False,
        "category": "credential_theft",
        "policy_version": "v1",
        "stage": "input",
        "duration_ms": 12.5,
        "occurred_at": now.isoformat(),
    }
    async with app.state.database.engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO users VALUES ('private-user','private@example.test',:now,1)"),
            {"now": now.isoformat()},
        )
        await connection.execute(
            text(
                "INSERT INTO usage_sessions (id,user_id,started_at,billed_seconds,cost_paise) "
                "VALUES ('s','private-user',:now,10,40)"
            ),
            {"now": now.isoformat()},
        )
    async with app.state.database.session_factory() as session:
        session.add(
            TurnMetric(
                usage_session_id="s",
                turn_index=0,
                dimensions={
                    "policy_events": [event, {**event, "stage": "output"}],
                    "transcript": "my private words",
                },
            )
        )
        await session.commit()

    first = await client.get("/operations/events?limit=2", headers=admin)
    assert first.status_code == 200
    body = first.json()
    for secret in (
        "private-user",
        "private-job",
        "secret-hash",
        "my private words",
        "input_tokens",
    ):
        assert secret not in first.text
    assert [item["id"] for item in body["attempts"]] == ["attempt-0", "attempt-1"]
    assert body["attempts"][0]["scope"] == "job"
    assert sorted((c["stage"], c["category"], c["count"]) for c in body["policy_counts"]) == [
        ("input", "credential_theft", 1),
        ("output", "credential_theft", 1),
    ]
    assert body["policy_recent"][0]["duration_ms"] == 12.5
    second = (
        await client.get(
            "/operations/events", params={"limit": 2, "before": body["next_before"]}, headers=admin
        )
    ).json()
    assert [item["id"] for item in second["attempts"]] == ["attempt-2"]
    assert second["next_before"] is None
    assert (
        await client.get("/operations/events", params={"before": "bogus"}, headers=admin)
    ).status_code == 422

    status = (await client.get("/operations/status", headers=admin)).json()
    assert status["spend"]["wallet_today_paise"] == 40


async def test_wallet_today_counts_orm_sessions_after_kolkata_midnight(operations):
    from types import SimpleNamespace

    from app.models import UsageSession, User

    client, app, admin, _ = operations
    now = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)  # 01:30 on 19 October in Kolkata
    app.state.clock = SimpleNamespace(utc_now=lambda: now)
    async with app.state.database.session_factory() as session:
        user = User(email="wallet@example.com", is_verified=True)
        session.add(user)
        await session.flush()
        for minutes, paise in ((-100, 7), (-60, 11)):  # before and after Kolkata midnight
            session.add(
                UsageSession(
                    user_id=user.id, started_at=now + timedelta(minutes=minutes), cost_paise=paise
                )
            )
        await session.commit()
    status = (await client.get("/operations/status", headers=admin)).json()
    assert status["spend"]["wallet_today_paise"] == 11
