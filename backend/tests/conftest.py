from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

os.environ.setdefault(
    "JWT_SECRET",
    "test-secret-that-is-at-least-thirty-two-characters",
)

from app.config import Settings
from app.db import Database
from app.main import create_app


@dataclass
class RecordingOtpSender:
    deliveries: list[dict[str, str]] = field(default_factory=list)

    async def send_otp(self, *, email: str, code: str, challenge_id: str) -> None:
        self.deliveries.append({"email": email, "code": code, "challenge_id": challenge_id})


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # A developer's live backend/.env must never reach tests.
        jwt_secret="test-secret-that-is-at-least-thirty-two-characters",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        sarvam_api_key=None,
        exa_api_key=None,
        resend_api_key=None,
        billing_tick_seconds=1,
        price_per_minute_paise=200,
        max_recharge_paise=1000,
    )


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings.database_url)
    await database.create_all()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def db_session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session_factory() as session:
        yield session


@pytest.fixture
async def api_client(
    settings: Settings,
) -> AsyncIterator[tuple[httpx.AsyncClient, RecordingOtpSender]]:
    app = create_app(settings)
    sender = RecordingOtpSender()
    async with app.router.lifespan_context(app):
        app.state.email_sender = sender
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, sender
