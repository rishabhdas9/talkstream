from __future__ import annotations

import logging
from typing import Protocol

import httpx


class OtpDeliveryError(RuntimeError):
    pass


class OtpEmailSender(Protocol):
    async def send_otp(self, *, email: str, code: str, challenge_id: str) -> None: ...


class ConsoleOtpSender:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("uvicorn.error")

    async def send_otp(self, *, email: str, code: str, challenge_id: str) -> None:
        self._logger.warning(
            "Development OTP for %s: %s (challenge_id=%s)", email, code, challenge_id
        )


class ResendOtpSender:
    def __init__(
        self,
        *,
        api_key: str,
        sender: str,
        client: httpx.AsyncClient,
        api_base_url: str = "https://api.resend.com",
    ) -> None:
        self._api_key = api_key
        self._sender = sender
        self._client = client
        self._api_base_url = api_base_url.rstrip("/")

    async def send_otp(self, *, email: str, code: str, challenge_id: str) -> None:
        try:
            response = await self._client.post(
                f"{self._api_base_url}/emails",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Idempotency-Key": f"otp/{challenge_id}",
                },
                json={
                    "from": self._sender,
                    "to": [email],
                    "subject": "Your Talkstream verification code",
                    "text": f"Your Talkstream verification code is {code}. It expires soon.",
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OtpDeliveryError("Resend could not deliver the OTP") from exc
