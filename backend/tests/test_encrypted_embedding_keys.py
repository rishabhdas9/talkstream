from __future__ import annotations

import traceback

import httpx
import pytest
from conftest import RecordingOtpSender
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import text
from test_knowledge_provider_keys import _login
from test_runtime_policy import public_values as public_values

from app.config import Settings
from app.knowledge.crypto import CredentialCipherError
from app.knowledge.service import KnowledgeService
from app.main import create_app
from app.models import User


def service(database, settings, key):
    return KnowledgeService(
        database=database,
        settings=settings.model_copy(update={"credential_encryption_key": key}),
        embedding_client=None,
    )


async def test_saved_keys_are_ciphertext_owned_and_updatable(database, settings):
    async with database.session_factory() as session:
        session.add_all([User(id="u", email="u@example.com"), User(id="v", email="v@example.com")])
        await session.commit()
    keys = service(database, settings, Fernet.generate_key().decode())
    await keys.set_provider_key("u", "openai", "first-secret-value")
    await keys.set_provider_key("u", "openai", "updated-secret-value")
    await keys.set_provider_key("v", "openai", "other-secret-value")
    async with database.engine.connect() as conn:
        rows = (
            (await conn.execute(text("SELECT key_ciphertext FROM embedding_provider_keys")))
            .scalars()
            .all()
        )
    assert len(rows) == 2
    assert all(row.startswith("fernet:v1:") for row in rows)
    assert not any(
        secret in repr(rows) for secret in ("first-secret", "updated-secret", "other-secret")
    )
    assert await keys.get_provider_key("u", "openai") == "updated-secret-value"
    assert await keys.provider_keys("v") == {"openai": "other-secret-value"}
    await keys.delete_provider_key("v", "openai")
    assert await keys.get_provider_key("v", "openai") is None
    assert await keys.get_provider_key("u", "openai") == "updated-secret-value"


@pytest.mark.parametrize("key", [None, "invalid-master-secret"])
async def test_missing_invalid_master_refuses_write_without_secret(database, settings, key):
    keys = service(database, settings, key)
    assert await keys.provider_keys("u") == {}
    assert await keys.get_provider_key("u", "openai") is None
    private_value = "private-provider-secret"
    with pytest.raises(CredentialCipherError) as caught:
        await keys.set_provider_key("u", "openai", private_value)
    trace = "".join(traceback.format_exception(caught.value))
    assert "private-provider-secret" not in trace
    assert "invalid-master-secret" not in trace
    async with database.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT COUNT(*) FROM embedding_provider_keys"))
        ).scalar_one() == 0


async def test_wrong_master_refuses_decrypt(database, settings):
    async with database.session_factory() as session:
        session.add(User(id="u", email="u@example.com"))
        await session.commit()
    await service(database, settings, Fernet.generate_key().decode()).set_provider_key(
        "u", "openai", "test-only-private"
    )
    wrong = service(database, settings, Fernet.generate_key().decode())
    for operation in (wrong.get_provider_key("u", "openai"), wrong.provider_keys("u")):
        with pytest.raises(CredentialCipherError, match="decryption failed"):
            await operation


def test_public_master_must_be_real_fernet_key(public_values):
    public_values["credential_encryption_key"] = "long-but-invalid-master-key-value" * 2
    with pytest.raises(ValidationError, match="encryption key"):
        Settings(_env_file=None, **public_values)


async def test_key_api_missing_master_has_safe_configuration_error(settings, caplog):
    app = create_app(settings.model_copy(update={"knowledge_rag_enabled": True}))
    async with app.router.lifespan_context(app):
        sender = RecordingOtpSender()
        app.state.email_sender = sender
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            token = await _login(client, sender)
            response = await client.put(
                "/knowledge/providers/openai/key",
                headers={"Authorization": f"Bearer {token}"},
                json={"api_key": "test-only-private-secret"},
            )
            assert response.status_code == 409
            assert response.json()["detail"] == "Stored provider credentials are unavailable"
            assert "test-only-private-secret" not in response.text + caplog.text


@pytest.mark.parametrize("replacement_key", [None, Fernet.generate_key().decode()])
async def test_profile_and_catalog_routes_fail_safely_with_unreadable_keys(
    settings, replacement_key, caplog
):
    from types import SimpleNamespace

    from app.auth.dependencies import get_current_user

    enabled = settings.model_copy(
        update={
            "knowledge_rag_enabled": True,
            "openai_api_key": None,
            "credential_encryption_key": Fernet.generate_key().decode(),
        }
    )
    app = create_app(enabled)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u")
    async with app.router.lifespan_context(app):
        async with app.state.database.session_factory() as session:
            session.add(User(id="u", email="u@example.com"))
            await session.commit()
        service = app.state.knowledge_service
        secret = "test-only-private-provider-key"
        await service.set_provider_key("u", "openai", secret)
        service.settings = enabled.model_copy(update={"credential_encryption_key": replacement_key})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            responses = [
                await client.get("/knowledge/providers"),
                await client.get("/knowledge/profile"),
                await client.put(
                    "/knowledge/profile",
                    json={"provider_id": "openai", "model_id": "text-embedding-3-small"},
                ),
            ]
        assert [response.status_code for response in responses] == [409, 409, 409]
        assert secret not in "".join(response.text for response in responses) + caplog.text
