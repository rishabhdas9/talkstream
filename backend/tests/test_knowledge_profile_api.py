from __future__ import annotations

import httpx
from conftest import RecordingOtpSender
from test_cost_service import clock as clock
from test_cost_service import rates as rates
from test_embedding_costs import embedding_costs as embedding_costs
from test_embedding_costs import embedding_rates as embedding_rates

from app.config import Settings
from app.knowledge.embeddings import EmbeddingClientFactory, OpenAIEmbeddingClient
from app.knowledge.service import KnowledgeService
from app.main import create_app


async def _login(client: httpx.AsyncClient, sender: RecordingOtpSender) -> str:
    await client.post("/auth/request-otp", json={"email": "profile@example.com"})
    code = sender.deliveries[-1]["code"]
    response = await client.post(
        "/auth/verify-otp",
        json={"email": "profile@example.com", "code": code},
    )
    return response.json()["access_token"]


async def test_profile_routes_require_authentication(settings: Settings) -> None:
    app = create_app(
        settings.model_copy(
            update={"live_evaluation_cap_micro_inr": 1_000_000, "knowledge_rag_enabled": True}
        )
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/knowledge/profile")).status_code == 401
            assert (await client.put("/knowledge/profile", json={})).status_code == 401


async def test_user_can_test_save_and_read_allowlisted_profile(
    settings: Settings, embedding_costs
) -> None:
    def embedding_handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.openai.com/v1/embeddings"
        assert request.headers["authorization"] == "Bearer test-openai-key"
        body = __import__("json").loads(request.content)
        dimensions = 1536 if body["model"] == "text-embedding-3-small" else 3072
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 5},
                "data": [{"embedding": [0.25] * dimensions, "index": 0}],
            },
        )

    enabled = settings.model_copy(
        update={
            "live_evaluation_cap_micro_inr": 1_000_000,
            "knowledge_rag_enabled": True,
            "openai_api_key": "test-openai-key",
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
                    api_key="test-openai-key",
                    http_client=upstream,
                ),
                client_factory=EmbeddingClientFactory(http_client=upstream, settings=enabled),
                costs=embedding_costs,
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                token = await _login(client, sender)
                headers = {"Authorization": f"Bearer {token}"}

                initial = await client.get("/knowledge/profile", headers=headers)
                assert initial.status_code == 200
                assert initial.json()["configured"] is False

                tested = await client.post(
                    "/knowledge/profile/test",
                    headers=headers,
                    json={"provider_id": "openai", "model_id": "text-embedding-3-small"},
                )
                assert tested.status_code == 200
                assert tested.json() == {"ok": True, "dimensions": 1536}

                saved = await client.put(
                    "/knowledge/profile",
                    headers=headers,
                    json={"provider_id": "openai", "model_id": "text-embedding-3-small"},
                )
                assert saved.status_code == 200
                assert saved.json()["configured"] is True
                assert saved.json()["active"] is True
                assert saved.json()["status"] == "ready"

                loaded = await client.get("/knowledge/profile", headers=headers)
                assert loaded.json()["generation_id"] == saved.json()["generation_id"]
                assert loaded.json()["model_id"] == "text-embedding-3-small"


async def test_profile_rejects_unknown_or_unavailable_model(settings: Settings) -> None:
    enabled = settings.model_copy(
        update={
            "live_evaluation_cap_micro_inr": 1_000_000,
            "knowledge_rag_enabled": True,
            "openai_api_key": None,
        }
    )
    app = create_app(enabled)
    async with app.router.lifespan_context(app):
        sender = RecordingOtpSender()
        app.state.email_sender = sender
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _login(client, sender)
            headers = {"Authorization": f"Bearer {token}"}

            unknown = await client.put(
                "/knowledge/profile",
                headers=headers,
                json={"provider_id": "openai", "model_id": "made-up-model"},
            )
            assert unknown.status_code == 400

            unavailable = await client.put(
                "/knowledge/profile",
                headers=headers,
                json={"provider_id": "openai", "model_id": "text-embedding-3-small"},
            )
            assert unavailable.status_code == 409
            assert "unavailable" in unavailable.json()["detail"].lower()
