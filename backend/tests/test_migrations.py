from __future__ import annotations

import asyncio
import importlib
import importlib.util

import pytest
from sqlalchemy import text

from app.db import Base, Database


def migration_module():
    assert importlib.util.find_spec("app.migrations") is not None, "No migration runner"
    return importlib.import_module("app.migrations")


@pytest.fixture
async def fresh_db(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'migrations.db'}")
    try:
        yield db
    finally:
        await db.dispose()


async def test_migrations_are_repeatable_and_keep_data(fresh_db):
    migrations = migration_module()
    await migrations.apply_migrations(fresh_db)
    async with fresh_db.engine.begin() as connection:
        before = (await connection.execute(text("SELECT * FROM schema_migrations"))).all()
        await connection.execute(
            text("INSERT INTO users VALUES ('u', 'owner@example.com', '2026-10-18', 1)")
        )
    await migrations.apply_migrations(fresh_db)
    async with fresh_db.engine.connect() as connection:
        assert (await connection.execute(text("SELECT email FROM users"))).scalar_one() == (
            "owner@example.com"
        )
        versions = (await connection.execute(text("SELECT * FROM schema_migrations"))).all()
        assert versions == before


async def test_sql_schema_supports_all_retained_orm_columns(fresh_db):
    await migration_module().apply_migrations(fresh_db)
    async with fresh_db.engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            rows = await connection.exec_driver_sql(f'PRAGMA table_info("{table.name}")')
            assert {row[1] for row in rows} == set(table.columns.keys()), table.name


async def test_create_all_uses_migrations(fresh_db):
    await fresh_db.create_all()
    async with fresh_db.engine.connect() as connection:
        names = (await connection.execute(text("SELECT name FROM sqlite_master"))).scalars().all()
        assert "schema_migrations" in names


async def test_changed_applied_migration_is_rejected(fresh_db, tmp_path, monkeypatch):
    migrations = migration_module()
    directory = tmp_path / "sql"
    directory.mkdir()
    migration = directory / "001_initial.sql"
    migration.write_text("CREATE TABLE marker (id INTEGER PRIMARY KEY);\n")
    monkeypatch.setattr(migrations, "MIGRATION_DIR", directory)
    await migrations.apply_migrations(fresh_db)
    migration.write_text("CREATE TABLE changed (id INTEGER PRIMARY KEY);\n")
    with pytest.raises(migrations.MigrationError, match="checksum"):
        await migrations.apply_migrations(fresh_db)


async def test_failed_migration_rolls_back_schema_and_version(fresh_db, tmp_path, monkeypatch):
    migrations = migration_module()
    directory = tmp_path / "sql"
    directory.mkdir()
    (directory / "001_initial.sql").write_text(
        "CREATE TABLE marker (id INTEGER PRIMARY KEY);\nINSERT INTO absent VALUES (1);\n"
    )
    monkeypatch.setattr(migrations, "MIGRATION_DIR", directory)
    with pytest.raises(migrations.MigrationError):
        await migrations.apply_migrations(fresh_db)
    async with fresh_db.engine.connect() as connection:
        names = (await connection.execute(text("SELECT name FROM sqlite_master"))).scalars().all()
        assert "marker" not in names
        assert "schema_migrations" not in names


async def test_missing_applied_migration_is_rejected(fresh_db, tmp_path, monkeypatch):
    migrations = migration_module()
    await migrations.apply_migrations(fresh_db)
    empty_directory = tmp_path / "empty"
    empty_directory.mkdir()
    monkeypatch.setattr(migrations, "MIGRATION_DIR", empty_directory)
    with pytest.raises(migrations.MigrationError):
        await migrations.apply_migrations(fresh_db)


async def test_nonempty_unversioned_database_is_not_imported(fresh_db):
    async with fresh_db.engine.begin() as connection:
        await connection.execute(text("CREATE TABLE existing_user_data (value TEXT)"))
    migrations = migration_module()
    with pytest.raises(migrations.MigrationError, match="unversioned"):
        await migrations.apply_migrations(fresh_db)


async def test_concurrent_startups_apply_once(fresh_db):
    migrations = migration_module()
    await asyncio.gather(
        migrations.apply_migrations(fresh_db), migrations.apply_migrations(fresh_db)
    )
    async with fresh_db.engine.connect() as connection:
        count, unique_count = (
            await connection.execute(
                text("SELECT COUNT(*), COUNT(DISTINCT version) FROM schema_migrations")
            )
        ).one()
        assert count > 0 and count == unique_count


async def test_sql_literal_semicolon_is_not_a_statement_boundary(fresh_db, tmp_path, monkeypatch):
    migrations = migration_module()
    directory = tmp_path / "sql"
    directory.mkdir()
    (directory / "001_initial.sql").write_text(
        "CREATE TABLE marker (value TEXT);\nINSERT INTO marker VALUES ('one;two');\n"
    )
    monkeypatch.setattr(migrations, "MIGRATION_DIR", directory)
    await migrations.apply_migrations(fresh_db)
    async with fresh_db.engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT value FROM marker"))
        ).scalar_one() == "one;two"


async def test_migration_cannot_commit_a_partial_schema(fresh_db, tmp_path, monkeypatch):
    migrations = migration_module()
    directory = tmp_path / "sql"
    directory.mkdir()
    (directory / "001_initial.sql").write_text("CREATE TABLE marker (value TEXT);\nCOMMIT;\n")
    monkeypatch.setattr(migrations, "MIGRATION_DIR", directory)
    with pytest.raises(migrations.MigrationError, match="transaction"):
        await migrations.apply_migrations(fresh_db)
