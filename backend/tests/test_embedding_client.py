from __future__ import annotations

import json

import httpx
import pytest

from app.knowledge.embeddings import (
    EmbeddingClientFactory,
    EmbeddingProviderError,
    GeminiEmbeddingClient,
    OpenAIEmbeddingClient,
)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "secret provider detail"}}),
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),
        httpx.Response(
            200,
            content=b'{"data":[{"index":0,"embedding":[NaN,NaN,NaN]}]}',
            headers={"content-type": "application/json"},
        ),
        httpx.Response(200, json={"data": ["not-an-object"]}),
    ],
)
async def test_embedding_failures_are_validated_and_sanitized(response: httpx.Response) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as http_client:
        client = OpenAIEmbeddingClient(api_key="secret-key", http_client=http_client)
        with pytest.raises(EmbeddingProviderError) as raised:
            await client.embed(["fixture"], model_id="model", expected_dimensions=3)

    message = str(raised.value)
    assert "secret-key" not in message
    assert "secret provider detail" not in message


@pytest.mark.parametrize("index", [False, True, 0.0, "0", -1, None])
async def test_indices_must_be_exact_nonnegative_integers(index) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"data": [{"index": index, "embedding": [1, 2, 3]}]}
            )
        )
    ) as http_client:
        with pytest.raises(EmbeddingProviderError):
            await OpenAIEmbeddingClient(api_key="secret", http_client=http_client).embed(
                ["fixture"], model_id="fixture", expected_dimensions=3
            )


@pytest.mark.parametrize("indices", [[0, 0], [0, 2], [0, -1]])
async def test_indices_must_cover_the_entire_batch_once(indices) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"data": [{"index": index, "embedding": [1]} for index in indices]}
            )
        )
    ) as http_client:
        with pytest.raises(EmbeddingProviderError):
            await OpenAIEmbeddingClient(api_key="secret", http_client=http_client).embed(
                ["first", "second"], model_id="fixture", expected_dimensions=1
            )


@pytest.mark.parametrize("client_class", [OpenAIEmbeddingClient, GeminiEmbeddingClient])
@pytest.mark.parametrize("value", ["1.0", True, False, None, {}, [], float("inf"), 10**500])
async def test_vector_elements_must_be_finite_numbers(client_class, value) -> None:
    payload = (
        {"data": [{"index": 0, "embedding": [value]}]}
        if client_class is OpenAIEmbeddingClient
        else {"embeddings": [{"values": [value]}]}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=json.dumps(payload).encode())
        )
    ) as http_client:
        with pytest.raises(EmbeddingProviderError):
            await client_class(api_key="secret", http_client=http_client).embed(
                ["fixture"], model_id="fixture", expected_dimensions=1
            )


@pytest.mark.parametrize("client_class", [OpenAIEmbeddingClient, GeminiEmbeddingClient])
@pytest.mark.parametrize("payload", [[], None, 1, "invalid"])
async def test_non_object_responses_are_sanitized(client_class, payload) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http_client:
        with pytest.raises(EmbeddingProviderError):
            await client_class(api_key="secret", http_client=http_client).embed(
                ["fixture"], model_id="fixture", expected_dimensions=1
            )


async def test_openai_returns_vectors_in_input_order() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [2, 0.5]},
                        {"index": 0, "embedding": [1, -0.5]},
                    ]
                },
            )
        )
    ) as http_client:
        vectors = await OpenAIEmbeddingClient(api_key="secret", http_client=http_client).embed(
            ["first", "second"], model_id="fixture", expected_dimensions=2
        )
    assert vectors == [[1.0, -0.5], [2.0, 0.5]]


@pytest.mark.parametrize(
    "provider_id,model_id,dimensions",
    [
        ("openai", "unknown", 1536),
        ("openai", "text-embedding-3-small", 3),
        ("gemini", "text-embedding-004", 768),
        ("gemini", "../secret", 3072),
        ("gemini", "gemini-embedding-2", 3),
    ],
)
async def test_factory_rejects_unlisted_models_and_dimensions_without_http(
    provider_id, model_id, dimensions
) -> None:
    def unexpected_request(request):
        pytest.fail("An unsupported model must not send credentials")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as http_client:
        client = EmbeddingClientFactory(http_client=http_client).client_for(provider_id, "secret")
        with pytest.raises(EmbeddingProviderError):
            await client.embed(["fixture"], model_id=model_id, expected_dimensions=dimensions)


async def test_gemini_current_model_requests_separate_fixed_dimension_vectors() -> None:
    def handler(request):
        assert str(request.url) == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-embedding-2:batchEmbedContents"
        )
        assert request.headers["x-goog-api-key"] == "secret"
        assert json.loads(request.content) == {
            "requests": [
                {
                    "model": "models/gemini-embedding-2",
                    "content": {"parts": [{"text": text}]},
                    "outputDimensionality": 3072,
                }
                for text in ["first", "second"]
            ]
        }
        return httpx.Response(
            200, json={"embeddings": [{"values": [1] * 3072}, {"values": [2] * 3072}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = EmbeddingClientFactory(http_client=http_client).client_for("gemini", "secret")
        vectors = await client.embed(
            ["first", "second"], model_id="gemini-embedding-2", expected_dimensions=3072
        )
    assert vectors == [[1.0] * 3072, [2.0] * 3072]


@pytest.mark.parametrize("client_class", [OpenAIEmbeddingClient, GeminiEmbeddingClient])
@pytest.mark.parametrize(
    "prompt_tokens,expected",
    [(0, [0]), (7, [7]), (True, []), (False, []), (-1, []), (1.5, []), ("7", []), (None, [])],
)
async def test_usage_callback_only_reports_strict_input_counts_before_vector_validation(
    client_class, prompt_tokens, expected
) -> None:
    usage = []
    payload = (
        {"usage": {"prompt_tokens": prompt_tokens}, "data": []}
        if client_class is OpenAIEmbeddingClient
        else {"usageMetadata": {"promptTokenCount": prompt_tokens}, "embeddings": []}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http_client:
        with pytest.raises(EmbeddingProviderError):
            await client_class(api_key="secret", http_client=http_client).embed(
                ["fixture"], model_id="fixture", expected_dimensions=1, usage_callback=usage.append
            )
    assert usage == expected


@pytest.mark.parametrize("client_class", [OpenAIEmbeddingClient, GeminiEmbeddingClient])
async def test_missing_usage_is_never_fabricated(client_class) -> None:
    usage = []
    payload = (
        {"data": [{"index": 0, "embedding": [1]}]}
        if client_class is OpenAIEmbeddingClient
        else {"embeddings": [{"values": [1]}]}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http_client:
        assert await client_class(api_key="secret", http_client=http_client).embed(
            ["fixture"], model_id="fixture", expected_dimensions=1, usage_callback=usage.append
        ) == [[1.0]]
    assert usage == []


@pytest.mark.parametrize("client_class", [OpenAIEmbeddingClient, GeminiEmbeddingClient])
async def test_provider_errors_suppress_raw_http_traceback_context(client_class) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "secret provider detail"})
        )
    ) as http_client:
        with pytest.raises(EmbeddingProviderError) as raised:
            await client_class(api_key="secret", http_client=http_client).embed(
                ["fixture"], model_id="fixture", expected_dimensions=1
            )
    assert raised.value.__suppress_context__ is True
    assert raised.value.__cause__ is None
