from __future__ import annotations

import httpx
import pytest
from conftest import RecordingOtpSender
from cryptography.fernet import Fernet
from test_cost_service import clock as clock
from test_cost_service import rates as rates
from test_embedding_costs import embedding_costs as embedding_costs
from test_embedding_costs import embedding_rates as embedding_rates

from app.config import Settings
from app.knowledge.embeddings import (
    EmbeddingClientFactory,
    EmbeddingProviderError,
    GeminiEmbeddingClient,
    OpenAIEmbeddingClient,
)
from app.knowledge.service import KnowledgeService
from app.main import create_app


async def _login(client: httpx.AsyncClient, sender: RecordingOtpSender) -> str:
    await client.post("/auth/request-otp", json={"email": "keys@example.com"})
    code = sender.deliveries[-1]["code"]
    response = await client.post(
        "/auth/verify-otp",
        json={"email": "keys@example.com", "code": code},
    )
    return response.json()["access_token"]


def _provider(payload: dict, provider_id: str) -> dict:
    return next(item for item in payload["providers"] if item["id"] == provider_id)


async def test_user_key_enables_provider_and_is_masked(settings: Settings, embedding_costs) -> None:
    def embedding_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta/models/gemini-embedding-2:batchEmbedContents"
        assert request.headers["x-goog-api-key"] == "user-gemini-key-123"
        return httpx.Response(
            200,
            json={
                "usageMetadata": {"promptTokenCount": 5},
                "embeddings": [{"values": [0.5] * 3072}],
            },
        )

    enabled = settings.model_copy(
        update={
            "credential_encryption_key": Fernet.generate_key().decode(),
            "live_evaluation_cap_micro_inr": 1_000_000,
            "knowledge_rag_enabled": True,
        }
    )
    app = create_app(enabled)
    async with app.router.lifespan_context(app):
        sender = RecordingOtpSender()
        app.state.email_sender = sender
        async with httpx.AsyncClient(transport=httpx.MockTransport(embedding_handler)) as upstream:
            app.state.knowledge_service = KnowledgeService(
                database=app.state.database,
                settings=enabled,
                embedding_client=OpenAIEmbeddingClient(api_key=None, http_client=upstream),
                client_factory=EmbeddingClientFactory(http_client=upstream, settings=enabled),
                costs=embedding_costs,
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                token = await _login(client, sender)
                headers = {"Authorization": f"Bearer {token}"}

                # Without env or user keys nothing is available.
                catalog = (await client.get("/knowledge/providers", headers=headers)).json()
                assert _provider(catalog, "gemini")["available"] is False
                assert _provider(catalog, "openai")["available"] is False

                # A short key is rejected by validation.
                rejected = await client.put(
                    "/knowledge/providers/gemini/key",
                    headers=headers,
                    json={"api_key": "short"},
                )
                assert rejected.status_code == 422

                saved = await client.put(
                    "/knowledge/providers/gemini/key",
                    headers=headers,
                    json={"api_key": "user-gemini-key-123"},
                )
                assert saved.status_code == 200
                gemini = _provider(saved.json(), "gemini")
                assert gemini["available"] is True
                assert gemini["key_set"] is True
                assert gemini["key_hint"] == "user…-123"
                assert "user-gemini-key-123" not in saved.text

                unknown = await client.put(
                    "/knowledge/providers/nope/key",
                    headers=headers,
                    json={"api_key": "whatever-key-value"},
                )
                assert unknown.status_code == 404

                # The stored key powers a live provider test.
                tested = await client.post(
                    "/knowledge/profile/test",
                    headers=headers,
                    json={"provider_id": "gemini", "model_id": "gemini-embedding-2"},
                )
                assert tested.status_code == 200
                assert tested.json() == {"ok": True, "dimensions": 3072}

                removed = await client.delete("/knowledge/providers/gemini/key", headers=headers)
                assert removed.status_code == 200
                assert _provider(removed.json(), "gemini")["available"] is False

                unavailable = await client.post(
                    "/knowledge/profile/test",
                    headers=headers,
                    json={"provider_id": "gemini", "model_id": "gemini-embedding-2"},
                )
                assert unavailable.status_code == 409


async def test_user_key_takes_precedence_over_environment_key(
    settings: Settings, embedding_costs
) -> None:
    seen_keys: list[str] = []

    def embedding_handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["authorization"])
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 5},
                "data": [{"embedding": [0.25] * 1536, "index": 0}],
            },
        )

    enabled = settings.model_copy(
        update={
            "credential_encryption_key": Fernet.generate_key().decode(),
            "live_evaluation_cap_micro_inr": 1_000_000,
            "knowledge_rag_enabled": True,
            "openai_api_key": "env-openai-key",
        }
    )
    app = create_app(enabled)
    async with app.router.lifespan_context(app):
        sender = RecordingOtpSender()
        app.state.email_sender = sender
        async with httpx.AsyncClient(transport=httpx.MockTransport(embedding_handler)) as upstream:
            app.state.knowledge_service = KnowledgeService(
                database=app.state.database,
                settings=enabled,
                embedding_client=OpenAIEmbeddingClient(
                    api_key="env-openai-key", http_client=upstream
                ),
                client_factory=EmbeddingClientFactory(http_client=upstream, settings=enabled),
                costs=embedding_costs,
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                token = await _login(client, sender)
                headers = {"Authorization": f"Bearer {token}"}

                await client.post(
                    "/knowledge/profile/test",
                    headers=headers,
                    json={"provider_id": "openai", "model_id": "text-embedding-3-small"},
                )
                assert seen_keys[-1] == "Bearer env-openai-key"

                await client.put(
                    "/knowledge/providers/openai/key",
                    headers=headers,
                    json={"api_key": "user-openai-key"},
                )
                await client.post(
                    "/knowledge/profile/test",
                    headers=headers,
                    json={"provider_id": "openai", "model_id": "text-embedding-3-small"},
                )
                assert seen_keys[-1] == "Bearer user-openai-key"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "secret provider detail"}}),
        httpx.Response(200, json={"embeddings": [{"values": [1.0]}]}),
        httpx.Response(
            200,
            content=b'{"embeddings":[{"values":[NaN,NaN,NaN]}]}',
            headers={"content-type": "application/json"},
        ),
        httpx.Response(200, json={"embeddings": ["not-an-object"]}),
        httpx.Response(200, json={"embeddings": []}),
    ],
)
async def test_gemini_failures_are_validated_and_sanitized(response: httpx.Response) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as http_client:
        client = GeminiEmbeddingClient(api_key="secret-key", http_client=http_client)
        with pytest.raises(EmbeddingProviderError) as raised:
            await client.embed(["fixture"], model_id="gemini-embedding-2", expected_dimensions=3)

    message = str(raised.value)
    assert "secret-key" not in message
    assert "secret provider detail" not in message
