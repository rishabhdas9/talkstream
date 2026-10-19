from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class EmbeddingModelDefinition:
    id: str
    label: str
    dimensions: int
    default: bool = False


@dataclass(frozen=True)
class EmbeddingProviderDefinition:
    id: str
    label: str
    models: tuple[EmbeddingModelDefinition, ...]


OPENAI_PROVIDER = EmbeddingProviderDefinition(
    id="openai",
    label="OpenAI",
    models=(
        EmbeddingModelDefinition(
            id="text-embedding-3-small",
            label="Text Embedding 3 Small",
            dimensions=1536,
            default=True,
        ),
        EmbeddingModelDefinition(
            id="text-embedding-3-large",
            label="Text Embedding 3 Large",
            dimensions=3072,
        ),
    ),
)

GEMINI_PROVIDER = EmbeddingProviderDefinition(
    id="gemini",
    label="Google Gemini",
    models=(
        EmbeddingModelDefinition(
            id="gemini-embedding-2",
            label="Gemini Embedding 2",
            dimensions=3072,
            default=True,
        ),
    ),
)

OPENROUTER_PROVIDER = EmbeddingProviderDefinition(
    id="openrouter",
    label="OpenRouter",
    models=(
        EmbeddingModelDefinition(
            id="qwen/qwen3-embedding-8b",
            label="Qwen3 Embedding 8B",
            dimensions=4096,
            default=True,
        ),
    ),
)

# Exact endpoint tags from OpenRouter's public model endpoints API.
OPENROUTER_EMBEDDING_ENDPOINTS = frozenset({"nebius", "deepinfra/us", "siliconflow/fp8"})
PROVIDERS = (OPENAI_PROVIDER, GEMINI_PROVIDER, OPENROUTER_PROVIDER)


def env_key_for(provider_id: str, settings: Settings) -> str | None:
    if provider_id == "openai":
        return settings.openai_api_key_value
    if provider_id == "gemini":
        return settings.gemini_api_key_value
    if provider_id == "openrouter":
        return settings.openrouter_api_key_value
    return None


def provider_is_configured(provider_id: str, settings: Settings) -> bool:
    if provider_id == "openrouter":
        return (
            settings.openrouter_embedding_provider in OPENROUTER_EMBEDDING_ENDPOINTS
            and settings.openrouter_embedding_data_collection == "deny"
        )
    return provider_id in {"openai", "gemini"}


def provider_is_available(provider_id: str, settings: Settings) -> bool:
    return (
        provider_is_configured(provider_id, settings)
        and env_key_for(provider_id, settings) is not None
    )


def get_model_definition(provider_id: str, model_id: str) -> EmbeddingModelDefinition | None:
    for provider in PROVIDERS:
        if provider.id != provider_id:
            continue
        return next((model for model in provider.models if model.id == model_id), None)
    return None
