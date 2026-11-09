# Lossless storage maintenance for the V2 → V3 research feed

`research_storage.py` is an operational tool outside either frozen research
package. It neither changes strategy code/settings/manifests nor resets the
cohort, positions, observations, registration watermark or consumer cursor.
It cannot place orders or start/stop services; it imports only Python 3.10's
standard library.

## Why cold storage is necessary

The workers stop admissions once `shadow.sqlite3*` exceeds their frozen 512 MiB
limit. A new limit would change the frozen protocol. Plain VACUUM cannot remove
live snapshot evidence when there are no free pages, and page-size tuning only
postpones the next stop. Keeping unlimited original blobs inline and retaining
the same finite limit is impossible.

The active database retains **every snapshot row and its original ID, symbol,
bar time and observation time**. Only eligible `snapshots.raw` cells become SQL
NULL after byte-exact originals have been durably copied and verified in a cold
SQLite archive. NULL explicitly means archived evidence, never a fabricated
empty snapshot. All other table rows/fields, indexes and frozen metadata stay
unchanged. Archive-aware historical reads use `resolve_raw`; a full historical
database can be reconstructed with `restore_nulls(..., copy_to=...)`.

The unchanged V2 scanner checks old snapshot keys to prevent duplicate entries;
it does not decode their raw blobs. The unchanged V3 follower decodes only new
rows beyond its saved feed cursor. Therefore old consumed blobs can be tiered
without changing future entry/exit logic. No missed historical trade is admitted.

## Eligibility and durable ordering

An eligible snapshot must satisfy all of these:

- Its original observation is at least 72 hours old. The caller may keep more.
- For V2, its ID is at most the actual, locked V3 `feed_cursor`. This argument is
  mandatory for cohort `2026-10-15-diversified-shadow-v1`; the tool never advances
  the cursor to make a row eligible.
- No position referencing the snapshot has any status other than `CLOSED`.
- The original `raw` blob is non-NULL and at most 8 MiB.

V3 has no downstream consumer, so it needs the age/open-position checks without
the source-cursor constraint. Source snapshots that are unconsumed, recent or
referenced by an open position remain fully available in active storage.

One append-only archive database belongs to one exact source manifest/schema
identity. `catalog_meta.identity` stores the protocol, cohort, SHA256 of the exact
manifest string and source schema. Each `archived_snapshots` row contains the
original ID/key/timestamps, original compressed blob, byte length and SHA256.
Existing IDs are accepted only if all metadata and bytes match; never replaced.
The archive's `runs` table records maintenance inputs and proofs.

The tool commits all candidate blobs, rereads/verifies every candidate, fsyncs
the file **and its directory entry**, and only then clears source raw in bounded
ID batches. Newly created archive directory entries are fsynced as well. A crash
before clearing leaves duplicate evidence, and a crash during clearing leaves
every NULL resolvable. Restart checks both archive → source and source NULL →
archive coverage; missing/corrupt/conflicting records stop maintenance.

`logical_digest` streams deterministic primary-key ordered hashes and row counts
for every source table plus the schema. Before/after archival hashes omit only
`snapshots.raw`. Restore and compaction additionally prove full blob equality.
No entire snapshot table is loaded in memory. Archive writes use DELETE journal,
FULL synchronization and a 2 MiB cache. The source retains normal SQLite locking.

## Caller runbook

1. Record protected code/settings/manifest hashes, all table digests/counts,
   snapshot maximum ID, V3 cursor/watermark, source statuses and service state.
   Ensure disk headroom for rollback copies, archive growth, a potential WAL and
   one compact copy. Archives/rollback copies must be outside active `data/`.
2. Stop **only** V2/V3 timers, let in-progress cycles finish, and hold both
   `.cycle.lock` flocks throughout maintenance. Do not kill or restart the main
   trading bot/monitor or unrelated studies. A flag is not a substitute for locks.
3. Create and verify standalone rollback copies using `backup_database`. Keep
   them outside active data until the restarted pipeline is verified. Copies
   contain the complete original DB, including every raw blob.
4. Read the now-stable V3 cursor. Archive eligible V2 raw with that cursor, then
   archive eligible V3 raw without a downstream cursor. Use a cold catalog per
   source cohort, outside the frozen code roots and active data directories.
