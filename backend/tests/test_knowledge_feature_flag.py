from __future__ import annotations

import httpx
from conftest import RecordingOtpSender

from app.config import Settings
from app.main import create_app


async def _login(client: httpx.AsyncClient, sender) -> str:
    await client.post("/auth/request-otp", json={"email": "rag@example.com"})
    code = sender.deliveries[-1]["code"]
    response = await client.post(
        "/auth/verify-otp",
        json={"email": "rag@example.com", "code": code},
    )
    return response.json()["access_token"]


async def test_knowledge_feature_defaults_to_disabled(api_client) -> None:
    client, sender = api_client

    capabilities = await client.get("/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["features"] == {"knowledge_rag": False}

    token = await _login(client, sender)
    response = await client.get(
        "/knowledge/providers",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404
    assert not hasattr(client._transport.app.state, "knowledge_service")


async def test_enabled_feature_exposes_only_allowlisted_provider_metadata(
    settings: Settings,
) -> None:
    enabled = settings.model_copy(
        update={
            "knowledge_rag_enabled": True,
            "openai_api_key": "test-openai-key",
        }
    )
    app = create_app(enabled)

    async with app.router.lifespan_context(app):
        sender = RecordingOtpSender()
        app.state.email_sender = sender
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/capabilities")).json()["features"] == {"knowledge_rag": True}
            token = await _login(client, sender)
            response = await client.get(
                "/knowledge/providers",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "providers": [
            {
                "id": "openai",
                "label": "OpenAI",
                "available": True,
                "key_set": False,
                "key_hint": None,
                "models": [
                    {
                        "id": "text-embedding-3-small",
                        "label": "Text Embedding 3 Small",
                        "dimensions": 1536,
                        "default": True,
                    },
                    {
                        "id": "text-embedding-3-large",
                        "label": "Text Embedding 3 Large",
                        "dimensions": 3072,
                        "default": False,
                    },
                ],
            },
            {
                "id": "gemini",
                "label": "Google Gemini",
                "available": False,
                "key_set": False,
                "key_hint": None,
                "models": [
                    {
                        "id": "gemini-embedding-2",
                        "label": "Gemini Embedding 2",
                        "dimensions": 3072,
                        "default": True,
                    },
                ],
            },
            {
                "id": "openrouter",
                "label": "OpenRouter",
                "available": False,
                "key_set": False,
                "key_hint": None,
                "models": [
                    {
                        "id": "qwen/qwen3-embedding-8b",
                        "label": "Qwen3 Embedding 8B",
                        "dimensions": 4096,
                        "default": True,
                    }
                ],
            },
        ]
    }
    assert "test-openai-key" not in response.text


async def test_enabled_provider_catalog_reports_missing_key_without_fallback(
    settings: Settings,
) -> None:
    app = create_app(
        settings.model_copy(update={"knowledge_rag_enabled": True, "openai_api_key": None})
    )

    async with app.router.lifespan_context(app):
        sender = RecordingOtpSender()
        app.state.email_sender = sender
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _login(client, sender)
            response = await client.get(
                "/knowledge/providers",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 200
    assert response.json()["providers"][0]["available"] is False
