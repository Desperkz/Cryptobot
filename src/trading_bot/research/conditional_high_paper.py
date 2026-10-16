"""Prospective v2 HIGH paper follower. No network or exchange client.

Read the frozen transfer pilot's first observations and minute evidence through
a read-only SQLite snapshot. Select before reading any execution bars/results.
Keep a new registration, journal and matched source-control arm.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import time
import zlib
from contextlib import closing

from trading_bot.research.cost_sizing import compare_sizing

ARMS = ('C2_HIGH_SOURCE_2R', 'C2_HIGH_2R')
SOURCE_ARM = 'PROMOTE_SOURCE_2R'
MINUTE = 60000
RULE = 'sqz-c2-high-forward-v1-20261016'
MAX_RECORD = 8 * 1024**2


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decode_observation(row):
    packed = row['payload_zlib']
    if len(packed) > 2 * 1024**2:
        raise ValueError('OVERSIZED_OBSERVATION')
    decoder = zlib.decompressobj()
    raw = decoder.decompress(packed, MAX_RECORD + 1)
    if len(raw) > MAX_RECORD or not decoder.eof or decoder.unused_data:
        raise ValueError('INVALID_COMPRESSED_OBSERVATION')
    if sha(raw) != row['payload_sha256']:
        raise ValueError('OBSERVATION_HASH_MISMATCH')
    value = json.loads(raw)
    if (value['source_id'] != row['source_id'] or value['observed_ms'] != row['observed_ms']
            or value['venue'] != 'https://fapi.binance.com'):
        raise ValueError('OBSERVATION_IDENTITY_MISMATCH')
    return value


def select_high(payload):
    """Selection depends only on the saved first-observation decision."""
    decision = payload['decisions']
    profile = decision['conditional_v2']
    if (decision['allowed'].get(SOURCE_ARM) is not True
            or decision.get('source_quality_failures') != []
            or profile['score_version'] != 'conditional_context_v2'
            or profile['bucket'] not in ('HIGH', 'MID', 'LOW')):
        raise ValueError('INVALID_SOURCE_DECISION')
    return profile['bucket'] == 'HIGH'


def open_source(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    deadline = time.monotonic() + 20
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return conn


def source_meta(conn):
    return {r['key']: json.loads(r['value']) for r in conn.execute(
        "SELECT key,value FROM meta WHERE key IN ('manifest','scan','monitor')")}


class Study:
    def __init__(self, data_dir, settings, code_sha256, *, now_ms=None):
        self.directory = Path(data_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        if (settings.get('experiment') != RULE or settings.get('risk_usdt') != 2
                or not settings.get('cohort') or len(settings.get('source_manifest_sha256', '')) != 64):
            raise ValueError('INVALID_FROZEN_SETTINGS')
        self.db = sqlite3.connect(self.directory/'mainnet_paper_lab.sqlite3', timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,first_ms INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY,source_id TEXT,
                observed_ms INTEGER,payload_sha256 TEXT,payload_zlib BLOB,decision TEXT);
            CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY,source_id TEXT,
                policy TEXT,arm TEXT,symbol TEXT,direction TEXT,entry_ms INTEGER,
                status TEXT,plan TEXT,result TEXT,UNIQUE(source_id,policy,arm));
        ''')
        self.manifest = {'cohort': settings['cohort'], 'mode': 'LOCAL_PAPER_ONLY',
            'data_venue': 'https://fapi.binance.com', 'arms': list(ARMS),
            'policies': ['FIRST_OBSERVATION'], 'risk_usdt': 2,
            'scan_interval_sec': 300, 'monitor_interval_sec': 300,
            'experiment': RULE, 'code_sha256': code_sha256, 'settings': settings,
            'order_capability': False, 'auto_promotion': False,
            'scope': 'Prospective source-matched v2 HIGH subset of the frozen transfer feed',
            'execution': 'Same source next-minute entry and original stop; single 2R, 24h',
            'sizing': 'STOP_PLUS_EXPENSES primary; fees 4bps per side, 5/10bps slippage comparisons',
            'review': 'At least 14 days and 50 closed entries in control AND HIGH; no automatic promotion',
            'source_dependency': 'Read-only original transfer observations and contiguous minute evidence; delayed publication possible',
            'maximum_database_bytes': 128*1024**2}
        saved = self.get('manifest')
        if saved is not None and saved != self.manifest:
            self.db.close()
            raise ValueError('Frozen experiment changed: use a new cohort directory')
        now = int(time.time()*1000) if now_ms is None else now_ms
        with closing(open_source(settings['source_database'])) as source:
            self.validate_source(source)
            watermark = source.execute('SELECT coalesce(max(id),0) FROM observations').fetchone()[0]
        with self.db:
            if saved is None:
                self.set('manifest', self.manifest)
                self.set('started_ms', now)
                self.set('registration_watermark', watermark)
                self.set('cursor', watermark)

    def get(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, canonical(value)))

    def validate_source(self, source):
        row = source.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
        if row is None or sha(row[0].encode()) != self.settings['source_manifest_sha256']:
            raise ValueError('SOURCE_MANIFEST_CHANGED')
        meta = source_meta(source)
        manifest = meta['manifest']
        if (manifest['mode'] != 'LOCAL_PAPER_ONLY' or manifest['data_venue'] != 'https://fapi.binance.com'
                or manifest['cohort'] != 'sqz-promotion-transfer-v1-20261005'
                or SOURCE_ARM not in manifest['arms']):
            raise ValueError('UNEXPECTED_SOURCE_PILOT')
        return meta

    def admit(self, source, row):
        payload = decode_observation(row)
        if (payload['decisions']['allowed'].get(SOURCE_ARM) is False
                and payload['decisions'].get('source_quality_failures')):
            self.db.execute('INSERT INTO sources VALUES(?,?)', (row['source_id'], row['observed_ms']))
            self.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',
                (row['id'], row['source_id'], row['observed_ms'], row['payload_sha256'],
                 row['payload_zlib'], canonical({'refusal': 'SOURCE_QUALITY_REJECTED',
                    'reasons': payload['decisions']['source_quality_failures']})))
            return
        high = select_high(payload)
        selected = [ARMS[0]] + ([ARMS[1]] if high else [])
        result = {'selected_high': high, 'arms': selected}
        original = source.execute('''SELECT symbol,direction,entry_ms,plan FROM positions
            WHERE source_id=? AND policy='FIRST_OBSERVATION' AND arm=?''',
            (row['source_id'], SOURCE_ARM)).fetchone()
        # Follow the source's shared occupancy/refusals, with no late retry.
        if original is None:
            result['refusal'] = 'SOURCE_CONTROL_NOT_ADMITTED'
        else:
            plan = json.loads(original['plan'])
            if (plan['venue'] != 'https://fapi.binance.com' or plan['risk_usdt'] != 2
                    or plan['max_minutes'] != 1440 or plan['targets'] != [[2., 1., False, False]]
                    or plan['observed_ms'] != row['observed_ms']
                    or plan['source_id'] != row['source_id']
                    or original['entry_ms'] != (row['observed_ms']//MINUTE+1)*MINUTE
                    or original['direction'] not in ('LONG', 'SHORT')
                    or not math.isfinite(float(plan['stop'])) or float(plan['stop']) <= 0):
                raise ValueError('INVALID_SOURCE_PLAN')
            plan.update(source_cohort=plan['cohort'], cohort=self.settings['cohort'],
                        sizing='STOP_PLUS_EXPENSES', source_observation_id=row['id'])
            for arm in selected:
                self.db.execute('''INSERT INTO positions(source_id,policy,arm,symbol,direction,
                    entry_ms,status,plan) VALUES(?,'FIRST_OBSERVATION',?,?,?,?,'PENDING',?)''',
                    (row['source_id'], arm, original['symbol'], original['direction'],
                     original['entry_ms'], canonical(plan)))
        self.db.execute('INSERT INTO sources VALUES(?,?)', (row['source_id'], row['observed_ms']))
        self.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',
            (row['id'], row['source_id'], row['observed_ms'], row['payload_sha256'],
             row['payload_zlib'], canonical(result)))

    def cycle(self, *, now_ms=None):
        now = int(time.time()*1000) if now_ms is None else now_ms
        errors = []
        with closing(open_source(self.settings['source_database'])) as source:
            meta = self.validate_source(source)
            cursor = self.get('cursor')
            # First assignment only, excluding every source already seen at registration.
            rows = source.execute('''SELECT o.* FROM observations o JOIN sources s ON s.id=o.source_id
                WHERE o.id>? AND s.first_ms>=? AND o.observed_ms=s.first_ms
                AND o.id=(SELECT min(f.id) FROM observations f WHERE f.source_id=o.source_id)
                ORDER BY o.id LIMIT 64''', (cursor, self.get('started_ms'))).fetchall()
            storage_ok = (sum(p.stat().st_size for p in self.directory.glob('mainnet_paper_lab.sqlite3*'))
                          < 128*1024**2 and shutil.disk_usage(self.directory).free > 512*1024**2)
            if storage_ok:
                for row in rows:
                    try:
                        # Commit the frozen selection before accessing minute bars.
                        with self.db:
                            self.admit(source, row)
                            self.set('cursor', row['id'])
                    except (ValueError, TypeError, KeyError, zlib.error, sqlite3.Error) as exc:
                        errors.append({'observation': row['id'], 'error': str(exc)[:200]})
                        break  # A damaged first observation must never turn into a later assignment.
            else:
                errors.append({'error': 'ADMISSIONS_PAUSED_STORAGE'})
            backlog = source.execute('''SELECT count(*) FROM observations o JOIN sources s ON s.id=o.source_id
                WHERE o.id>? AND s.first_ms>=? AND o.observed_ms=s.first_ms
                AND o.id=(SELECT min(f.id) FROM observations f WHERE f.source_id=o.source_id)''',
                (self.get('cursor'), self.get('started_ms'))).fetchone()[0]
            active = self.db.execute('''SELECT * FROM positions WHERE status IN ('PENDING','OPEN')
                OR json_extract(result,'$.cost_comparison."10bps".with_expenses.status')='OPEN' ''').fetchall()
            for position in active:
                try:
                    plan = json.loads(position['plan'])
                    end = min(now//MINUTE*MINUTE, position['entry_ms']+1440*MINUTE)
                    bars = [json.loads(r[0]) for r in source.execute('''SELECT payload FROM execution_candles
                        WHERE symbol=? AND open_ms>=? AND open_ms<? ORDER BY open_ms''',
                        (position['symbol'], position['entry_ms'], end))]
                    if not bars:
                        if now-position['entry_ms'] > 900000:
                            errors.append({'position': position['id'], 'error': 'MINUTE_EVIDENCE_DELAYED'})
                        continue
                    if int(bars[0][0]) != position['entry_ms']:
                        raise ValueError('MISSING_ENTRY_MINUTE')
                    if len(bars)<1440 and end-int(bars[-1][6])-1>900000:
                        errors.append({'position': position['id'], 'error': 'MINUTE_EVIDENCE_DELAYED'})
                    comparison = compare_sizing(bars, float(plan['stop']), position['direction'],
                        plan['targets'], complete=len(bars)==1440, funding_rate=plan['funding_rate'], budget=2.)
                    primary = comparison['5bps']['with_expenses']
                    result = {**primary, 'cost_comparison': comparison,
                              'source_observation_id': plan['source_observation_id']}
                    with self.db:
                        self.db.execute('UPDATE positions SET status=?,result=? WHERE id=?',
                            (primary['status'], canonical(result), position['id']))
                except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                    errors.append({'position': position['id'], 'error': str(exc)[:200]})
            # Source timestamps preserve freshness; polling never launders stale inputs.
            scan = {**meta.get('scan', {}), 'backlog': backlog}
            if errors or backlog:
                scan['status'] = 'DEGRADED'
            with self.db:
                self.set('scan', scan)
                self.set('monitor', {'at_ms': meta.get('monitor', {}).get('at_ms'),
                    'status': 'DEGRADED' if errors else meta.get('monitor', {}).get('status', 'DEGRADED'),
                    'errors': errors, 'active_checked': len(active)})
                self.set('cost_monitor', {'at_ms': now, 'status': 'DEGRADED' if errors else 'OK', 'errors': errors})
        stamp = self.get('monitor').get('at_ms')
        scan_stamp = self.get('scan').get('finished_ms')
        fresh = all(isinstance(t, (int, float)) and 0 <= now-t <= 900000 for t in (stamp, scan_stamp))
        status = {'at_ms': now, 'health': 'DEGRADED' if errors or backlog else 'OK' if fresh else 'STALE',
            'execution': 'LOCAL_PAPER_ONLY', 'order_capability': False, 'auto_promotion': False,
            'cohort': self.settings['cohort'], 'registered_ms': self.get('started_ms'),
            'registration_watermark': self.get('registration_watermark'),
            'observations': self.db.execute('SELECT count(*) FROM observations').fetchone()[0],
            'positions': [dict(r) for r in self.db.execute('SELECT arm,status,count(*) n FROM positions GROUP BY arm,status')],
            'backlog': backlog, 'errors': errors}
        temp = self.directory/'status.tmp'
        temp.write_text(json.dumps(status, indent=2))
        temp.replace(self.directory/'status.json')
        return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', required=True)
    parser.add_argument('--data-dir', required=True)
    args = parser.parse_args()
    settings = json.loads(Path(args.settings).read_text())
    # The frozen dependency closure is deliberately small and has no HTTP capability.
    code = [Path(__file__), Path(__file__).with_name('cost_sizing.py')]
    fingerprint = sha(canonical({p.name: sha(p.read_bytes()) for p in code}).encode())
    study = Study(args.data_dir, settings, fingerprint)
    try:
        print(json.dumps(study.cycle()))
    finally:
        study.db.close()


if __name__ == '__main__':
    main()
