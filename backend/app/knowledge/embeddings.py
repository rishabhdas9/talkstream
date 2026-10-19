from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import httpx

from app.config import Settings
from app.knowledge.providers import get_model_definition, provider_is_configured


class EmbeddingProviderError(RuntimeError):
    pass


def _validate_model(provider_id: str | None, model_id: str, dimensions: int) -> None:
    if provider_id is None:
        return
    model = get_model_definition(provider_id, model_id)
    if model is None or type(dimensions) is not int or model.dimensions != dimensions:
        raise EmbeddingProviderError("Unsupported embedding provider model or dimensions")


def _vector(raw_vector: object, dimensions: int) -> list[float]:
    if not isinstance(raw_vector, list) or len(raw_vector) != dimensions:
        raise EmbeddingProviderError("Embedding provider returned an unexpected vector dimension")
    if not all(type(value) in (int, float) for value in raw_vector):
        raise EmbeddingProviderError("Embedding provider returned a non-numeric value")
    vector = [float(value) for value in raw_vector]
    if not all(math.isfinite(value) for value in vector):
        raise EmbeddingProviderError("Embedding provider returned a non-finite value")
    return vector


class OpenAIEmbeddingClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        http_client: httpx.AsyncClient,
        base_url: str = "https://api.openai.com/v1",
        provider_id: str | None = None,
        provider_preferences: dict | None = None,
    ) -> None:
        self._api_key = api_key
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")
        self._provider_id = provider_id
        self._provider_preferences = provider_preferences

    async def embed(
        self,
        texts: Sequence[str],
        *,
        model_id: str,
        expected_dimensions: int,
        usage_callback: Callable[[int], None] | None = None,
    ) -> list[list[float]]:
        if not self._api_key:
            raise EmbeddingProviderError("OpenAI embedding provider is unavailable")
        _validate_model(self._provider_id, model_id, expected_dimensions)
        if not texts:
            return []
        body = {"model": model_id, "input": list(texts)}
        if self._provider_preferences is not None:
            body.update(
                {
                    "provider": self._provider_preferences,
                    "dimensions": expected_dimensions,
                    "encoding_format": "float",
                }
            )
        try:
            response = await self._http_client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
                timeout=30.0,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise EmbeddingProviderError("Embedding provider returned an invalid response")
            usage = payload.get("usage")
            prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
            if usage_callback and type(prompt_tokens) is int and prompt_tokens >= 0:
                usage_callback(prompt_tokens)
            if self._provider_id == "openrouter" and payload.get("model") != model_id:
                raise EmbeddingProviderError("Embedding provider returned an unexpected model")
            raw_items = payload.get("data")
            if not isinstance(raw_items, list) or len(raw_items) != len(texts):
                raise EmbeddingProviderError("Embedding provider returned an invalid batch")
            if not all(isinstance(item, dict) for item in raw_items):
                raise EmbeddingProviderError("Embedding provider returned an invalid batch")
            if not all(type(item.get("index")) is int for item in raw_items):
                raise EmbeddingProviderError("Embedding provider returned invalid vector indices")
            ordered = sorted(raw_items, key=lambda item: item.get("index", -1))
            if [item.get("index") for item in ordered] != list(range(len(texts))):
                raise EmbeddingProviderError("Embedding provider returned invalid vector indices")
            vectors: list[list[float]] = []
            for item in ordered:
                raw_vector = item.get("embedding") if isinstance(item, dict) else None
                vectors.append(_vector(raw_vector, expected_dimensions))
            return vectors
        except EmbeddingProviderError:
            raise
        except (httpx.HTTPError, TypeError, ValueError, OverflowError):
            raise EmbeddingProviderError("Embedding provider request failed") from None


class GeminiEmbeddingClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        http_client: httpx.AsyncClient,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        provider_id: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")
        self._provider_id = provider_id

    async def embed(
        self,
        texts: Sequence[str],
        *,
        model_id: str,
        expected_dimensions: int,
        usage_callback: Callable[[int], None] | None = None,
    ) -> list[list[float]]:
        if not self._api_key:
            raise EmbeddingProviderError("Gemini embedding provider is unavailable")
        _validate_model(self._provider_id, model_id, expected_dimensions)
        if not texts:
            return []
        try:
            response = await self._http_client.post(
                f"{self._base_url}/models/{model_id}:batchEmbedContents",
                headers={"x-goog-api-key": self._api_key},
                json={
                    "requests": [
                        {
                            "model": f"models/{model_id}",
                            "content": {"parts": [{"text": text}]},
                            "outputDimensionality": expected_dimensions,
                        }
                        for text in texts
                    ]
                },
                timeout=30.0,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise EmbeddingProviderError("Embedding provider returned an invalid response")
            usage = payload.get("usageMetadata")
            prompt_tokens = usage.get("promptTokenCount") if isinstance(usage, dict) else None
            if usage_callback and type(prompt_tokens) is int and prompt_tokens >= 0:
                usage_callback(prompt_tokens)
            raw_items = payload.get("embeddings")
            if not isinstance(raw_items, list) or len(raw_items) != len(texts):
                raise EmbeddingProviderError("Embedding provider returned an invalid batch")
            vectors: list[list[float]] = []
            for item in raw_items:
                raw_vector = item.get("values") if isinstance(item, dict) else None
                vectors.append(_vector(raw_vector, expected_dimensions))
            return vectors
        except EmbeddingProviderError:
            raise
        except (httpx.HTTPError, TypeError, ValueError, OverflowError):
            raise EmbeddingProviderError("Embedding provider request failed") from None


class EmbeddingClientFactory:
    """Builds a provider client bound to a specific API key.

    Keys can come from the environment or from a per-user stored key, so
    clients are constructed per call rather than once at startup.
    """

    def __init__(self, *, http_client: httpx.AsyncClient, settings: Settings | None = None) -> None:
        self._http_client = http_client
        self._settings = settings

    def client_for(self, provider_id: str, api_key: str):
        if provider_id == "openai":
            return OpenAIEmbeddingClient(
                api_key=api_key, http_client=self._http_client, provider_id="openai"
            )
        if provider_id == "gemini":
            return GeminiEmbeddingClient(
                api_key=api_key, http_client=self._http_client, provider_id="gemini"
            )
        if provider_id == "openrouter":
            if self._settings is None or not provider_is_configured(provider_id, self._settings):
                raise EmbeddingProviderError("OpenRouter embedding routing policy is unavailable")
            return OpenAIEmbeddingClient(
                api_key=api_key,
                http_client=self._http_client,
                base_url="https://openrouter.ai/api/v1",
                provider_id="openrouter",
                provider_preferences={
                    "order": [self._settings.openrouter_embedding_provider],
                    "only": [self._settings.openrouter_embedding_provider],
                    "data_collection": "deny",
                    "allow_fallbacks": False,
                    "require_parameters": True,
                },
            )
        raise EmbeddingProviderError(f"Unknown embedding provider: {provider_id}")