5. Compact one source at a time. `compact_database` checkpoints WAL, rejects a
   busy/nontruncatable WAL, requires exclusive SQLite access, switches the closed
   file to DELETE journaling, and runs `VACUUM INTO` on a separate same-filesystem
   path. It verifies integrity and all logical values before atomic replacement;
   any remaining WAL/SHM/journal sidecar rejects the replacement. The unchanged
   worker re-enables WAL on its next normal connection.
6. Verify all nonraw table digests, row/ID counts, manifest/code/settings hashes,
   archive coverage and the quota glob size. Restore DB permissions; keep the
   existing V3 read-only DB/status bind mounts and WAL-sidecar directory access.
7. Release locks, start V2's existing timer and then V3's. Verify a genuinely new
   source snapshot and a subsequent V3 cursor/decision cycle, not just an updated
   status timestamp. Record the maintenance gap; do not backfill missed entries
   or rewrite source health/time to look current. Review any errors before
   considering the pipeline recovered.

Python API used by the operational wrapper:

```python
backup_database(source, rollback_copy)
proof = archive_raw(source, archive, cutoff_ms=now_ms - RETENTION_MS,
                    consumer_cursor=v3_cursor)  # omit cursor for V3 only
verify_archive(source, archive)
compact_database(source, compact_copy,
                 expected_nonraw_digest=proof['nonraw_digest'])
raw = resolve_raw(source, archive, snapshot_id)
restore_nulls(source, archive, copy_to=restored_copy)
```

CLI examples (actual service/flock exclusion must already be held):

```text
python research_storage.py --locks-held archive --source SOURCE --archive COLD --cutoff-ms CUTOFF --consumer-cursor CURSOR
python research_storage.py verify --source SOURCE --archive COLD
python research_storage.py --locks-held compact --source SOURCE --compact-path TEMP
python research_storage.py --locks-held restore --source SOURCE --archive COLD --copy-to NEW_COPY
```

## Rollback, retention and limits

Before resuming workers, a verified rollback copy can replace the quiescent
source. After any new entries exist, **do not replace the DB with an old copy**:
that would lose future IDs/trades. `restore_nulls` instead restores only verified
NULL cells and preserves all newer rows/non-NULL evidence. It rejects any
conflicting non-NULL raw value. Full restoration into active storage can exceed
the original cap again; normally reconstruct a separate copy for historical
analysis. The archive must remain with the study and its exported reports.

Do not delete catalogs or cold evidence while any active raw is NULL. The code
verifies one complete catalog per source, not a chain of optional files. A lost
catalog is an evidence failure, not zero history. Backup/catalog files are not
hidden under `shadow.sqlite3*` to bypass accounting; they are separately counted
against whole-server disk space. Moving evidence frees the working-set quota,
not total long-term disk demand.

A separate low-priority operational timer may maintain the working set before
384 MiB, leaving at least 72 hours hot and targeting roughly 256 MiB or less.
This helper does not schedule itself or relax disk guards. If the whole-server
reserve cannot cover archival/WAL/compaction, it fails before clearing raw;
external disk expansion or verified archive offload is then necessary. Existing
rollback copies should be managed deliberately, not multiplied on each run.

Tests cover consumed/recent/open protection, immutable rows/statistics, exact
hash resolution, complete restore, future ID 8996 and subsequent ID generation,
archive conflicts/corruption/missing coverage, crash-before-NULL restart,
idempotence, active WAL refusal and committed WAL visibility, quota paths,
disk-headroom refusal, and directory durability ordering.

## Scheduled maintenance

`research_storage_maintenance.py` is the separate operational wrapper. It
checks both labs daily, starts archival at 384 MiB, retains at least 72 hours
of hot input and protects V3's committed source cursor and nonclosed trades.
It stops only V2/V3 timers/oneshots, holds both cycle locks and verifies exact
non-raw history throughout. The initial rollout also restores each real archive
into a disposable database and compares the complete pre-maintenance digest.

The initial temporary override runs the permanent systemd sandbox with
`--force --verify-restore`. Normal runs reuse the same append-only archive,
verify byte coverage, and remove only their disposable rollback copies after
success. Archive files are retained. `/research-storage` exposes saved health;
low disk space or errors are distinct from an idle successful capacity check.
`ExecStopPost --recover-timers` restores only the originally active research
timers even after timeout/OOM, and records a degraded maintenance status.
