"""Lossless cold storage for consumed research snapshot blobs (Python 3.10).

Run only while the source writer and every follower are stopped and their flock
locks are held by the caller. This tool does not start/stop services. It changes
only snapshots.raw; every row/key, observation, position and manifest survives.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time

MIB = 1024 ** 2
RETENTION_MS = 72 * 60 * 60 * 1000
PROTOCOL = 'research-snapshot-cold-storage-v1'
V2_COHORT = '2026-10-15-diversified-shadow-v1'
MAX_BLOB_BYTES = 8 * MIB


class StorageError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as saved:
        for chunk in iter(lambda: saved.read(MIB), b''):
            result.update(chunk)
    return result.hexdigest()


def _identifier(name):
    return '"' + name.replace('"', '""') + '"'


def _connect(path, *, readonly=False):
    path = Path(path).resolve(strict=True)
    connection = sqlite3.connect(path.as_uri() + '?mode=' + ('ro' if readonly else 'rw'),
                                 uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=3000')
    connection.execute('PRAGMA cache_size=-2048')
    connection.execute('PRAGMA temp_store=FILE')
    if readonly:
        connection.execute('PRAGMA query_only=ON')
    return connection


def _schema(connection):
    return [list(row) for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_stat_%' ORDER BY type,name")]


def _identity(connection):
    columns = {row['name']: row for row in connection.execute('PRAGMA table_info(snapshots)')}
    if not {'id', 'symbol', 'bar_ms', 'observed_ms', 'raw'} <= columns.keys():
        raise StorageError('UNEXPECTED_SNAPSHOT_SCHEMA')
    if columns['id']['pk'] != 1 or columns['raw']['notnull']:
        raise StorageError('SNAPSHOT_IDS_MUST_BE_PRIMARY_AND_RAW_NULLABLE')
    position_columns = {row['name'] for row in connection.execute('PRAGMA table_info(positions)')}
    if not {'snapshot_id', 'status'} <= position_columns:
        raise StorageError('MISSING_OPEN_POSITION_PROTECTION')
    row = connection.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
    if row is None:
        raise StorageError('MISSING_FROZEN_MANIFEST')
    manifest = json.loads(row[0])
    if not isinstance(manifest, dict) or not manifest.get('cohort'):
        raise StorageError('INVALID_FROZEN_MANIFEST')
    schema = _schema(connection)
    return {'protocol': PROTOCOL, 'cohort': manifest['cohort'],
            'source_manifest_sha256': digest_bytes(row[0].encode()),
            'source_schema_sha256': digest_bytes(canonical(schema).encode()),
            'source_schema': schema}


def _encoded(value):
    if isinstance(value, bytes):
        return {'blob_sha256': digest_bytes(value), 'length': len(value)}
    return value


def logical_digest(path, *, include_raw=False):
    """Streaming logical table/schema proof; raw is omitted only from snapshots.

    Explicit primary keys define row ordering, including future snapshot IDs.
    Counts and hashes cover all other tables, not only trading-stat summaries.
    """
    with closing(_connect(path, readonly=True)) as connection:
        connection.execute('BEGIN')
        schema = _schema(connection)
        result = {'schema_sha256': digest_bytes(canonical(schema).encode()), 'tables': {}}
        for table_row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                            "AND name NOT LIKE 'sqlite_stat_%' ORDER BY name"):
            name = table_row[0]
            info = list(connection.execute('PRAGMA table_info(' + _identifier(name) + ')'))
            columns = [row['name'] for row in info
                       if include_raw or name != 'snapshots' or row['name'] != 'raw']
            primary = [row['name'] for row in sorted(info, key=lambda row: row['pk']) if row['pk']]
            ordering = primary or columns
            query = ('SELECT ' + ','.join(map(_identifier, columns)) + ' FROM ' + _identifier(name)
                     + ' ORDER BY ' + ','.join(map(_identifier, ordering)))
            digest, count = hashlib.sha256(), 0
            digest.update(canonical(columns).encode() + b'\n')
            for row in connection.execute(query):
                digest.update(canonical([_encoded(value) for value in row]).encode() + b'\n')
                count += 1
            result['tables'][name] = {'rows': count, 'sha256': digest.hexdigest()}
        return result


def _separate(path, source, label):
    path, source = Path(path).resolve(), Path(source).resolve()
    if path == source or path.is_relative_to(source.parent):
        raise StorageError(label + '_MUST_BE_OUTSIDE_ACTIVE_DATA_DIRECTORY')
    return path


def _free_space(directory, required, reserve):
    free = shutil.disk_usage(directory).free
    if free < required + reserve:
        raise StorageError('INSUFFICIENT_DISK_HEADROOM: ' + canonical(
            {'free_bytes': free, 'required_bytes': required, 'reserve_bytes': reserve}))
    return free


def _fsync_file(path):
    # Windows CRT rejects fsync on a read-only descriptor; no bytes are changed.
    with Path(path).open('r+b') as saved:
        os.fsync(saved.fileno())


def _fsync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _mkdir_durable(path):
    """Persist every newly created directory entry before relying on its files."""
    path = Path(path)
    missing, current = [], path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        _fsync_directory(created.parent)


def backup_database(source, destination, *, minimum_free_bytes=256 * MIB):
    """Create a standalone consistent rollback copy, never overwrite a file."""
    source = Path(source).resolve(strict=True)
    destination = _separate(destination, source, 'BACKUP')
    if destination.exists():
        raise StorageError('BACKUP_ALREADY_EXISTS')
    _mkdir_durable(destination.parent)
    _free_space(destination.parent, source.stat().st_size, minimum_free_bytes)
    before = logical_digest(source, include_raw=True)
    with closing(_connect(source, readonly=True)) as original:
        with closing(sqlite3.connect(destination)) as backup:
            original.backup(backup, pages=128, sleep=.01)
            backup.execute('PRAGMA journal_mode=DELETE')
            if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise StorageError('BACKUP_INTEGRITY_FAILED')
    if logical_digest(destination, include_raw=True) != before:
        raise StorageError('BACKUP_LOGICAL_DIGEST_MISMATCH')
    os.chmod(destination, 0o600)
    _fsync_file(destination)
    _fsync_directory(destination.parent)
    return {'backup': str(destination), 'bytes': destination.stat().st_size,
            'sha256': file_sha256(destination), 'logical_digest': before}


def _archive_connection(path, identity, *, create=False):
    path = Path(path).resolve()
    if not path.exists() and not create:
        raise StorageError('ARCHIVE_MISSING')
    if create:
        _mkdir_durable(path.parent)
        connection = sqlite3.connect(path, timeout=3)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('PRAGMA cache_size=-2048')
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS archived_snapshots(
                id INTEGER PRIMARY KEY,symbol TEXT,bar_ms INTEGER,observed_ms INTEGER,
                raw BLOB NOT NULL,raw_sha256 TEXT NOT NULL,raw_bytes INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(
                id INTEGER PRIMARY KEY,at_ms INTEGER NOT NULL,report TEXT NOT NULL);
        ''')
        row = connection.execute("SELECT value FROM catalog_meta WHERE key='identity'").fetchone()
        if row is None:
            if connection.execute('SELECT 1 FROM archived_snapshots LIMIT 1').fetchone():
                connection.close()
                raise StorageError('NONEMPTY_ARCHIVE_WITHOUT_IDENTITY')
            connection.execute('INSERT INTO catalog_meta VALUES(?,?)', ('identity', canonical(identity)))
            connection.commit()
    else:
        connection = _connect(path, readonly=True)
        row = connection.execute("SELECT value FROM catalog_meta WHERE key='identity'").fetchone()
    row = connection.execute("SELECT value FROM catalog_meta WHERE key='identity'").fetchone()
    if row is None or json.loads(row[0]) != identity:
        connection.close()
        raise StorageError('ARCHIVE_SOURCE_IDENTITY_MISMATCH')
    return connection


def _check_blob(row):
    raw = row['raw']
    if (not isinstance(raw, bytes) or len(raw) > MAX_BLOB_BYTES
            or len(raw) != row['raw_bytes'] or digest_bytes(raw) != row['raw_sha256']):
        raise StorageError('ARCHIVE_BLOB_HASH_MISMATCH: ' + str(row['id']))
    return raw


def _same_metadata(source, archived):
    if source is None or any(source[key] != archived[key] for key in ('id', 'symbol', 'bar_ms', 'observed_ms')):
        raise StorageError('ARCHIVE_SNAPSHOT_IDENTITY_MISMATCH: ' + str(archived['id']))


def _verify_one(source_row, archive_row):
    if archive_row is None:
        raise StorageError('ARCHIVE_RECORD_MISSING: ' + str(source_row['id']))
    raw = _check_blob(archive_row)
    _same_metadata(source_row, archive_row)
    if source_row['raw'] is not None and source_row['raw'] != raw:
        raise StorageError('NONNULL_RAW_CONFLICT: ' + str(source_row['id']))
    return raw


def _verify_catalog(original, cold):
    """Verify both directions: every archive row, and every missing hot raw."""
    count, size, nulls = 0, 0, 0
    for row in cold.execute('SELECT * FROM archived_snapshots ORDER BY id'):
        current = original.execute('SELECT * FROM snapshots WHERE id=?', (row['id'],)).fetchone()
        raw = _verify_one(current, row)
        count += 1
        size += len(raw)
        nulls += int(current['raw'] is None)
    for row in original.execute('SELECT id FROM snapshots WHERE raw IS NULL ORDER BY id'):
        if cold.execute('SELECT 1 FROM archived_snapshots WHERE id=?', (row['id'],)).fetchone() is None:
            raise StorageError('UNRESOLVABLE_SOURCE_NULL: ' + str(row['id']))
    return {'archive_records': count, 'archive_raw_bytes': size, 'source_nulls_resolvable': nulls}


def verify_archive(source, archive):
    """Verify every catalog blob and its surviving source row, streaming."""
    source = Path(source).resolve(strict=True)
    archive = _separate(archive, source, 'ARCHIVE')
    with closing(_connect(source, readonly=True)) as original:
        with closing(_archive_connection(archive, _identity(original))) as cold:
            if cold.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise StorageError('ARCHIVE_INTEGRITY_FAILED')
            return _verify_catalog(original, cold)


def _candidates(cutoff_ms, consumer_cursor):
    condition = ("s.raw IS NOT NULL AND s.observed_ms<=? AND NOT EXISTS "
                 "(SELECT 1 FROM positions p WHERE p.snapshot_id=s.id AND coalesce(p.status,'')!='CLOSED')")
    parameters = [cutoff_ms]
    if consumer_cursor is not None:
        condition += ' AND s.id<=?'
        parameters.append(consumer_cursor)
    return condition, parameters


def archive_raw(source, archive, *, cutoff_ms, consumer_cursor=None, now_ms=None,
                batch_size=64, minimum_free_bytes=256 * MIB, _after_archive=None):
    """Durably archive old consumed raw, verify all, then NULL bounded ID batches.

    The optional private callback is a crash-injection seam used by tests. An
    interruption before/after a batch is safe: archive records are immutable,
    duplicate IDs are byte-checked, and only verified records can become NULL.
    """
    source = Path(source).resolve(strict=True)
    archive = _separate(archive, source, 'ARCHIVE')
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if (isinstance(cutoff_ms, bool) or not isinstance(cutoff_ms, int)
            or cutoff_ms > now - RETENTION_MS or cutoff_ms < 0):
        raise StorageError('KEEP_AT_LEAST_72_HOURS_OF_HOT_EVIDENCE')
    if consumer_cursor is not None and (isinstance(consumer_cursor, bool)
                                       or not isinstance(consumer_cursor, int) or consumer_cursor < 0):
        raise StorageError('INVALID_CONSUMER_CURSOR')
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 256:
        raise StorageError('INVALID_BATCH_SIZE')
    before = logical_digest(source)
    with closing(_connect(source)) as original:
        identity = _identity(original)
        if identity['cohort'] == V2_COHORT and consumer_cursor is None:
            raise StorageError('V2_REQUIRES_CONSUMER_FEED_CURSOR')
        condition, parameters = _candidates(cutoff_ms, consumer_cursor)
        totals = original.execute('SELECT count(*),coalesce(sum(length(s.raw)),0) FROM snapshots s WHERE '
                                  + condition, parameters).fetchone()
        _mkdir_durable(archive.parent)
        # Reserve enough for blob copies and a pessimistic source WAL rewrite.
        _free_space(archive.parent, int(totals[1] * 1.15) + source.stat().st_size + 8 * MIB,
                    minimum_free_bytes)
        with closing(_archive_connection(archive, identity, create=True)) as cold:
            # Validate earlier runs as well, including blobs already absent hot.
            _verify_catalog(original, cold)
            inserted = 0
            for row in original.execute('SELECT s.* FROM snapshots s WHERE ' + condition + ' ORDER BY s.id', parameters):
                raw = row['raw']
                if not isinstance(raw, bytes) or len(raw) > MAX_BLOB_BYTES:
                    raise StorageError('INVALID_OR_OVERSIZED_RAW: ' + str(row['id']))
                saved = (row['id'], row['symbol'], row['bar_ms'], row['observed_ms'], raw,
                         digest_bytes(raw), len(raw))
                cursor = cold.execute('INSERT OR IGNORE INTO archived_snapshots VALUES(?,?,?,?,?,?,?)', saved)
                inserted += max(0, cursor.rowcount)
                _verify_one(row, cold.execute('SELECT * FROM archived_snapshots WHERE id=?', (row['id'],)).fetchone())
                if inserted and inserted % batch_size == 0:
                    cold.commit()
            cold.commit()
            # Separate full pass after durable commit, before the first NULL.
            for row in original.execute('SELECT s.* FROM snapshots s WHERE ' + condition + ' ORDER BY s.id', parameters):
                _verify_one(row, cold.execute('SELECT * FROM archived_snapshots WHERE id=?', (row['id'],)).fetchone())
            _fsync_file(archive)
            _fsync_directory(archive.parent)
            if _after_archive is not None:
                _after_archive()
            cleared, last_id = 0, -1
            while True:
                ids = []
                rows = original.execute('SELECT s.* FROM snapshots s WHERE ' + condition
                                        + ' AND s.id>? ORDER BY s.id LIMIT ?', [*parameters, last_id, batch_size])
                for row in rows:
                    _verify_one(row, cold.execute('SELECT * FROM archived_snapshots WHERE id=?', (row['id'],)).fetchone())
                    ids.append(row['id'])
                rows.close()
                if not ids:
                    break
                with original:
                    original.executemany('UPDATE snapshots SET raw=NULL WHERE id=? AND raw IS NOT NULL',
                                         [(identifier,) for identifier in ids])
                cleared += len(ids)
                last_id = ids[-1]
            after = logical_digest(source)
            if after != before:
                raise StorageError('NONRAW_LOGICAL_DIGEST_CHANGED')
            report = {'protocol': PROTOCOL, 'at_ms': now, 'source': str(source), 'archive': str(archive),
                      'identity': identity, 'cutoff_ms': cutoff_ms, 'consumer_cursor': consumer_cursor,
                      'new_archive_records': inserted, 'cleared_raw_records': cleared,
                      'candidate_raw_bytes': totals[1], 'nonraw_digest': after,
                      'retained_snapshot_rows': after['tables']['snapshots']['rows']}
            cold.execute('INSERT INTO runs(at_ms,report) VALUES(?,?)', (now, canonical(report)))
            cold.commit()
    report.update(verify_archive(source, archive))
    return report


def resolve_raw(source, archive, snapshot_id):
    """Resolve original bytes without changing hot storage or frozen workers."""
    source = Path(source).resolve(strict=True)
    archive = _separate(archive, source, 'ARCHIVE')
    with closing(_connect(source, readonly=True)) as original:
        row = original.execute('SELECT * FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
        if row is None:
            raise StorageError('SNAPSHOT_MISSING: ' + str(snapshot_id))
        if not archive.exists():
            if row['raw'] is None:
                raise StorageError('ARCHIVE_MISSING')
            return row['raw']
        with closing(_archive_connection(archive, _identity(original))) as cold:
            archived = cold.execute('SELECT * FROM archived_snapshots WHERE id=?', (snapshot_id,)).fetchone()
            if archived is None and row['raw'] is not None:
                return row['raw']
            return _verify_one(row, archived)


def restore_nulls(source, archive, *, copy_to=None, batch_size=64, minimum_free_bytes=256 * MIB):
    """Restore archive bytes only into NULL cells; preserve future rows and raw."""
    source = Path(source).resolve(strict=True)
    archive = _separate(archive, source, 'ARCHIVE')
    target = source
    if copy_to is not None:
        backup_database(source, copy_to, minimum_free_bytes=minimum_free_bytes)
        target = Path(copy_to).resolve(strict=True)
    before = logical_digest(target)
    verify_archive(target, archive)  # Reject all conflicts before restoring one cell.
    with closing(_connect(target)) as original:
        with closing(_archive_connection(archive, _identity(original))) as cold:
            restored = 0
            for row in cold.execute('SELECT * FROM archived_snapshots ORDER BY id'):
                current = original.execute('SELECT * FROM snapshots WHERE id=?', (row['id'],)).fetchone()
                raw = _verify_one(current, row)
                if current['raw'] is None:
                    original.execute('UPDATE snapshots SET raw=? WHERE id=? AND raw IS NULL', (raw, row['id']))
                    restored += 1
                    if restored % batch_size == 0:
                        original.commit()
            original.commit()
    if logical_digest(target) != before:
        raise StorageError('RESTORE_CHANGED_NONRAW_HISTORY')
    return {'restored_raw_records': restored, 'target': str(target), 'source_untouched': copy_to is not None,
            'logical_digest': logical_digest(target, include_raw=True)}


def compact_database(source, compact_path, *, expected_nonraw_digest=None, minimum_free_bytes=256 * MIB):
    """Checkpoint, VACUUM INTO, prove equality and replace one closed database.

    Services/flocks must remain stopped/held until the replacement completes.
    A busy/nontruncatable WAL or leftover sidecar rejects the replacement.
    """
    source = Path(source).resolve(strict=True)
    compact = _separate(compact_path, source, 'COMPACT_COPY')
    if compact.exists():
        raise StorageError('COMPACT_COPY_ALREADY_EXISTS')
    _mkdir_durable(compact.parent)
    if source.stat().st_dev != compact.parent.stat().st_dev:
        raise StorageError('COMPACT_COPY_MUST_SHARE_FILESYSTEM_FOR_ATOMIC_REPLACE')
    _free_space(compact.parent, source.stat().st_size, minimum_free_bytes)
    before_nonraw = logical_digest(source)
    if expected_nonraw_digest is not None and before_nonraw != expected_nonraw_digest:
        raise StorageError('PRECOMPACTION_LOGICAL_DIGEST_MISMATCH')
    before_full = logical_digest(source, include_raw=True)
    size_before, mode = source.stat().st_size, stat.S_IMODE(source.stat().st_mode)
    with closing(_connect(source)) as original:
        checkpoint = original.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint[0] != 0:
            raise StorageError('BUSY_WAL_REJECTED')
        try:
            journal = original.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
            if journal != 'delete':
                raise StorageError('WAL_NOT_CLOSED')
            original.execute('PRAGMA locking_mode=EXCLUSIVE')
            original.execute('BEGIN EXCLUSIVE')
            original.commit()
            original.execute('VACUUM INTO ?', (str(compact),))
        except sqlite3.OperationalError as exc:
            raise StorageError('DATABASE_NOT_EXCLUSIVELY_QUIESCENT: ' + str(exc)) from exc
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(source) + suffix).exists():
            raise StorageError('LIVE_SIDECAR_REJECTED: ' + suffix)
    with closing(_connect(compact, readonly=True)) as checked:
        if checked.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise StorageError('COMPACT_INTEGRITY_FAILED')
    if logical_digest(compact, include_raw=True) != before_full:
        raise StorageError('COMPACT_LOGICAL_DIGEST_MISMATCH')
    if logical_digest(source, include_raw=True) != before_full:
        raise StorageError('SOURCE_CHANGED_BEFORE_REPLACE')
    os.chmod(compact, mode)
    _fsync_file(compact)
    os.replace(compact, source)
    _fsync_directory(source.parent)
    return {'source': str(source), 'bytes_before': size_before, 'bytes_after': source.stat().st_size,
            'bytes_reclaimed': size_before - source.stat().st_size,
            'nonraw_digest': before_nonraw, 'sha256': file_sha256(source)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--locks-held', action='store_true', help='Caller stopped source/follower and holds both flocks')
    sub = parser.add_subparsers(dest='operation', required=True)
    for operation in ('backup', 'archive', 'verify', 'compact', 'restore', 'resolve'):
        command = sub.add_parser(operation)
        command.add_argument('--source', type=Path, required=True)
        if operation == 'backup':
            command.add_argument('--destination', type=Path, required=True)
        elif operation == 'compact':
            command.add_argument('--compact-path', type=Path, required=True)
        else:
            command.add_argument('--archive', type=Path, required=True)
        if operation == 'archive':
            command.add_argument('--cutoff-ms', type=int, required=True)
            command.add_argument('--consumer-cursor', type=int)
        if operation == 'restore':
            command.add_argument('--copy-to', type=Path)
        if operation == 'resolve':
            command.add_argument('--snapshot-id', type=int, required=True)
            command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.operation in ('archive', 'compact', 'restore') and not args.locks_held:
        parser.error('Mutating maintenance requires --locks-held and actual external service/flock exclusion')
    if args.operation == 'backup':
        result = backup_database(args.source, args.destination)
    elif args.operation == 'archive':
        result = archive_raw(args.source, args.archive, cutoff_ms=args.cutoff_ms, consumer_cursor=args.consumer_cursor)
    elif args.operation == 'verify':
        result = verify_archive(args.source, args.archive)
    elif args.operation == 'compact':
        result = compact_database(args.source, args.compact_path)
    elif args.operation == 'restore':
        result = restore_nulls(args.source, args.archive, copy_to=args.copy_to)
    else:
        raw = resolve_raw(args.source, args.archive, args.snapshot_id)
        with args.output.open('xb') as output:
            output.write(raw)
        result = {'snapshot_id': args.snapshot_id, 'output': str(args.output), 'sha256': digest_bytes(raw)}
    print(canonical(result))


if __name__ == '__main__':
    main()
