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

    empty = await client.get("/wallet", headers=headers)
    recharged = await client.post(
        "/wallet/recharge",
        headers=headers,
        json={"amount_paise": 500},
    )
    wallet = await client.get("/wallet", headers=headers)

    assert empty.json()["balance_paise"] == 0
    assert recharged.status_code == 200
    assert recharged.json()["balance_paise"] == 500
    assert wallet.json()["balance_paise"] == 500
    assert wallet.json()["recent_transactions"][0]["amount_paise"] == 500


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
