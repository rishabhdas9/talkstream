"""Authentication exchange shared by the command-line voice probes."""

from urllib.parse import urlsplit, urlunsplit

import httpx


async def request_voice_ticket(ws_url: str, token: str, origin: str) -> str:
    parts = urlsplit(ws_url)
    if parts.scheme not in {"ws", "wss"} or parts.username or parts.password:
        raise ValueError("A ws/wss URL without embedded credentials is required")
    endpoint = urlunsplit(
        ("https" if parts.scheme == "wss" else "http", parts.netloc, "/auth/voice-ticket", "", "")
    )
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            endpoint, headers={"Authorization": f"Bearer {token}", "Origin": origin}
        )
        response.raise_for_status()
        return response.json()["ticket"]
