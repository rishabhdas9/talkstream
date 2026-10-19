from __future__ import annotations

import json

import httpx
import pytest

from app.knowledge import providers
from app.knowledge.embeddings import EmbeddingClientFactory, EmbeddingProviderError
from app.knowledge.providers import (
    env_key_for,
    get_model_definition,
    provider_is_available,
)


def configured(settings, **updates):
    return settings.model_copy(
        update={
            "openrouter_api_key": "secret-openrouter-key",
            "openrouter_embedding_provider": "nebius",
            "openrouter_embedding_data_collection": "deny",
            **updates,
        }
    )


async def test_openrouter_pins_route_privacy_model_dimensions_and_endpoint(settings) -> None:
    def handler(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/embeddings"
        assert request.headers["authorization"] == "Bearer secret-key"
        assert json.loads(request.content) == {
            "model": "qwen/qwen3-embedding-8b",
            "input": ["fixture"],
            "dimensions": 4096,
            "encoding_format": "float",
            "provider": {
                "order": ["nebius"],
                "only": ["nebius"],
                "data_collection": "deny",
                "allow_fallbacks": False,
                "require_parameters": True,
            },
        }
        return httpx.Response(
            200,
            json={
                "model": "qwen/qwen3-embedding-8b",
                "data": [{"index": 0, "embedding": [0.5] * 4096}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = EmbeddingClientFactory(
            http_client=http_client, settings=configured(settings)
        ).client_for("openrouter", "secret-key")
        assert await client.embed(
            ["fixture"], model_id="qwen/qwen3-embedding-8b", expected_dimensions=4096
        ) == [[0.5] * 4096]


@pytest.mark.parametrize(
    "updates",
    [
        {"openrouter_embedding_provider": None},
        {"openrouter_embedding_provider": "deepinfra"},
        {"openrouter_embedding_provider": "unknown"},
        {"openrouter_embedding_data_collection": None},
        {"openrouter_embedding_data_collection": "allow"},
    ],
)
async def test_openrouter_requires_exact_provider_pin_and_explicit_privacy(
    settings, updates
) -> None:
    policy = configured(settings, **updates)
    assert not providers.provider_is_configured("openrouter", policy)
    assert not provider_is_available("openrouter", policy)
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(EmbeddingProviderError):
            EmbeddingClientFactory(http_client=http_client, settings=policy).client_for(
                "openrouter", "secret"
            )


async def test_openrouter_factory_without_settings_fails_closed() -> None:
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(EmbeddingProviderError):
            EmbeddingClientFactory(http_client=http_client).client_for("openrouter", "secret")


@pytest.mark.parametrize(
    "model_id,dimensions",
    [
        ("openai/text-embedding-3-small", 4096),
        ("qwen/qwen3-embedding-8b", 1536),
    ],
)
async def test_openrouter_rejects_unlisted_model_and_dimensions(settings, model_id, dimensions):
    def unexpected_request(request):
        pytest.fail("Unsupported model must not send credentials")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as http_client:
        client = EmbeddingClientFactory(
            http_client=http_client, settings=configured(settings)
        ).client_for("openrouter", "secret")
        with pytest.raises(EmbeddingProviderError):
            await client.embed(["fixture"], model_id=model_id, expected_dimensions=dimensions)


@pytest.mark.parametrize("provider_id", ["openai", "gemini", "openrouter"])
async def test_factory_clients_never_follow_redirects(settings, provider_id) -> None:
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://untrusted.invalid/steal"})

    models = {
        "openai": ("text-embedding-3-small", 1536),
        "gemini": ("gemini-embedding-2", 3072),
        "openrouter": ("qwen/qwen3-embedding-8b", 4096),
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http_client:
        client = EmbeddingClientFactory(
            http_client=http_client, settings=configured(settings)
        ).client_for(provider_id, "secret")
        model_id, dimensions = models[provider_id]
        with pytest.raises(EmbeddingProviderError):
            await client.embed(["fixture"], model_id=model_id, expected_dimensions=dimensions)
    assert len(seen) == 1
    assert "untrusted.invalid" not in seen[0]


def test_openrouter_catalog_and_environment_key(settings) -> None:
    policy = configured(settings)
    assert get_model_definition("gemini", "text-embedding-004") is None
    assert get_model_definition("gemini", "gemini-embedding-2").dimensions == 3072
    assert get_model_definition("openrouter", "qwen/qwen3-embedding-8b").dimensions == 4096
    assert env_key_for("openrouter", policy) == "secret-openrouter-key"
    assert provider_is_available("openrouter", policy)
    assert not providers.provider_is_configured("unknown", policy)
