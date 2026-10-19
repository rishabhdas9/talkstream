import httpx
import pytest


async def test_probe_exchanges_jwt_for_ticket_without_putting_it_in_url(monkeypatch):
    import app.auth.probe as probe

    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"ticket": "opaque-ticket"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    assert (
        await probe.request_voice_ticket(
            "wss://talkstream.example/ws/voice", "private-jwt", "https://demo.example"
        )
        == "opaque-ticket"
    )
    request = calls[0]
    assert str(request.url) == "https://talkstream.example/auth/voice-ticket"
    assert request.headers["authorization"] == "Bearer private-jwt"
    assert request.headers["origin"] == "https://demo.example"


async def test_probe_does_not_open_socket_on_failed_exchange(monkeypatch):
    import app.auth.probe as probe

    async def handle(request):
        return httpx.Response(401)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await probe.request_voice_ticket(
            "ws://localhost:8000/ws/voice", "expired", "http://localhost:5173"
        )
