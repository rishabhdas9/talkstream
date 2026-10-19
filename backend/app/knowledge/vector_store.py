from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx


class VectorStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class QdrantPoint:
    id: str
    vector: list[float]
    payload: dict[str, Any]


def point_id(chunk_id: str, generation_id: str) -> str:
    # One chunk gets a distinct, replay-stable UUID in every generation of a shared collection.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{generation_id}:{chunk_id}"))


class QdrantVectorStore:
    def __init__(self, *, base_url: str, http_client: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client
        self._ready_collections: set[str] = set()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http_client.request(
                method,
                f"{self._base_url}{path}",
                timeout=30.0,
                follow_redirects=False,
                **kwargs,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise VectorStoreError("Vector database request failed") from exc

    async def _ensure_collection(self, collection: str, dimensions: int) -> None:
        if collection in self._ready_collections:
            return
        try:
            response = await self._http_client.get(
                f"{self._base_url}/collections/{collection}", timeout=10.0
            )
        except httpx.HTTPError as exc:
            raise VectorStoreError("Vector database request failed") from exc
        if response.status_code == 404:
            await self._request(
                "PUT",
                f"/collections/{collection}",
                json={"vectors": {"size": dimensions, "distance": "Cosine"}},
            )
            for field in ("user_id", "source_id", "generation_id"):
                await self._request(
                    "PUT",
                    f"/collections/{collection}/index",
                    params={"wait": "true"},
                    json={"field_name": field, "field_schema": "keyword"},
                )
        elif response.is_error:
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise VectorStoreError("Vector database request failed") from exc
        self._ready_collections.add(collection)

    async def upsert(
        self,
        *,
        collection: str,
        dimensions: int,
        points: list[QdrantPoint],
    ) -> None:
        await self._ensure_collection(collection, dimensions)
        if not points:
            return
        await self._request(
            "PUT",
            f"/collections/{collection}/points",
            params={"wait": "true"},
            json={
                "points": [
                    {"id": point.id, "vector": point.vector, "payload": point.payload}
                    for point in points
                ]
            },
        )

    async def search(
        self,
        *,
        collection: str,
        vector: list[float],
        user_id: str,
        generation_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "POST",
            f"/collections/{collection}/points/search",
            json={
                "vector": vector,
                "limit": limit,
                "with_payload": True,
                "filter": {
                    "must": [
                        {"key": "user_id", "match": {"value": user_id}},
                        {"key": "generation_id", "match": {"value": generation_id}},
                    ]
                },
            },
        )
        result = response.json().get("result", [])
        if not isinstance(result, list):
            raise VectorStoreError("Vector database returned an invalid search response")
        return result

    async def delete_source(
        self,
        *,
        collection: str,
        user_id: str,
        source_id: str,
        generation_id: str,
    ) -> None:
        await self._request(
            "POST",
            f"/collections/{collection}/points/delete",
            params={"wait": "true"},
            json={
                "filter": {
                    "must": [
                        {"key": "user_id", "match": {"value": user_id}},
                        {"key": "source_id", "match": {"value": source_id}},
                        {"key": "generation_id", "match": {"value": generation_id}},
                    ]
                }
            },
        )
