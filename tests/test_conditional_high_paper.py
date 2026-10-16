import hashlib
import json
from pathlib import Path
import sqlite3
import zlib

import pytest

from trading_bot.research.conditional_high_paper import (
    ARMS, RULE, Study, canonical, open_source, sha,
)

NOW = 1792124400000
HOUR = 3600000


def source_db(tmp_path):
    path = tmp_path/'feed.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE sources(id TEXT PRIMARY KEY,first_ms INTEGER);
            CREATE TABLE observations(id INTEGER PRIMARY KEY,source_id TEXT,observed_ms INTEGER,
                payload_sha256 TEXT,payload_zlib BLOB);
            CREATE TABLE positions(id INTEGER PRIMARY KEY,source_id TEXT,policy TEXT,arm TEXT,
                symbol TEXT,direction TEXT,entry_ms INTEGER,status TEXT,plan TEXT,result TEXT);
            CREATE TABLE execution_candles(symbol TEXT,open_ms INTEGER,payload TEXT,
                PRIMARY KEY(symbol,open_ms));''')
        manifest = canonical({'mode': 'LOCAL_PAPER_ONLY', 'data_venue': 'https://fapi.binance.com',
            'cohort': 'sqz-promotion-transfer-v1-20261005', 'arms': ['PROMOTE_SOURCE_2R']})
        c.executemany('INSERT INTO meta VALUES(?,?)', [('manifest', manifest),
            ('scan', canonical({'status': 'OK', 'finished_ms': NOW})),
            ('monitor', canonical({'status': 'OK', 'at_ms': NOW}))])
    settings = {'experiment': RULE, 'cohort': 'new-high-test', 'risk_usdt': 2,
        'source_database': str(path), 'source_manifest_sha256': sha(manifest.encode())}
    return path, settings


def add_source(path, *, identity='new', observed=NOW+1000, bucket='HIGH', admitted=True):
    with sqlite3.connect(path) as c:
        c.execute('INSERT INTO sources VALUES(?,?)', (identity, observed))
        payload = {'source_id': identity, 'observed_ms': observed, 'venue': 'https://fapi.binance.com',
            'decisions': {'allowed': {'PROMOTE_SOURCE_2R': True}, 'source_quality_failures': [],
                'conditional_v2': {'score_version': 'conditional_context_v2', 'bucket': bucket}}}
        raw = canonical(payload).encode()
        c.execute('INSERT INTO observations(source_id,observed_ms,payload_sha256,payload_zlib) VALUES(?,?,?,?)',
            (identity, observed, sha(raw), zlib.compress(raw)))
        entry = (observed//60000+1)*60000
        plan = {'venue': 'https://fapi.binance.com', 'risk_usdt': 2, 'max_minutes': 1440,
            'targets': [[2., 1., False, False]], 'observed_ms': observed, 'source_id': identity,
            'cohort': 'old-feed', 'stop': '90', 'funding_rate': None}
        if admitted:
            c.execute('''INSERT INTO positions(source_id,policy,arm,symbol,direction,entry_ms,status,plan,result)
                VALUES(?,'FIRST_OBSERVATION','PROMOTE_SOURCE_2R','BTCUSDT','LONG',?,'CLOSED',?,?)''',
                (identity, entry, canonical(plan), 'not-a-valid-result-selection-must-not-read-this'))
    return entry


def bars(path, entry, candles):
    with sqlite3.connect(path) as c:
        c.executemany('INSERT INTO execution_candles VALUES(?,?,?)',
            [('BTCUSDT', entry+i*60000, canonical([entry+i*60000, *ohlc, '1', entry+i*60000+59999, '1']))
             for i, ohlc in enumerate(candles)])


def test_only_future_first_sources_and_idempotent_resume(tmp_path):
    path, settings = source_db(tmp_path)
    add_source(path, identity='old', observed=NOW-1000)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    assert study.get('registration_watermark') == 1
    entry = add_source(path)
    bars(path, entry, [('100', '121', '99', '120')])
    before = path.read_bytes()
    status = study.cycle(now_ms=entry+60000)
    assert status['observations'] == 1 and not status['errors']
    positions = list(study.db.execute('SELECT * FROM positions'))
    assert len(positions) == 2 and {r['source_id'] for r in positions} == {'new'}
    assert len({r['plan'] for r in positions}) == 1
    a, b = [json.loads(r['result']) for r in positions]
    assert a == b and a['status'] == 'CLOSED' and a['sizing'] == 'STOP_PLUS_EXPENSES'
    assert set(a['cost_comparison']) == {'5bps', '10bps'}
    assert path.read_bytes() == before
    registered = study.get('started_ms')
    study.db.close()
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW+HOUR)
    study.cycle(now_ms=entry+120000)
    assert study.get('started_ms') == registered
    assert study.db.execute('SELECT count(*) FROM positions').fetchone()[0] == 2
    study.db.close()


@pytest.mark.parametrize('bucket,expected', [('HIGH', 2), ('MID', 1), ('LOW', 1)])
def test_frozen_bucket_selection_does_not_read_source_pnl(tmp_path, bucket, expected):
    path, settings = source_db(tmp_path)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    add_source(path, bucket=bucket)
    study.cycle(now_ms=NOW+10000)
    assert study.db.execute('SELECT count(*) FROM positions').fetchone()[0] == expected
    study.db.close()


def test_repeated_source_with_later_high_bucket_never_changes_first_mid(tmp_path):
    path, settings = source_db(tmp_path)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    add_source(path, bucket='MID')
    study.cycle(now_ms=NOW+10000)
    with sqlite3.connect(path) as c:
        payload = json.loads(zlib.decompress(c.execute('SELECT payload_zlib FROM observations').fetchone()[0]))
        payload['decisions']['conditional_v2']['bucket'] = 'HIGH'
        payload['observed_ms'] += 1000
        raw = canonical(payload).encode()
        c.execute('INSERT INTO observations(source_id,observed_ms,payload_sha256,payload_zlib) VALUES(?,?,?,?)',
            ('new', payload['observed_ms'], sha(raw), zlib.compress(raw)))
    study.cycle(now_ms=NOW+20000)
    assert study.db.execute('SELECT count(*) FROM positions').fetchone()[0] == 1
    study.db.close()


def test_source_occupancy_refusal_is_preserved(tmp_path):
    path, settings = source_db(tmp_path)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    add_source(path, admitted=False)
    study.cycle(now_ms=NOW+10000)
    assert study.db.execute('SELECT count(*) FROM positions').fetchone()[0] == 0
    d = json.loads(study.db.execute('SELECT decision FROM observations').fetchone()[0])
    assert d['refusal'] == 'SOURCE_CONTROL_NOT_ADMITTED'
    study.db.close()


def test_expected_quality_rejection_does_not_block_the_next_valid_source(tmp_path):
    path, settings = source_db(tmp_path)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    add_source(path, identity='invalid', admitted=False)
    add_source(path, identity='valid', observed=NOW+2000)
    with sqlite3.connect(path) as c:
        r = c.execute('SELECT payload_zlib FROM observations WHERE id=1').fetchone()
        payload = json.loads(zlib.decompress(r[0]))
        payload['decisions'] = {'allowed': {'PROMOTE_SOURCE_2R': False},
            'source_quality_failures': ['INVALID_TRANSFER_SOURCE']}
        raw = canonical(payload).encode()
        c.execute('UPDATE observations SET payload_zlib=?,payload_sha256=? WHERE id=1',
            (zlib.compress(raw), sha(raw)))
    result = study.cycle(now_ms=NOW+10000)
    assert result['observations'] == 2 and not result['errors']
    assert study.db.execute('SELECT count(*) FROM positions').fetchone()[0] == 2
    study.db.close()


def test_stop_expense_budget_and_open_mark_are_not_reported_as_realized(tmp_path):
    from paper_pilots_api import read_pilot
    path, settings = source_db(tmp_path)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    entry = add_source(path)
    bars(path, entry, [('100', '101', '99', '100')])
    study.cycle(now_ms=entry+60000)
    view = read_pilot('high', 'HIGH', tmp_path/'study', now_ms=entry+60000)
    assert all(g['closed'] == 0 and g['open'] == 1 and g['net_usdt'] == 0 for g in view['groups'])
    bars(path, entry+60000, [('100', '101', '89', '90')])
    study.cycle(now_ms=entry+120000)
    r = json.loads(study.db.execute('SELECT result FROM positions LIMIT 1').fetchone()[0])
    assert r['status'] == 'CLOSED' and -2.00001 <= r['model_pnl_usdt'] < 0
    study.db.close()


def test_changed_runtime_settings_and_source_fail_closed(tmp_path):
    path, settings = source_db(tmp_path)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    study.db.close()
    with pytest.raises(ValueError, match='Frozen'):
        Study(tmp_path/'study', settings, 'changed', now_ms=NOW)
    with pytest.raises(ValueError, match='INVALID'):
        Study(tmp_path/'other', {**settings, 'risk_usdt': 5}, 'fixed', now_ms=NOW)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    with sqlite3.connect(path) as c:
        c.execute("UPDATE meta SET value='{}' WHERE key='manifest'")
    with pytest.raises(ValueError, match='SOURCE_MANIFEST_CHANGED'):
        study.cycle(now_ms=NOW)
    study.db.close()


def test_corrupt_first_observation_blocks_later_rows_without_partial_admission(tmp_path):
    path, settings = source_db(tmp_path)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    add_source(path)
    add_source(path, identity='later', observed=NOW+2000)
    with sqlite3.connect(path) as c:
        c.execute("UPDATE observations SET payload_sha256='bad' WHERE id=1")
    result = study.cycle(now_ms=NOW+10000)
    assert result['health'] == 'DEGRADED' and result['backlog'] == 2
    assert study.db.execute('SELECT count(*) FROM positions').fetchone()[0] == 0
    study.db.close()


def test_missing_first_minute_and_internal_gaps_do_not_create_pnl(tmp_path):
    path, settings = source_db(tmp_path)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    entry = add_source(path)
    bars(path, entry+60000, [('100', '101', '99', '100')])
    result = study.cycle(now_ms=entry+180000)
    assert result['errors'] and all(r[0] is None for r in study.db.execute('SELECT result FROM positions'))
    bars(path, entry, [('100', '101', '99', '100')])
    bars(path, entry+180000, [('100', '121', '99', '120')])
    result = study.cycle(now_ms=entry+240000)
    assert result['errors'] and all(r[0] is None for r in study.db.execute('SELECT result FROM positions'))
    study.db.close()


def test_cost_stress_continues_after_primary_exit(tmp_path):
    path, settings = source_db(tmp_path)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    entry = add_source(path)
    bars(path, entry, [('100', '120.17', '99', '100')])
    study.cycle(now_ms=entry+60000)
    r = json.loads(study.db.execute('SELECT result FROM positions LIMIT 1').fetchone()[0])
    assert r['status'] == 'CLOSED' and r['cost_comparison']['10bps']['with_expenses']['status'] == 'OPEN'
    bars(path, entry+60000, [('100', '121', '99', '100')])
    study.cycle(now_ms=entry+120000)
    r = json.loads(study.db.execute('SELECT result FROM positions LIMIT 1').fetchone()[0])
    assert r['cost_comparison']['10bps']['with_expenses']['status'] == 'CLOSED'
    study.db.close()


def test_source_is_read_only_and_missing_file_is_not_created(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        open_source(tmp_path/'missing.sqlite3')
    assert not (tmp_path/'missing.sqlite3').exists()
    path, _ = source_db(tmp_path)
    c = open_source(path)
    with pytest.raises(sqlite3.OperationalError):
        c.execute("DELETE FROM sources")
    c.close()


def test_polling_does_not_make_stale_source_fresh(tmp_path):
    path, settings = source_db(tmp_path)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    result = study.cycle(now_ms=NOW+HOUR)
    assert result['health'] == 'STALE'
    assert study.get('scan')['finished_ms'] == NOW
    assert study.get('monitor')['at_ms'] == NOW
    study.db.close()


def test_pilot_api_reads_zero_entry_high_control_and_expense_aware_results(tmp_path):
    from paper_pilots_api import read_pilot
    path, settings = source_db(tmp_path)
    study = Study(tmp_path/'study', settings, 'fixed', now_ms=NOW)
    study.cycle(now_ms=NOW+1000)
    view = read_pilot('high', 'HIGH', tmp_path/'study', now_ms=NOW+1000)
    assert view['health'] == 'OK' and len(view['groups']) == 2
    assert all(g['closed'] == 0 for g in view['groups'])
    entry = add_source(path)
    bars(path, entry, [('100', '121', '99', '120')])
    study.cycle(now_ms=entry+60000)
    view = read_pilot('high', 'HIGH', tmp_path/'study', now_ms=entry+60000)
    assert view['health'] == 'OK' and all(g['closed'] == 1 and g['net_usdt'] > 0 for g in view['groups'])
    study.db.close()
