from __future__ import annotations

import json
import logging

from conftest import RecordingOtpSender
from httpx import AsyncClient


async def authenticate(client: AsyncClient, sender: RecordingOtpSender) -> dict[str, str]:
    response = await client.post("/auth/request-otp", json={"email": "wallet@example.com"})
    assert response.status_code == 202
    verified = await client.post(
        "/auth/verify-otp",
        json={"email": "wallet@example.com", "code": sender.deliveries[-1]["code"]},
    )
    assert verified.status_code == 200
    return {"Authorization": f"Bearer {verified.json()['access_token']}"}


async def test_wallet_recharge_balance_and_transactions(api_client) -> None:
    client, sender = api_client
    headers = await authenticate(client, sender)

    fresh = (await client.get("/wallet", headers=headers)).json()
    recharged = await client.post(
        "/wallet/recharge",
        headers=headers,
        json={"amount_paise": 500},
    )
    second = await client.post("/wallet/recharge", headers=headers, json={"amount_paise": 100})
    wallet = (await client.get("/wallet", headers=headers)).json()

    assert fresh["balance_paise"] == 3000 and fresh["daily_allowance_paise"] == 3000
    assert fresh["max_recharge_paise"] == 1000 and fresh["topups_left_today"] == 1
    assert recharged.status_code == 200
    assert recharged.json()["balance_paise"] == 3500
    assert second.status_code == 429
    assert wallet["balance_paise"] == 3500 and wallet["topups_left_today"] == 0
    assert [item["amount_paise"] for item in wallet["recent_transactions"]] == [500]


async def test_wallet_recharge_rejects_amount_above_limit(api_client) -> None:
    client, sender = api_client
    headers = await authenticate(client, sender)

    response = await client.post(
        "/wallet/recharge",
        headers=headers,
        json={"amount_paise": 1001},
    )

    assert response.status_code == 422


async def test_wallet_invalid_token_logs_token_safe_rejection(
    api_client, caplog, monkeypatch
) -> None:
    client, _ = api_client

    monkeypatch.setattr(logging.getLogger("talkstream"), "propagate", True)
    with caplog.at_level(logging.WARNING, logger="talkstream.auth"):
        response = await client.get("/wallet", headers={"Authorization": "Bearer invalid-token"})

    assert response.status_code == 401
    payloads = [json.loads(record.message) for record in caplog.records]
    assert {
        "event": "auth_rejected",
        "surface": "http",
        "path": "/wallet",
        "reason": "invalid_token",
        "status_code": 401,
    } in payloads
    assert "invalid-token" not in caplog.text
