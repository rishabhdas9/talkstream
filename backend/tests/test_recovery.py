from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from test_cost_service import clock as clock
from test_cost_service import rates as rates
from test_knowledge_sources_api import FakeEmbeddingClient, FakeVectorStore
from test_paid_boundaries import paid_boundary as paid_boundary
from test_session_policy import ALLOW, run_turn
from test_session_policy import session_policy as session_policy

from app.costs.models import CostAccountingError, CostScope, Usage, WorkBound
from app.costs.service import CostService
from app.db import Database
from app.knowledge.service import KnowledgeService
from app.models import (
    OtpChallenge,
    TurnMetric,
    UsageSession,
    User,
    WalletTransaction,
)
from app.recovery import RecoveryError, restore_backup, write_backup
from app.retention import purge_expired
from app.runtime import RuntimePolicy
from app.voice.lifecycle import database_owner

BACKUP_KEY = "backup-key-that-is-independent-of-credentials-0123"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
async def recovery_rig(database, settings, tmp_path):
    journal = tmp_path / "offhost" / "deletions.jsonl"
    journal.parent.mkdir()
    configured = settings.model_copy(
        update={
            "openai_api_key": "test-key",
            "credential_encryption_key": Fernet.generate_key().decode(),
            "recovery_journal_file": journal,
        }
    )
    db_path = Path(database.engine.url.database)
    state = SimpleNamespace(database=database)

    def service():
        return KnowledgeService(
            database=state.database,
            settings=configured,
            embedding_client=FakeEmbeddingClient(),
            vector_store=FakeVectorStore(),
        )

    async with database.session_factory() as session:
        user = User(email="recover@example.com", is_verified=True)
        session.add(user)
        await session.commit()
    knowledge = service()
    await knowledge.save_profile(user_id=user.id, provider_id="openai", model_id=MODEL)
    source = await knowledge.add_text_source(user_id=user.id, name="Diary", text="private diary")
    await knowledge.set_provider_key(user.id, "openai", "sk-user-owned-key")

    async def backup(when=NOW):
        return write_backup(
            tmp_path / "backups",
            database=db_path,
            journal=journal,
            key=BACKUP_KEY,
            credential_key=configured.credential_encryption_key,
            now=when,
        )

    async def restore(manifest, journal_path=journal, key=BACKUP_KEY):
        await state.database.dispose()
        try:
            restore_backup(manifest.path, journal_path, database=db_path, key=key)
        finally:
            state.database = Database(configured.database_url)

    async def source_visible():
        return any(item.id == source.id for item in await service().list_sources(user.id))

    async def stored_key_exists():
        return await service().get_provider_key(user.id, "openai") is not None

    rig = SimpleNamespace(
        state=state,
        service=service,
        user_id=user.id,
        source=source,
        journal=journal,
        db_path=db_path,
        backup=backup,
        restore=restore,
        source_visible=source_visible,
        stored_key_exists=stored_key_exists,
        tmp_path=tmp_path,
    )
    yield rig
    await state.database.dispose()


MODEL = "text-embedding-3-small"


async def delete_source_and_key(rig):
    knowledge = rig.service()
    await knowledge.delete_source(user_id=rig.user_id, source_id=rig.source.id)
    await knowledge.delete_provider_key(rig.user_id, "openai")


async def test_restore_does_not_revive_deleted_data(recovery_rig):
    snapshot = await recovery_rig.backup()
    await delete_source_and_key(recovery_rig)
    await recovery_rig.restore(snapshot)
    assert await recovery_rig.source_visible() is False
    assert await recovery_rig.stored_key_exists() is False
    async with recovery_rig.state.database.engine.connect() as connection:
        chunks = await connection.scalar(text("SELECT COUNT(*) FROM knowledge_chunks"))
    assert chunks == 0


async def test_key_saved_after_its_deletion_survives_replay(recovery_rig):
    knowledge = recovery_rig.service()
    await knowledge.delete_provider_key(recovery_rig.user_id, "openai")
    await knowledge.set_provider_key(recovery_rig.user_id, "openai", "sk-new-key")
    snapshot = await recovery_rig.backup()
    await recovery_rig.restore(snapshot)
    assert await recovery_rig.service().get_provider_key(recovery_rig.user_id, "openai") == (
        "sk-new-key"
    )


