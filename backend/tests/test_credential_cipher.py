from __future__ import annotations

import base64
import shutil
import traceback

import pytest
from sqlalchemy import text
from test_migrations import fresh_db as fresh_db
from test_migrations import migration_module

from app.knowledge.crypto import CredentialCipher, CredentialCipherError

MASTER_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")


@pytest.mark.parametrize("secret", ["test-only-secret", "  exact\tcredential\n", "凭证🔑", ""])
def test_roundtrip_is_randomized_and_preserves_exact_credentials(secret):
    cipher = CredentialCipher(MASTER_KEY)
    first = cipher.encrypt(secret)
    second = cipher.encrypt(secret)
    assert first.startswith("fernet:v1:")
    assert first != second
    if secret:
        assert secret not in first
    assert cipher.decrypt(first) == secret
    assert cipher.decrypt(second) == secret


@pytest.mark.parametrize(
    "key", [None, "", "invalid-sensitive-key", MASTER_KEY + "\n", "☃", "A" * 44]
)
def test_invalid_master_keys_raise_safe_errors(key):
    with pytest.raises(CredentialCipherError, match="^Credential encryption key is invalid\\.$"):
        CredentialCipher(key)


@pytest.mark.parametrize(
    "token", ["plaintext-secret", "fernet:v2:secret", "fernet:v1:", "fernet:v1:☃", None]
)
def test_invalid_ciphertext_cannot_fall_back_to_plaintext(token):
    with pytest.raises(CredentialCipherError) as caught:
        CredentialCipher(MASTER_KEY).decrypt(token)
    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) in {
        "Credential ciphertext format is invalid.",
        "Credential decryption failed.",
    }
    if token:
        assert token not in rendered


def test_tampering_and_wrong_key_are_rejected_without_secret_errors(caplog):
    cipher = CredentialCipher(MASTER_KEY)
    token = cipher.encrypt("sensitive-plaintext")
    replacement = "A" if token[-10] != "A" else "B"
    tampered = token[:-10] + replacement + token[-9:]
    wrong_key = base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode("ascii")
    for decoder, ciphertext in [(cipher, tampered), (CredentialCipher(wrong_key), token)]:
        with pytest.raises(
            CredentialCipherError, match="^Credential decryption failed\\.$"
        ) as caught:
            decoder.decrypt(ciphertext)
        rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
        assert "sensitive-plaintext" not in rendered
        assert ciphertext not in rendered
        assert MASTER_KEY not in rendered
        assert wrong_key not in rendered


async def test_empty_database_upgrades_to_ciphertext_column(fresh_db):
    await migration_module().apply_migrations(fresh_db)
    async with fresh_db.engine.connect() as connection:
        columns = await connection.exec_driver_sql("PRAGMA table_info(embedding_provider_keys)")
        names = {column[1] for column in columns}
        assert "key_ciphertext" in names
        assert "api_key" not in names


async def test_plaintext_rows_refuse_upgrade_and_leave_database_unchanged(
    fresh_db, tmp_path, monkeypatch, caplog
):
    migrations = migration_module()
    source_directory = migrations.MIGRATION_DIR
    prior_directory = tmp_path / "prior_sql"
    prior_directory.mkdir()
    for path in source_directory.glob("*.sql"):
        if int(path.name[:3]) <= 7:
            shutil.copy(path, prior_directory)
    monkeypatch.setattr(migrations, "MIGRATION_DIR", prior_directory)
    await migrations.apply_migrations(fresh_db)
    secret = "plaintext-must-not-be-logged"
    async with fresh_db.engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO users VALUES ('u', 'owner@example.com', '2026-10-18', 1)")
        )
        await connection.execute(
            text(
                "INSERT INTO embedding_provider_keys "
                "(id, user_id, provider_id, api_key, created_at, updated_at) "
                "VALUES ('k', 'u', 'openai', :secret, '2026-10-18', '2026-10-18')"
            ),
            {"secret": secret},
        )
        before = (await connection.execute(text("SELECT * FROM schema_migrations"))).all()
    shutil.copy(source_directory / "008_key_encryption.sql", prior_directory)
    with pytest.raises(migrations.MigrationError, match="Schema migration failed") as caught:
        await migrations.apply_migrations(fresh_db)
    assert secret not in "".join(traceback.format_exception(caught.value)) + caplog.text
    assert "plaintext_embedding_keys_require_explicit_removal" in str(caught.value.__cause__)
    async with fresh_db.engine.connect() as connection:
        assert (await connection.execute(text("SELECT * FROM schema_migrations"))).all() == before
        assert (
            await connection.execute(text("SELECT api_key FROM embedding_provider_keys"))
        ).scalar_one() == secret
        names = (await connection.execute(text("SELECT name FROM sqlite_master"))).scalars().all()
        assert "_embedding_key_encryption_guard" not in names
