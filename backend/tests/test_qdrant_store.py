from __future__ import annotations

import json

import httpx

from app.knowledge.vector_store import QdrantPoint, QdrantVectorStore


async def test_qdrant_store_applies_non_optional_tenant_filters() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"result": {"status": "green"}})
        if request.url.path.endswith("/points/search"):
            return httpx.Response(200, json={"result": []})
        return httpx.Response(200, json={"result": {"status": "ok"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = QdrantVectorStore(base_url="http://qdrant:6333", http_client=client)
        await store.upsert(
            collection="knowledge_test",
            dimensions=3,
            points=[
                QdrantPoint(
                    id="5e3fb571-c37c-41bc-8635-87cbb69520e4",
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "user_id": "user-a",
                        "source_id": "source-a",
                        "chunk_id": "chunk-a",
                        "generation_id": "generation-a",
                    },
                )
            ],
        )
        await store.search(
            collection="knowledge_test",
            vector=[1.0, 0.0, 0.0],
            user_id="user-a",
            generation_id="generation-a",
            limit=5,
        )
        await store.delete_source(
            collection="knowledge_test",
            user_id="user-a",
            source_id="source-a",
            generation_id="generation-a",
        )

    search_body = json.loads(
        next(request.content for request in requests if request.url.path.endswith("/points/search"))
    )
    assert search_body["filter"]["must"] == [
        {"key": "user_id", "match": {"value": "user-a"}},
        {"key": "generation_id", "match": {"value": "generation-a"}},
    ]

    delete_request = next(
        request for request in requests if request.url.path.endswith("/points/delete")
    )
    delete_body = json.loads(delete_request.content)
    assert {condition["key"] for condition in delete_body["filter"]["must"]} == {
        "user_id",
        "source_id",
        "generation_id",
    }
