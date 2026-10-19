# Backups, retention and deletion-safe restore

Talkstream keeps one SQLite database. It is backed up as encrypted daily snapshots; a separate deletion journal guarantees that restoring an older snapshot cannot bring back a source or provider key that a user deleted.

## Configuration

| Setting | Requirement |
|---|---|
| `BACKUP_ENCRYPTION_KEY` (or `_FILE`) | At least 32 characters, different from `CREDENTIAL_ENCRYPTION_KEY`. Store it apart from the backups and from the credential key. Backups are refused if both keys are equal. |
| `RECOVERY_JOURNAL_FILE` | A path on an owner-controlled mount that is **not** inside database backups. Its directory must already exist: a missing mount stops startup and refuses deletions instead of journaling to local disk. Public mode will not start without it. |
| Backup destination | An off-host encrypted location (for example a mounted remote volume or a synced bucket). A directory on the same disk as the database is insufficient for public release. |

The journal is append-only metadata (`kind`, `user_id`, record ID, `deleted_at`), written and fsynced **before** a deletion is acknowledged. Copy it off-host together with the backups; a restore needs a journal at least as new as the backup.

## Daily backup

Run as the `talkstream` user from `/opt/talkstream/backend` with the service environment, for example from a systemd timer or cron:

```sh
.venv/bin/python scripts/recovery.py backup --destination /mnt/offhost/talkstream-backups
```

The snapshot uses SQLite's online backup API (consistent while the service runs) and is encrypted with AES-256-GCM; every manifest field is authenticated, so an edited manifest or ciphertext fails restore. Backups older than seven days relative to the newest are pruned from the destination automatically.

## Restore

1. Stop the service: `systemctl stop talkstream`. Restore refuses while the runtime owns the database.
2. Restore the chosen manifest using the **current** journal:
   ```sh
   .venv/bin/python scripts/recovery.py restore --manifest /mnt/offhost/talkstream-backups/<name>.json
   ```
   Restore verifies authentication, content digest and SQLite integrity, checks that the journal is not older than the backup, replays every tombstone (deleted sources and their chunks; provider keys not re-saved after deletion), then atomically swaps the file. Any failure leaves the existing database untouched. A missing or older journal fails closed.
3. Start the service. Paid work stays **disabled**: a restored budget counter is not evidence of unused spend.
4. From each vendor dashboard, total the current Kolkata day and month provider spend in micro-INR and record it:
   ```sh
   .venv/bin/python scripts/recovery.py reconcile --day-micro-inr <day> --month-micro-inr <month>
   ```
   The ledger keeps the higher of the restored and supplied totals. Per-user daily counters cannot be rebuilt from vendor totals, so paid work resumes at the next Kolkata midnight. Unknown/reserved attempts restored from the snapshot remain held until reconciled normally.

## Retention

Applied at startup and daily (`scripts/recovery.py purge` runs it on demand):

| Data | Kept |
|---|---|
| Expired OTP challenges, expired voice tickets | 1 day after expiry |
| Usage sessions, turn metrics (metadata only), tool receipts, OTP send counters, settled provider attempts, released call leases, closed envelopes, finished ingestion admissions, completed deletion events | 30 days |
| Wallet ledger | Kept; ticks of purged sessions fold into one row per session so balances are unchanged |
| Budget period counters | Current and prior period; any counter still holding money is kept |
| Reserved, unknown or disputed provider attempts | Until reconciled |
| Live-evaluation attempts | Kept (the evaluation cap is cumulative over them) |
| Source text and provider keys | Until the user deletes them |
| Encrypted backups | 7 days |
| Deletion journal | Not pruned automatically; entries are small metadata and must outlive the backup horizon |

Audio and complete transcripts are not persisted by default.

## What deletion means for users

Deleting a source or key removes it from the live service as soon as cleanup completes. Copies inside encrypted backups disappear when those backups expire, within seven days, and a restore cannot reactivate them because the journal is replayed first.

## Limits

Qdrant vectors are not part of SQLite backups. After a restore, vectors for sources missing from SQL are never served (every hit is rechecked against owned SQL rows) but remain stored until the collection is rebuilt or swept. Hosting, the off-host destination and the timer are deployment inputs for S28.
