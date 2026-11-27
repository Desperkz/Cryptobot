"""Export only VWAP plans, derived inputs and settlements from the frozen V3 feed."""
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import time
import zlib

SOURCE = Path('/root/bot_diversified_v3/data/shadow.sqlite3')
ARCHIVE = Path('/root/bot_research_storage/archives/bot_diversified_v3.sqlite3')
GATE = Path('/root/diversified_v4_research_20261122/diversified_v4_gates.py')
GATE_HASH = '5ad43997e4cbc6a263492102a72b13fd4eafc8e20bb4b5f3ad350cdd23f6dee4'
MANIFEST_HASH = '7254d8a4904cb905ae8c6f0b8dc7f87f2ab286c8352d6e6afb04c78d43e1b7a0'
FIELDS = ('id','strategy','symbol','direction','opened_ms','entry_mid','entry',
          'stop','target','quantity','risk_usdt','expires_ms','snapshot_id')


def readonly(path):
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA cache_size=-512')
    conn.execute('BEGIN')
    return conn


def allow_position_columns(*extra):
    columns = set(FIELDS) | set(extra) | {''}

    def authorize(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and table == 'positions' and column not in columns:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load_snapshot(source, archive, snapshot_id):
    row = source.execute('SELECT id,symbol,bar_ms,observed_ms,raw FROM snapshots WHERE id=?',
                         (snapshot_id,)).fetchone()
    if row is None:
        raise ValueError('SOURCE_SNAPSHOT_MISSING')
    snapshot = {k: row[k] for k in ('id','symbol','bar_ms','observed_ms')}
    blob = row['raw']
    location = 'HOT'
    if blob is None:
        archived = archive.execute('SELECT * FROM archived_snapshots WHERE id=?', (snapshot_id,)).fetchone()
        if archived is None:
            raise ValueError('ARCHIVED_SNAPSHOT_MISSING')
        blob = archived['raw']
        if any(archived[k] != v for k, v in snapshot.items()):
            raise ValueError('ARCHIVE_METADATA_MISMATCH')
        if sha(blob) != archived['raw_sha256'] or len(blob) != archived['raw_bytes']:
            raise ValueError('ARCHIVE_BLOB_MISMATCH')
        location = 'COLD'
    if not isinstance(blob, bytes) or len(blob) > 2 * 1024**2:
        raise ValueError('INVALID_SNAPSHOT_SIZE')
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(blob, 8 * 1024**2 + 1)
    if len(decoded) > 8 * 1024**2 or not decoder.eof or decoder.unused_data:
        raise ValueError('INVALID_SNAPSHOT_PAYLOAD')
    return snapshot, json.loads(decoded), location, sha(blob)


def export():
    if sha(GATE.read_bytes()) != GATE_HASH:
        raise ValueError('FROZEN_GATE_CHANGED')
    spec = importlib.util.spec_from_file_location('gates', GATE)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    strategy = 'VWAP_REVERSION_V3_RECLAIM'
    rows = []
    with closing(readonly(SOURCE)) as source, closing(readonly(ARCHIVE)) as archive:
        source.set_authorizer(allow_position_columns())
        manifest = source.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()[0]
        if sha(manifest.encode()) != MANIFEST_HASH:
            raise ValueError('FROZEN_SOURCE_CHANGED')
        captured_ms = int(time.time() * 1000)
        watermark = source.execute('SELECT max(id) FROM positions').fetchone()[0]
        count = source.execute('SELECT count(*) FROM positions WHERE strategy=?', (strategy,)).fetchone()[0]
        if count > 500:
            raise ValueError('SOURCE_LIMIT_EXCEEDED')
        plans = source.execute('SELECT ' + ','.join(FIELDS) + ' FROM positions WHERE strategy=? ORDER BY id',
                               (strategy,)).fetchall()
        for row in plans:
            item = {'plan': dict(row), 'snapshot': None}
            try:
                snapshot, raw, location, blob_hash = load_snapshot(source, archive, row['snapshot_id'])
                item['snapshot'] = snapshot
                features = gate.extract(item['plan'], snapshot, raw)
                diagnostic = source.execute('SELECT detail FROM observations WHERE strategy=? AND snapshot_id=?',
                                            (strategy, snapshot['id'])).fetchone()
                if diagnostic is None or json.loads(diagnostic[0])['hourly_trend'] != features['hourly_trend']:
                    raise ValueError('ORIGINAL_TREND_MISMATCH')
                item.update(features=features, decision=gate.evaluate(features), storage=location, raw_sha256=blob_hash)
            except (ValueError, TypeError, KeyError, zlib.error) as exc:
                item['input_error'] = str(exc)
            rows.append(item)
        source.set_authorizer(allow_position_columns('status', 'closed_ms', 'exit_price', 'net_usdt', 'reason', 'costs'))
        for item in rows:
            item['outcome'] = dict(source.execute(
                'SELECT status,closed_ms,exit_price,net_usdt,reason,costs FROM positions WHERE id=?',
                (item['plan']['id'],)).fetchone())
    return {'captured_ms': captured_ms, 'watermark': watermark, 'manifest_sha256': MANIFEST_HASH,
            'gate_sha256': GATE_HASH, 'rows': rows}


if __name__ == '__main__':
    print(json.dumps(export(), sort_keys=True, separators=(',', ':'), allow_nan=False))
