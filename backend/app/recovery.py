from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.voice.lifecycle import DatabaseOwnedError, owner_lock

FORMAT = "talkstream-backup-v1"
BACKUP_RETENTION = timedelta(days=7)
JOURNAL_KINDS = {"knowledge_source", "provider_key"}


class RecoveryError(RuntimeError):
    pass


def _secret(value) -> str | None:
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.utcoffset() is None else parsed.astimezone(UTC)


class RecoveryJournal:
    """Append-only deletion tombstones kept outside database snapshots.

    The parent directory must already exist (an owner-controlled mount); a missing mount
    fails deletion instead of silently journaling to the local disk.
    """

    def __init__(self, path: Path):
        self.path = path
        path.touch(exist_ok=True)  # An existing empty journal means no deletions yet.

    def record(self, kind: str, user_id: str, record_id: str, deleted_at: datetime) -> None:
        if kind not in JOURNAL_KINDS:
            raise ValueError("Unknown journal record kind")
        line = json.dumps(
            {
                "kind": kind,
                "user_id": user_id,
                "id": record_id,
                "deleted_at": deleted_at.isoformat(),
            },
            sort_keys=True,
        )
        with self.path.open("a", encoding="utf-8") as journal:
            journal.write(line + "\n")
            journal.flush()
            os.fsync(journal.fileno())


@dataclass(frozen=True)
class BackupManifest:
    format: str
    created_at: str
    ciphertext: str
    salt: str
    nonce: str
    plaintext_sha256: str
    journal_entries: int
    journal_sha256: str
    path: Path | None = None

    def bound_fields(self) -> bytes:
        """Every manifest field is authenticated as AES-GCM associated data."""
        fields = {key: value for key, value in asdict(self).items() if key != "path"}
        return json.dumps(fields, sort_keys=True).encode()


def _cipher(key: str, salt: bytes) -> AESGCM:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=FORMAT.encode()).derive(
        key.encode()
    )
    return AESGCM(derived)


def _journal_lines(journal: Path) -> list[bytes]:
    if not journal.is_file():
        raise RecoveryError("Deletion journal is unavailable; refusing to continue")
    return journal.read_bytes().splitlines(keepends=True)


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_backup(
    destination: Path,
    *,
    database: Path,
    journal: Path,
    key: str,
    credential_key=None,
    now: datetime,
) -> BackupManifest:
    """Encrypt a consistent SQLite snapshot and prune backups older than seven days."""
    if not key or len(key) < 32:
        raise RecoveryError("Backup encryption key must contain at least 32 characters")
    if key == _secret(credential_key):
        raise RecoveryError("Backup and credential encryption keys must be separate")
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    snapshot = sqlite3.connect(":memory:")
    try:
        source.backup(snapshot)  # SQLite's online backup API, never a raw copy of a live file
        plaintext = snapshot.serialize()
    finally:
        snapshot.close()
        source.close()
    # Measured after the snapshot: a restore must replay at least these tombstones.
    lines = _journal_lines(journal)
    stamp = now.astimezone(UTC)
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    name = f"talkstream-{stamp:%Y%m%dT%H%M%S%fZ}-{secrets.token_hex(4)}"
    manifest = BackupManifest(
        format=FORMAT,
        created_at=stamp.isoformat(),
        ciphertext=f"{name}.db.enc",
        salt=base64.b64encode(salt).decode(),
        nonce=base64.b64encode(nonce).decode(),
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
        journal_entries=len(lines),
        journal_sha256=hashlib.sha256(b"".join(lines)).hexdigest(),
    )
    ciphertext = _cipher(key, salt).encrypt(nonce, plaintext, manifest.bound_fields())
    destination.mkdir(parents=True, exist_ok=True)
    _atomic_write(destination / manifest.ciphertext, ciphertext)
    manifest_path = destination / f"{name}.json"
    _atomic_write(manifest_path, manifest.bound_fields())
    for older in destination.glob("talkstream-*.json"):
        try:
            previous = json.loads(older.read_text())
            if _utc(previous["created_at"]) < stamp - BACKUP_RETENTION:
                (destination / Path(previous["ciphertext"]).name).unlink(missing_ok=True)
                older.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            continue  # Unreadable files are left for the operator, never deleted blindly.
    return BackupManifest(**{**asdict(manifest), "path": manifest_path})


