from __future__ import annotations

import math
from typing import Any

import httpx
from conftest import RecordingOtpSender

from app.config import Settings
from app.knowledge.service import KnowledgeService
from app.main import create_app


class FakeEmbeddingClient:
    async def embed(
        self,
        texts,
        *,
        model_id: str,
        expected_dimensions: int,
    ) -> list[list[float]]:
        del model_id
        vectors = []
        for text in texts:
            is_hindi = any("\u0900" <= character <= "\u097f" for character in text)
            refund = 1.0 if "refund" in text.lower() else 0.0
            vectors.append([refund, float(is_hindi)] + [0.0] * (expected_dimensions - 2))
        return vectors


class FakeVectorStore:
    def __init__(self) -> None:
        self.points: dict[str, dict[str, Any]] = {}
        self.search_filters: list[tuple[str, str]] = []

    async def upsert(self, *, collection: str, dimensions: int, points) -> None:
        del dimensions
        for point in points:
            self.points[point.id] = {"collection": collection, "point": point}

    async def search(
        self,
        *,
        collection: str,
        vector,
        user_id: str,
        generation_id: str,
        limit: int,
    ):
        self.search_filters.append((user_id, generation_id))
        matches = []
        for stored in self.points.values():
            point = stored["point"]
            if stored["collection"] != collection:
                continue
            if point.payload["user_id"] != user_id:
                continue
            if point.payload["generation_id"] != generation_id:
                continue
            score = sum(a * b for a, b in zip(point.vector, vector, strict=True))
            matches.append({"id": point.id, "score": score, "payload": point.payload})
        return sorted(matches, key=lambda item: item["score"], reverse=True)[:limit]

    async def delete_source(
        self,
        *,
        collection: str,
        user_id: str,
        source_id: str,
        generation_id: str,
    ) -> None:
        doomed = [
            point_id
            for point_id, stored in self.points.items()
            if stored["collection"] == collection
            and stored["point"].payload["user_id"] == user_id
            and stored["point"].payload["source_id"] == source_id
            and stored["point"].payload["generation_id"] == generation_id
        ]
        for point_id in doomed:
            del self.points[point_id]


async def _login(client: httpx.AsyncClient, sender: RecordingOtpSender, email: str) -> str:
    await client.post("/auth/request-otp", json={"email": email})
    response = await client.post(
        "/auth/verify-otp",
        json={"email": email, "code": sender.deliveries[-1]["code"]},
    )
    return response.json()["access_token"]


async def _save_default_profile(client: httpx.AsyncClient, token: str) -> None:
    response = await client.put(
        "/knowledge/profile",
        headers={"Authorization": f"Bearer {token}"},
        json={"provider_id": "openai", "model_id": "text-embedding-3-small"},
    )
    assert response.status_code == 200


