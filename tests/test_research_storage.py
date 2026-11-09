from contextlib import closing
import json
from pathlib import Path
import sqlite3
import zlib

import pytest

import research_storage as storage


NOW = 2_000_000_000_000
OLD = NOW - 7 * 86_400_000
CUTOFF = NOW - storage.RETENTION_MS


def open_db(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def fixture(tmp_path, *, cohort=storage.V2_COHORT):
    data = tmp_path / 'lab' / 'data'
    data.mkdir(parents=True)
    source = data / 'shadow.sqlite3'
    with closing(open_db(source)) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.executescript('''
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE snapshots(id INTEGER PRIMARY KEY,symbol TEXT,bar_ms INTEGER,
                observed_ms INTEGER,raw BLOB,UNIQUE(symbol,bar_ms));
            CREATE TABLE observations(id INTEGER PRIMARY KEY,snapshot_id INTEGER,
                strategy TEXT,symbol TEXT,bar_ms INTEGER,reason TEXT,detail TEXT);
            CREATE TABLE positions(id INTEGER PRIMARY KEY,snapshot_id INTEGER,status TEXT,
                strategy TEXT,net_usdt REAL,costs TEXT);
        ''')
        manifest = {'cohort': cohort, 'registered_ms': OLD - 1000,
                    'order_capability': False, 'settings': {'max_store_mb': 512}}
        connection.executemany('INSERT INTO meta VALUES(?,?)', [
            ('manifest', json.dumps(manifest)), ('feed_cursor', '4'), ('registration_watermark', '0')])
        for identifier, observed in ((1, OLD), (2, OLD + 1000), (3, OLD + 2000),
                                     (4, NOW - 3_600_000), (8994, OLD + 3000), (8995, NOW - 1000)):
            raw = zlib.compress(json.dumps({'id': identifier, 'observed_ms': observed,
                                           'evidence': 'not-a-fabricated-placeholder'}).encode())
            connection.execute('INSERT INTO snapshots VALUES(?,?,?,?,?)',
                               (identifier, 'BTCUSDT', observed - 100, observed, raw))
        connection.executemany('INSERT INTO positions VALUES(?,?,?,?,?,?)', [
            (1, 1, 'CLOSED', 'MR', -1.234567, '{"fees":0.00345}'),
            (2, 3, 'OPEN', 'VWAP', None, None)])
        connection.executemany('INSERT INTO observations VALUES(?,?,?,?,?,?,?)', [
            (1, 1, 'MR', 'BTCUSDT', OLD - 100, 'OPENED', '{"x":"y"}'),
            (2, 2, 'VWAP', 'BTCUSDT', OLD + 900, 'NO_SIGNAL', '{}')])
        connection.commit()
    return source, tmp_path / 'cold' / 'snapshots.sqlite3'


def archive(source, cold, **overrides):
    arguments = {'cutoff_ms': CUTOFF, 'consumer_cursor': 4,
                 'now_ms': NOW, 'minimum_free_bytes': 0}
    arguments.update(overrides)
    return storage.archive_raw(source, cold, **arguments)


def raw_values(source):
    with closing(open_db(source)) as connection:
        return {row['id']: row['raw'] for row in connection.execute('SELECT id,raw FROM snapshots')}


def test_only_consumed_old_raw_is_archived_and_all_history_keys_stats_survive(tmp_path):
    source, cold = fixture(tmp_path)
    before = storage.logical_digest(source)
    original = raw_values(source)
    report = archive(source, cold)
    assert report['cleared_raw_records'] == report['new_archive_records'] == 2
    assert report['retained_snapshot_rows'] == 6
    assert storage.logical_digest(source) == before
    after = raw_values(source)
    assert after[1] is after[2] is None
    for protected in (3, 4, 8994, 8995):
        assert after[protected] == original[protected]
    for identifier in original:
        assert storage.resolve_raw(source, cold, identifier) == original[identifier]
    assert storage.verify_archive(source, cold)['source_nulls_resolvable'] == 2


def test_exact_restore_to_copy_preserves_future_8996_and_original_hot_storage(tmp_path):
    source, cold = fixture(tmp_path)
    expected = tmp_path / 'expected' / 'complete.sqlite3'
    storage.backup_database(source, expected, minimum_free_bytes=0)
    archive(source, cold)
    future = (8996, 'SOLUSDT', NOW, NOW + 1000, zlib.compress(b'future-original-evidence'))
    for path in (source, expected):
        with closing(open_db(path)) as connection:
            connection.execute('INSERT INTO snapshots VALUES(?,?,?,?,?)', future)
            connection.execute('INSERT INTO observations VALUES(3,8996,?,?,?,?,?)',
                               ('MR', 'SOLUSDT', NOW, 'NO_SIGNAL', '{"future":true}'))
            connection.commit()
    restored = tmp_path / 'restored' / 'full.sqlite3'
    result = storage.restore_nulls(source, cold, copy_to=restored, minimum_free_bytes=0)
    assert result['restored_raw_records'] == 2 and result['source_untouched']
    assert raw_values(source)[1] is None
    assert raw_values(restored)[8996] == future[4]
    assert storage.logical_digest(restored, include_raw=True) == storage.logical_digest(expected, include_raw=True)
    with closing(open_db(restored)) as connection:
        cursor = connection.execute('INSERT INTO snapshots(symbol,bar_ms,observed_ms,raw) VALUES(?,?,?,?)',
                                    ('XRPUSDT', NOW + 2000, NOW + 2001, b'next'))
        assert cursor.lastrowid == 8997
        connection.rollback()


def test_crash_after_durable_archive_before_nulls_is_safe_and_restart_idempotent(tmp_path):
    source, cold = fixture(tmp_path)
    before = storage.logical_digest(source, include_raw=True)

    def fail():
        raise RuntimeError('injected stop after archive commit')

    with pytest.raises(RuntimeError, match='injected'):
        archive(source, cold, _after_archive=fail)
    assert storage.logical_digest(source, include_raw=True) == before
    assert storage.verify_archive(source, cold)['archive_records'] == 2
    result = archive(source, cold)
    assert result['new_archive_records'] == 0 and result['cleared_raw_records'] == 2
    again = archive(source, cold)
    assert again['new_archive_records'] == again['cleared_raw_records'] == 0
    storage.restore_nulls(source, cold)
    assert storage.logical_digest(source, include_raw=True) == before


def test_conflicting_nonnull_raw_rejected_before_any_other_cell_is_cleared(tmp_path):
    source, cold = fixture(tmp_path)

    def stop():
        raise RuntimeError('stop')

    with pytest.raises(RuntimeError):
        archive(source, cold, _after_archive=stop)
    with closing(open_db(source)) as connection:
        connection.execute('UPDATE snapshots SET raw=? WHERE id=2', (b'changed-hot-payload',))
        connection.commit()
    before = raw_values(source)
    with pytest.raises(storage.StorageError, match='NONNULL_RAW_CONFLICT'):
        archive(source, cold)
    with pytest.raises(storage.StorageError, match='NONNULL_RAW_CONFLICT'):
        storage.restore_nulls(source, cold)
    assert raw_values(source) == before


def test_archive_corruption_or_wrong_source_identity_cannot_be_resolved_or_restored(tmp_path):
    source, cold = fixture(tmp_path)
    archive(source, cold)
    with closing(open_db(cold)) as connection:
        connection.execute("UPDATE archived_snapshots SET raw=x'00' WHERE id=1")
        connection.commit()
    with pytest.raises(storage.StorageError, match='HASH_MISMATCH'):
        storage.resolve_raw(source, cold, 1)
    with pytest.raises(storage.StorageError, match='HASH_MISMATCH'):
        storage.restore_nulls(source, cold)
    with closing(open_db(source)) as connection:
        connection.execute("UPDATE meta SET value='{}' WHERE key='manifest'")
        connection.commit()
    with pytest.raises(storage.StorageError, match='INVALID_FROZEN_MANIFEST'):
        storage.verify_archive(source, cold)


def test_missing_archive_record_cannot_launder_an_unresolvable_source_null(tmp_path):
    source, cold = fixture(tmp_path)
    archive(source, cold)
    with closing(open_db(cold)) as connection:
        connection.execute('DELETE FROM archived_snapshots WHERE id=1')
        connection.commit()
    before = raw_values(source)
    with pytest.raises(storage.StorageError, match='UNRESOLVABLE_SOURCE_NULL'):
        storage.verify_archive(source, cold)
    with pytest.raises(storage.StorageError, match='UNRESOLVABLE_SOURCE_NULL'):
        archive(source, cold)
    assert raw_values(source) == before


def test_v2_cursor_required_retention_cannot_be_relaxed_and_v3_needs_no_consumer(tmp_path):
    source, cold = fixture(tmp_path)
    with pytest.raises(storage.StorageError, match='CONSUMER_FEED_CURSOR'):
        archive(source, cold, consumer_cursor=None)
    with pytest.raises(storage.StorageError, match='72_HOURS'):
        archive(source, cold, cutoff_ms=NOW - 60_000)
    other, other_cold = fixture(tmp_path / 'v3', cohort='2026-10-21-diversified-v3-shadow-r2')
    report = archive(other, other_cold, consumer_cursor=None)
    assert report['cleared_raw_records'] == 3  # IDs1,2,8994; OPEN3 stays hot.


def test_full_backup_and_compact_keep_all_tables_and_archive_outside_quota_glob(tmp_path):
    source, cold = fixture(tmp_path)
    backup = tmp_path / 'rollback' / 'original.sqlite3'
    result = storage.backup_database(source, backup, minimum_free_bytes=0)
    assert result['logical_digest'] == storage.logical_digest(backup, include_raw=True)
    report = archive(source, cold)
    before = storage.logical_digest(source, include_raw=True)
    compact = tmp_path / 'maintenance' / 'compact.sqlite3'
    result = storage.compact_database(source, compact, expected_nonraw_digest=report['nonraw_digest'],
                                      minimum_free_bytes=0)
    assert storage.logical_digest(source, include_raw=True) == before
    assert result['bytes_reclaimed'] >= 0 and not compact.exists()
    assert cold not in source.parent.glob('shadow.sqlite3*')
    assert list(source.parent.glob('shadow.sqlite3*')) == [source]
    assert not Path(str(source) + '-wal').exists()
    assert storage.verify_archive(source, cold)['archive_records'] == 2


def test_live_wal_reader_blocks_compact_and_no_committed_wal_row_is_lost(tmp_path):
    source, cold = fixture(tmp_path)
    writer = open_db(source)
    reader = open_db(source)
    try:
        reader.execute('BEGIN')
        reader.execute('SELECT count(*) FROM snapshots').fetchone()
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('INSERT INTO snapshots VALUES(8996,?,?,?,?)', ('SOLUSDT', NOW, NOW, b'committed-in-wal'))
        writer.commit()
        with pytest.raises((storage.StorageError, sqlite3.OperationalError), match='WAL|locked|quiescent'):
            storage.compact_database(source, tmp_path / 'compact' / 'blocked.sqlite3', minimum_free_bytes=0)
        assert writer.execute('SELECT raw FROM snapshots WHERE id=8996').fetchone()[0] == b'committed-in-wal'
    finally:
        reader.close()
        writer.close()
    assert raw_values(source)[8996] == b'committed-in-wal'


def test_rejects_archive_backup_and_compact_inside_active_data(tmp_path):
    source, cold = fixture(tmp_path)
    for operation in (lambda: archive(source, source.parent / 'archive.sqlite3'),
                      lambda: storage.backup_database(source, source.parent / 'backup.sqlite3'),
                      lambda: storage.compact_database(source, source.parent / 'compact.sqlite3')):
        with pytest.raises(storage.StorageError, match='OUTSIDE_ACTIVE_DATA'):
            operation()


def test_disk_guard_precedes_raw_mutation_and_manifest_or_table_changes_are_detected(tmp_path, monkeypatch):
    source, cold = fixture(tmp_path)
    before = storage.logical_digest(source, include_raw=True)
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda path: type('Disk', (), {'free': 1})())
    with pytest.raises(storage.StorageError, match='INSUFFICIENT_DISK'):
        archive(source, cold)
    assert storage.logical_digest(source, include_raw=True) == before


def test_archive_directory_is_durable_before_first_nulling(tmp_path, monkeypatch):
    source, cold = fixture(tmp_path)
    synced = []
    monkeypatch.setattr(storage, '_fsync_directory', lambda path: synced.append(Path(path)))

    def inspect_before_nulls():
        assert cold.parent in synced
        assert raw_values(source)[1] is not None

    archive(source, cold, _after_archive=inspect_before_nulls)