def _load_manifest(path: Path) -> BackupManifest:
    try:
        manifest = BackupManifest(**json.loads(path.read_text()))
    except (OSError, ValueError, TypeError) as exc:
        raise RecoveryError("Backup manifest is unreadable") from exc
    if manifest.format != FORMAT or Path(manifest.ciphertext).name != manifest.ciphertext:
        raise RecoveryError("Backup manifest is invalid")
    return manifest


def _tombstones(lines: list[bytes]) -> list[dict]:
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
            valid = (
                set(entry) == {"kind", "user_id", "id", "deleted_at"}
                and entry["kind"] in JOURNAL_KINDS
                and all(isinstance(entry[field], str) for field in ("user_id", "id"))
            )
            entry["deleted_at"] = _utc(entry["deleted_at"])
        except (ValueError, TypeError, KeyError) as exc:
            raise RecoveryError("Deletion journal is corrupt") from exc
        if not valid:
            raise RecoveryError("Deletion journal is corrupt")
        entries.append(entry)
    return entries


def _replay(connection: sqlite3.Connection, entries: list[dict]) -> None:
    for entry in entries:
        if entry["kind"] == "knowledge_source":
            # Chunks cascade; ingestion admissions keep their counters with a null source.
            connection.execute(
                "DELETE FROM knowledge_sources WHERE id=? AND user_id=?",
                (entry["id"], entry["user_id"]),
            )
            continue
        keys = connection.execute(
            "SELECT id, updated_at FROM embedding_provider_keys WHERE user_id=? AND provider_id=?",
            (entry["user_id"], entry["id"]),
        ).fetchall()
        for key_id, updated_at in keys:
            # A key saved again after its deletion is a new, wanted credential.
            if _utc(updated_at) <= entry["deleted_at"]:
                connection.execute("DELETE FROM embedding_provider_keys WHERE id=?", (key_id,))


def restore_backup(manifest: Path, deletion_journal: Path, *, database: Path, key: str) -> None:
    """Replace the database with a verified backup after replaying every deletion tombstone.

    Nothing touches the target until decryption, integrity, journal replay and the
    paid-work hold have all succeeded on a private temporary copy.
    """
    loaded = _load_manifest(manifest)
    try:
        plaintext = _cipher(key, base64.b64decode(loaded.salt)).decrypt(
            base64.b64decode(loaded.nonce),
            (manifest.parent / loaded.ciphertext).read_bytes(),
            loaded.bound_fields(),
        )
    except (InvalidTag, OSError, ValueError) as exc:
        raise RecoveryError("Backup failed authentication or is unreadable") from exc
    if hashlib.sha256(plaintext).hexdigest() != loaded.plaintext_sha256:
        raise RecoveryError("Backup content digest does not match its manifest")
    lines = _journal_lines(deletion_journal)
    prefix = b"".join(lines[: loaded.journal_entries])
    if (
        len(lines) < loaded.journal_entries
        or hashlib.sha256(prefix).hexdigest() != loaded.journal_sha256
    ):
        raise RecoveryError("Deletion journal is older than the backup; refusing to restore")
    entries = _tombstones(lines)
    try:
        with owner_lock(database):
            _replace_database(database, plaintext, entries, loaded.created_at)
    except DatabaseOwnedError as exc:
        raise RecoveryError("Database is owned by a running Talkstream runtime") from exc


def _replace_database(
    database: Path, plaintext: bytes, entries: list[dict], created_at: str
) -> None:
    temporary = database.with_name(f".{database.name}.restore-{secrets.token_hex(4)}")
    try:
        _atomic_write(temporary, plaintext)
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RecoveryError("Restored database failed its integrity check")
            _replay(connection, entries)
            try:
                connection.execute(
                    "INSERT OR REPLACE INTO recovery_holds (id, reason, until_day, created_at) "
                    "VALUES ('restored_backup', ?, NULL, ?)",
                    (
                        f"Restored backup from {created_at}; reconcile provider spend",
                        datetime.now(UTC).isoformat(),
                    ),
                )
            except sqlite3.OperationalError as exc:
                raise RecoveryError("Backup predates paid-work recovery holds") from exc
            connection.commit()
        finally:
            connection.close()
        # Sidecars of the replaced file would be applied to the restored one.
        for suffix in ("-journal", "-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)
        os.replace(temporary, database)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