async def test_text_ingestion_search_and_delete_are_tenant_scoped(settings: Settings) -> None:
    enabled = settings.model_copy(
        update={"knowledge_rag_enabled": True, "openai_api_key": "test-key"}
    )
    app = create_app(enabled)
    vectors = FakeVectorStore()
    async with app.router.lifespan_context(app):
        sender = RecordingOtpSender()
        app.state.email_sender = sender
        app.state.knowledge_service = KnowledgeService(
            database=app.state.database,
            settings=enabled,
            embedding_client=FakeEmbeddingClient(),
            vector_store=vectors,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            token_a = await _login(client, sender, "a@example.com")
            token_b = await _login(client, sender, "b@example.com")
            await _save_default_profile(client, token_a)
            await _save_default_profile(client, token_b)

            source_a = await client.post(
                "/knowledge/sources/text",
                headers={"Authorization": f"Bearer {token_a}"},
                json={
                    "name": "Refund policy",
                    "text": "Approved refund requests are credited within five working days.",
                },
            )
            assert source_a.status_code == 201
            assert source_a.json()["status"] == "indexed"

            source_b = await client.post(
                "/knowledge/sources/text",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"name": "Private notes", "text": "Secret refund phrase for user B."},
            )
            assert source_b.status_code == 201

            searched = await client.post(
                "/knowledge/search",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"query": "When do refunds arrive?", "limit": 5},
            )
            assert searched.status_code == 200
            assert [item["source_name"] for item in searched.json()["results"]] == ["Refund policy"]
            assert all(math.isfinite(item["score"]) for item in searched.json()["results"])
            assert vectors.search_filters[-1][0] != ""

            hindi_source = await client.post(
                "/knowledge/sources/text",
                headers={"Authorization": f"Bearer {token_a}"},
                json={
                    "name": "छुट्टी नीति",
                    "text": "कर्मचारी साल में बीस दिन की छुट्टी ले सकते हैं।",
                },
            )
            assert hindi_source.status_code == 201
            hindi_search = await client.post(
                "/knowledge/search",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"query": "मुझे कितनी छुट्टी मिलती है?", "limit": 1},
            )
            assert hindi_search.json()["results"][0]["source_name"] == "छुट्टी नीति"

            forbidden_delete = await client.delete(
                f"/knowledge/sources/{source_b.json()['id']}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert forbidden_delete.status_code == 204

            deleted = await client.delete(
                f"/knowledge/sources/{source_a.json()['id']}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert deleted.status_code == 204
            assert (
                await client.delete(
                    f"/knowledge/sources/{source_a.json()['id']}",
                    headers={"Authorization": f"Bearer {token_a}"},
                )
            ).status_code == 204


async def test_source_requires_profile_and_enforces_size_limit(settings: Settings) -> None:
    enabled = settings.model_copy(
        update={
            "knowledge_rag_enabled": True,
            "openai_api_key": "test-key",
            "knowledge_max_source_characters": 20,
        }
    )
    app = create_app(enabled)
    async with app.router.lifespan_context(app):
        sender = RecordingOtpSender()
        app.state.email_sender = sender
        app.state.knowledge_service = KnowledgeService(
            database=app.state.database,
            settings=enabled,
            embedding_client=FakeEmbeddingClient(),
            vector_store=FakeVectorStore(),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _login(client, sender, "limits@example.com")
            headers = {"Authorization": f"Bearer {token}"}

            missing_profile = await client.post(
                "/knowledge/sources/text",
                headers=headers,
                json={"name": "Notes", "text": "short text"},
            )
            assert missing_profile.status_code == 409

            await _save_default_profile(client, token)
            oversized = await client.post(
                "/knowledge/sources/text",
                headers=headers,
                json={"name": "Too large", "text": "x" * 21},
            )
            assert oversized.status_code == 413

            markdown = await client.post(
                "/knowledge/sources/file",
                headers=headers,
                files={"file": ("guide.md", b"# Guide\n\nTrusted.", "text/markdown")},
            )
            assert markdown.status_code == 201
            assert markdown.json()["name"] == "guide.md"
            assert markdown.json()["media_type"] == "text/markdown"

            unsupported = await client.post(
                "/knowledge/sources/file",
                headers=headers,
                files={"file": ("archive.zip", b"not a supported source", "application/zip")},
            )
            assert unsupported.status_code == 400


async def test_delete_reports_retryable_cleanup_and_conflicts(settings: Settings) -> None:
    enabled = settings.model_copy(
        update={"knowledge_rag_enabled": True, "openai_api_key": "test-key"}
    )
    app = create_app(enabled)
    vectors = FakeVectorStore()
    async with app.router.lifespan_context(app):
        sender = RecordingOtpSender()
        app.state.email_sender = sender
        app.state.knowledge_service = KnowledgeService(
            database=app.state.database,
            settings=enabled,
            embedding_client=FakeEmbeddingClient(),
            vector_store=vectors,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {await _login(client, sender, 'd@example.com')}"}
            await _save_default_profile(client, token=headers["Authorization"][7:])
            ids = []
            for name in ("First", "Second"):
                created = await client.post(
                    "/knowledge/sources/text",
                    headers=headers,
                    json={"name": name, "text": f"{name} refund note."},
                )
                ids.append(created.json()["id"])
            working_delete = vectors.delete_source

            async def unavailable(**kwargs):
                raise RuntimeError("qdrant down")

            vectors.delete_source = unavailable
            failed = await client.delete(f"/knowledge/sources/{ids[0]}", headers=headers)
            assert failed.status_code == 502 and failed.headers["retry-after"] == "5"
            listed = (await client.get("/knowledge/sources", headers=headers)).json()["sources"]
            pending = next(item for item in listed if item["id"] == ids[0])
            assert pending["status"] == "failed" and "retry" in pending["error_message"].lower()
            conflict = await client.delete(f"/knowledge/sources/{ids[1]}", headers=headers)
            assert conflict.status_code == 409

            vectors.delete_source = working_delete
            for source_id in ids:
                deleted = await client.delete(f"/knowledge/sources/{source_id}", headers=headers)
                assert deleted.status_code == 204
            assert (await client.get("/knowledge/sources", headers=headers)).json() == {
                "sources": []
            }
            assert vectors.points == {}
