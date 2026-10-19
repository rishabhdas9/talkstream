from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from app.db import Database

MIGRATION_DIR = Path(__file__).with_name("sql")


class MigrationError(RuntimeError):
    pass


def _statements(source: str) -> list[str]:
    statements = []
    pending = ""
    for character in source:
        pending += character
        if character == ";" and sqlite3.complete_statement(pending):
            head = re.sub(r"--[^\n]*|/\*.*?\*/", "", pending, flags=re.DOTALL).strip()
            first = head.split(maxsplit=1)[0].upper().rstrip(";") if head else ""
            if first in {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
                raise MigrationError("Migration files cannot control their own transaction")
            statements.append(pending)
            pending = ""
    if re.sub(r"--[^\n]*|/\*.*?\*/", "", pending, flags=re.DOTALL).strip():
        raise MigrationError("Migration contains an incomplete SQL statement")
    return statements


async def apply_migrations(database: Database) -> None:
    if database.engine.dialect.name != "sqlite":
        raise MigrationError("Talkstream requires SQLite")
    migrations = {}
    for path in sorted(MIGRATION_DIR.glob("*.sql")):
        match = re.fullmatch(r"([0-9]{3})_[a-z0-9_]+\.sql", path.name)
        if match is None or int(match[1]) in migrations:
            raise MigrationError("Invalid or duplicate migration version")
        content = path.read_bytes()
        migrations[int(match[1])] = (
            hashlib.sha256(content).hexdigest(),
            _statements(content.decode("utf-8")),
        )
    if not migrations or sorted(migrations) != list(range(1, max(migrations) + 1)):
        raise MigrationError("Migration history is missing a version")

    async with database.engine.connect() as connection:
        try:
            # SQLite's legacy driver mode otherwise autocommits DDL before rollback.
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            tables = set(
                (
                    await connection.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                ).scalars()
            )
            if "schema_migrations" not in tables and tables - {"sqlite_sequence"}:
                raise MigrationError("Refusing to import an unversioned existing database")
            await connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            applied = dict(
                (
                    await connection.execute(
                        text("SELECT version, checksum FROM schema_migrations")
                    )
                ).all()
            )
            for version, checksum in applied.items():
                if version not in migrations:
                    raise MigrationError(f"Applied migration {version} is missing")
                if migrations[version][0] != checksum:
                    raise MigrationError(f"Migration {version} checksum mismatch")
            for version, (checksum, statements) in migrations.items():
                if version in applied:
                    continue
                for statement in statements:
                    await connection.exec_driver_sql(statement)
                await connection.execute(
                    text(
                        "INSERT INTO schema_migrations (version, checksum, applied_at) "
                        "VALUES (:version, :checksum, :applied_at)"
                    ),
                    {
                        "version": version,
                        "checksum": checksum,
                        "applied_at": datetime.now(UTC).isoformat(),
                    },
                )
            await connection.commit()
        except BaseException as exc:
            await connection.rollback()
            if isinstance(exc, MigrationError | KeyboardInterrupt | SystemExit):
                raise
            if not isinstance(exc, Exception):
                raise
            raise MigrationError("Schema migration failed; startup refused") from exc