async def test_journal_is_written_before_deletion_is_acknowledged(recovery_rig):
    knowledge = recovery_rig.service()
    entries = recovery_rig.journal.read_text().splitlines()
    assert entries == []
    await delete_source_and_key(recovery_rig)
    recorded = [json.loads(line) for line in recovery_rig.journal.read_text().splitlines()]
    assert [(item["kind"], item["id"]) for item in recorded] == [
        ("knowledge_source", recovery_rig.source.id),
        ("provider_key", "openai"),
    ]
    assert all(set(item) == {"kind", "user_id", "id", "deleted_at"} for item in recorded)

    await knowledge.set_provider_key(recovery_rig.user_id, "openai", "sk-again")
    recovery_rig.journal.parent.rename(recovery_rig.tmp_path / "unmounted")
    with pytest.raises(OSError):
        await knowledge.delete_provider_key(recovery_rig.user_id, "openai")
    assert await knowledge.get_provider_key(recovery_rig.user_id, "openai") == "sk-again"


async def test_unwritable_journal_refuses_source_deletion(recovery_rig):
    knowledge = recovery_rig.service()
    recovery_rig.journal.parent.rename(recovery_rig.tmp_path / "unmounted")
    with pytest.raises(OSError):
        await knowledge.delete_source(
            user_id=recovery_rig.user_id, source_id=recovery_rig.source.id
        )
    with pytest.raises(OSError):
        recovery_rig.service()  # A missing journal mount also stops the service from starting.
    sources = await knowledge.list_sources(recovery_rig.user_id)
    assert [item.status.value for item in sources] == ["indexed"]


@pytest.mark.parametrize("journal_state", ["missing", "older"])
async def test_restore_fails_closed_without_current_journal(recovery_rig, journal_state):
    await delete_source_and_key(recovery_rig)
    snapshot = await recovery_rig.backup()
    before = recovery_rig.db_path.read_bytes()
    stale = recovery_rig.tmp_path / "stale.jsonl"
    if journal_state == "older":
        stale.write_text(recovery_rig.journal.read_text().splitlines(keepends=True)[0])
    with pytest.raises(RecoveryError, match="journal"):
        await recovery_rig.restore(snapshot, journal_path=stale)
    assert recovery_rig.db_path.read_bytes() == before


@pytest.mark.parametrize("tamper", ["ciphertext", "manifest", "wrong_key"])
async def test_tampered_or_wrong_key_backup_leaves_database_untouched(recovery_rig, tamper):
    snapshot = await recovery_rig.backup()
    await delete_source_and_key(recovery_rig)
    before = recovery_rig.db_path.read_bytes()
    key = BACKUP_KEY
    if tamper == "ciphertext":
        blob = snapshot.path.with_name(snapshot.ciphertext)
        data = bytearray(blob.read_bytes())
        data[len(data) // 2] ^= 1
        blob.write_bytes(bytes(data))
    elif tamper == "manifest":
        manifest = json.loads(snapshot.path.read_text())
        manifest["created_at"] = (NOW + timedelta(days=30)).isoformat()
        snapshot.path.write_text(json.dumps(manifest))
    else:
        key = "a-different-backup-key-that-is-long-enough-000"
    with pytest.raises(RecoveryError):
        await recovery_rig.restore(snapshot, key=key)
    assert recovery_rig.db_path.read_bytes() == before
    assert not list(recovery_rig.db_path.parent.glob(".*restore*"))


async def test_backup_is_encrypted_and_keys_stay_separate(recovery_rig):
    snapshot = await recovery_rig.backup()
    blob = snapshot.path.with_name(snapshot.ciphertext).read_bytes()
    assert b"private diary" not in blob and b"SQLite format" not in blob
    with pytest.raises(RecoveryError, match="separate"):
        write_backup(
            recovery_rig.tmp_path / "backups",
            database=recovery_rig.db_path,
            journal=recovery_rig.journal,
            key="shared-key-used-for-both-purposes-0123456789",
            credential_key="shared-key-used-for-both-purposes-0123456789",
            now=NOW,
        )


async def test_restore_refuses_while_runtime_owns_database(recovery_rig):
    snapshot = await recovery_rig.backup()
    with database_owner(recovery_rig.state.database), pytest.raises(RecoveryError, match="owned"):
        restore_backup(
            snapshot.path, recovery_rig.journal, database=recovery_rig.db_path, key=BACKUP_KEY
        )


async def test_backups_older_than_seven_days_are_pruned(recovery_rig):
    for day in range(9):
        await recovery_rig.backup(NOW + timedelta(days=day))
    kept = sorted(path.name for path in (recovery_rig.tmp_path / "backups").glob("*.json"))
    assert len(kept) == 8  # days 1..8 are within seven days of the newest backup
    assert len(list((recovery_rig.tmp_path / "backups").glob("*.enc"))) == 8


async def test_restored_budget_counter_keeps_paid_work_closed_until_reconciled(
    database, settings, clock, rates, tmp_path
):
    journal = tmp_path / "deletions.jsonl"
    journal.touch()
    db_path = Path(database.engine.url.database)
    costs = CostService(database, clock, RuntimePolicy(), rates)
    bound = WorkBound(units={"work": 1})

    async def spend(attempt_id):
        await costs.reserve(
            CostScope(user_id="u", job_id=attempt_id), attempt_id, "fake", "model", bound
        )
        await costs.settle(attempt_id, Usage(units={"work": 1}), "succeeded")

    await spend("before-backup")
    snapshot = write_backup(
        tmp_path / "backups",
        database=db_path,
        journal=journal,
        key=BACKUP_KEY,
        credential_key=None,
        now=clock.now,
    )
    await spend("after-backup-1")
    await spend("after-backup-2")
    await database.dispose()
    restore_backup(snapshot.path, journal, database=db_path, key=BACKUP_KEY)

    restored = Database(settings.database_url)
    try:
        costs = CostService(restored, clock, RuntimePolicy(), rates)
        assert await costs.spent_micro_inr() == 1_000_000  # stale restored counter
        with pytest.raises(CostAccountingError):
            await spend("after-restore")
        with pytest.raises(ValueError):
            await costs.reconcile_restored_spend(day_micro_inr=-1, month_micro_inr=0)
        await costs.reconcile_restored_spend(day_micro_inr=3_000_000, month_micro_inr=3_000_000)
        assert await costs.spent_micro_inr() == 3_000_000
        # Per-user day counters are stale too, so paid work resumes only next Kolkata day.
        with pytest.raises(CostAccountingError):
            await spend("same-day-after-reconcile")
        clock.now += timedelta(seconds=1)  # fixture clock sits at 23:59:59 Kolkata
        await spend("next-day")
        assert await costs.spent_micro_inr() == 4_000_000
    finally:
        await restored.dispose()


async def test_purge_respects_retention_boundaries(database):
    day, month = timedelta(days=1), timedelta(days=30)
    old, recent = NOW - month - timedelta(hours=1), NOW - month + timedelta(hours=1)
    async with database.session_factory() as session:
        user = User(email="purge@example.com", is_verified=True)
        session.add(user)
        await session.flush()
        for when, code in (
            (NOW - day - timedelta(hours=1), "old"),
            (NOW - day + timedelta(hours=1), "new"),
        ):
            session.add(
                OtpChallenge(
                    email=f"{code}@example.com", code_hash=code, expires_at=when, created_at=when
                )
            )
        sessions = {}
        for label, started, ended in (
            ("old", old, old),
            ("recent", recent, recent),
            ("open", old, None),
        ):
            sessions[label] = UsageSession(user_id=user.id, started_at=started, ended_at=ended)
            session.add(sessions[label])
        await session.flush()
        for label in ("old", "recent"):
            session.add(
                TurnMetric(
                    usage_session_id=sessions[label].id,
                    turn_index=0,
                    created_at=sessions[label].started_at,
                )
            )
            for _tick in range(3):
                session.add(
                    WalletTransaction(
                        user_id=user.id,
                        amount_paise=-2,
                        kind="usage",
                        usage_session_id=sessions[label].id,
                        created_at=sessions[label].started_at,
                    )
                )
        session.add(
            WalletTransaction(user_id=user.id, amount_paise=500, kind="topup", created_at=old)
        )
        await session.commit()
    async with database.engine.begin() as connection:
        for attempt_id, state, created, job in (
            ("settled-old", "settled", old, "j"),
            ("unknown-old", "unknown", old, "j"),
            ("settled-recent", "settled", recent, "j"),
            # The live-evaluation cap is cumulative; purging would silently re-grant it.
            ("eval-old", "settled", old, "live-eval:old"),
        ):
            await connection.execute(
                text(
                    "INSERT INTO cost_attempts (id,request_hash,user_id,job_id,provider,model,"
                    "bound_json,rate_card_json,maximum_micro_inr,actual_micro_inr,state,outcome,"
                    "day_key,month_key,created_at) VALUES (:id,'h','u',:job,'p','m','{}','{}',5,"
                    "1,:state,'succeeded','2026-09-01','2026-09',:created)"
                ),
                {"id": attempt_id, "state": state, "created": created.isoformat(), "job": job},
            )
        for kind, period, held in (
            ("global_day", "2026-08-31", 0),
            ("global_day", "2026-08-30", 7),
            ("global_day", "2026-09-01", 0),
            ("global_month", "2026-08", 0),
            ("global_month", "2026-09", 0),
        ):
            await connection.execute(
                text(
                    "INSERT INTO cost_periods (kind,owner,period,spent_micro_inr,held_micro_inr)"
                    " VALUES (:kind,'*',:period,1,:held)"
                ),
                {"kind": kind, "period": period, "held": held},
            )

    result = await purge_expired(database, NOW)

    async with database.engine.connect() as connection:

        async def column(sql):
            return [row[0] for row in await connection.execute(text(sql))]

        assert await column("SELECT code_hash FROM otp_challenges") == ["new"]
        assert sorted(await column("SELECT id FROM cost_attempts")) == [
            "eval-old",
            "settled-recent",
            "unknown-old",
        ]
        assert sorted(await column("SELECT kind || ':' || period FROM cost_periods")) == [
            "global_day:2026-08-30",
            "global_day:2026-09-01",
            "global_month:2026-09",
        ]
        assert len(await column("SELECT id FROM turn_metrics")) == 1
        remaining = set(await column("SELECT id FROM usage_sessions"))
        assert remaining == {sessions["recent"].id, sessions["open"].id}
        assert await column("SELECT SUM(amount_paise) FROM wallet_transactions") == [488]
        ledger = await column(
            "SELECT amount_paise FROM wallet_transactions"
            " WHERE usage_session_id IS NULL AND kind='usage'"
        )
        assert ledger == [-6], "old ticks fold into one row per purged session"
    assert result.deleted["cost_attempts"] == 1 and result.deleted["usage_sessions"] == 1


async def test_default_logging_has_no_full_transcript(session_policy, paid_boundary, caplog):
    user_text = "my secret holiday plan is to visit Goa"
    caplog.set_level(logging.DEBUG)
    r = session_policy([ALLOW, "Sounds lovely.", ALLOW])
    await run_turn(r, user_text)
    await r.voice._persist_timer(r.timer)
    persisted = []
    async with paid_boundary.costs.database.engine.connect() as connection:
        tables = await connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        for (table,) in tables.all():
            rows = await connection.execute(text(f'SELECT * FROM "{table}"'))
            persisted.extend(str(value) for row in rows for value in row)
    for spoken in (user_text, "Sounds lovely."):
        assert all(spoken not in value for value in persisted)
        assert spoken not in caplog.text
    async with paid_boundary.costs.database.session_factory() as session:
        assert (await session.execute(select(TurnMetric))).scalars().first() is not None
