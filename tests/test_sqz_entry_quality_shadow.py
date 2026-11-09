import hashlib
import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from trading_bot.research.sqz_entry_quality_shadow import (
    COHORT, STUDY, MAX_PAYLOAD, canonical, cycle, decode_observation,
    quality_decision, open_store, readonly,
)

SETTINGS = {'study': STUDY, 'cohort': COHORT, 'mode': 'SHADOW', 'order_capability': False}
REGISTERED = 3600000
OBSERVED = REGISTERED + 10000
ENTRY = 3660000
NOW = ENTRY + 120000
MANIFEST = {'mode': 'LOCAL_PAPER_ONLY', 'data_venue': 'https://fapi.binance.com',
            'arms': ['BASELINE_2R'], 'policies': ['FIRST_OBSERVATION'], 'risk_usdt': 2,
            'entry': 'next_minute_open_after_observation_plus_5bps',
            'fees_bps_each_side': 4, 'exit_slippage_bps': 5, 'maximum_holding_minutes': 1440}


@pytest.fixture
def source(tmp_path):
    path = tmp_path/'source.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE observations(id INTEGER PRIMARY KEY,source_id TEXT,observed_ms INTEGER,
                payload_sha256 TEXT,payload_zlib BLOB);
            CREATE TABLE positions(id INTEGER PRIMARY KEY,source_id TEXT,policy TEXT,arm TEXT,
                symbol TEXT,direction TEXT,entry_ms INTEGER,status TEXT,plan TEXT,result TEXT);
            CREATE TABLE execution_candles(symbol TEXT,open_ms INTEGER,payload TEXT,
                PRIMARY KEY(symbol,open_ms));''')
        db.execute('INSERT INTO meta VALUES(?,?)', ('manifest', canonical(MANIFEST)))
        db.execute('INSERT INTO meta VALUES(?,?)', ('scan', canonical(
            {'started_ms': REGISTERED, 'finished_ms': OBSERVED, 'status': 'OK', 'errors': 0})))
        db.execute('INSERT INTO meta VALUES(?,?)', ('monitor', canonical(
            {'at_ms': OBSERVED, 'status': 'OK', 'errors': []})))
    return path


@pytest.fixture
def code_root(tmp_path):
    root = tmp_path/'code'
    model = root/'src/trading_bot/research/paper_execution.py'
    model.parent.mkdir(parents=True)
    model.write_text('# frozen model\n', encoding='utf-8')
    return root


def start(tmp_path, source, code_root, at=REGISTERED):
    return open_store(tmp_path/'out', source, SETTINGS, code_root, at)


def add(source, pid=1, *, direction='LONG', opened=101., pnl=3., state='CLOSED',
        observed=OBSERVED, bar_close=None, with_bar=True, observed_market=99.,
        volume='3.5', breakout='.5'):
    closed = REGISTERED+pid-1 if bar_close is None else bar_close
    symbol = f'COIN{pid}USDT'
    source_id = f'SQZ:{symbol}:{direction}:{closed}'
    stop = '90' if direction == 'LONG' else '110'
    entry = (observed//60000+1)*60000
    plan = {'source_id': source_id, 'venue': 'https://fapi.binance.com',
            'stop': stop, 'signal_entry': '100', 'targets': [[2., 1., False, False]],
            'risk_usdt': 2., 'max_minutes': 1440, 'funding_rate': None,
            'observed_ms': observed, 'exit_model': 'minute_ohlc_conservative_v1'}
    payload = {'source_id': source_id, 'observed_ms': observed,
               'signal': {'symbol': symbol, 'direction': direction, 'entry_price': '100',
                          'stop_loss': stop, 'metadata': {'signal_bar_close_time': closed,
                          'compression_low': '99', 'compression_high': '100',
                          'volume_ratio': volume, 'breakout_atr': breakout, 'atr_pct': '1.0',
                          'source_hour_close_time': REGISTERED-1, 'regime': 'RANGE'}},
               'evidence': {'public_requests': {'/fapi/v1/ticker/24hr:test': {
                   'at_ms': observed-1, 'data': {'lastPrice': str(observed_market)}}}}}
    raw = canonical(payload).encode()
    result = {'status': state, 'R': pnl/2, 'model_pnl_usdt': pnl,
              'entry_ms': entry, 'last_ms': entry+59999,
              'entry_price': opened*(1+(.0005 if direction == 'LONG' else -.0005)),
              'gross_R': pnl/2+.4, 'fees_R': .2, 'exit_slippage_R': .1, 'funding_R': .1}
    bar = [entry, str(opened), str(opened+1), str(opened-1), str(opened), '5', entry+59999]
    with sqlite3.connect(source) as db:
        db.execute('INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (pid, source_id, 'FIRST_OBSERVATION', 'BASELINE_2R', symbol, direction,
                    entry, state, canonical(plan), canonical(result)))
        db.execute('INSERT INTO observations VALUES(?,?,?,?,?)',
                   (pid, source_id, observed, hashlib.sha256(raw).hexdigest(), zlib.compress(raw)))
        if with_bar:
            db.execute('INSERT INTO execution_candles VALUES(?,?,?)', (symbol, entry, canonical(bar)))
    return plan, bar


@pytest.mark.parametrize('volume,breakout,bucket,kept', [
    ('2.5', '.3', 'BOTH_PASS', True), ('2.499', '.3', 'VOLUME_ONLY_FAIL', False),
    ('2.5', '.299999999999999999999', 'BREAKOUT_ONLY_FAIL', False),
    ('2.499', '.299', 'BOTH_FAIL', False), ('0', '0', 'BOTH_FAIL', False),
])
@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_exact_inclusive_and_direction_normalized_metadata(volume, breakout, bucket, kept, direction):
    # SHORT breakout_atr is positive in the original metadata, not negated here.
    decision = quality_decision({'volume_ratio': volume, 'breakout_atr': breakout, 'direction': direction})
    assert decision['bucket'] == bucket and decision['kept'] == kept
    assert decision['minimum_volume_ratio'] == '2.5' and decision['minimum_breakout_atr'] == '0.3'


@pytest.mark.parametrize('value', [None, 'NaN', 'Infinity', '-.1', True, 'not a number'])
def test_invalid_quality_values_not_silently_zero_or_kept(value):
    with pytest.raises(ValueError):
        quality_decision({'volume_ratio': value, 'breakout_atr': '.5'})


def test_four_buckets_accounting_accepted_identical_rejected_zero_no_mtm(tmp_path, source, code_root):
    db, reg = start(tmp_path, source, code_root)
    add(source, pid=1, pnl=3.)
    add(source, pid=2, pnl=-2., volume='2.4')
    add(source, pid=3, pnl=4., breakout='.2')
    add(source, pid=4, pnl=-3., volume='2.4', breakout='.2')
    add(source, pid=5, pnl=999., state='OPEN')
    value = cycle(db, source, reg, NOW)
    assert value['study'] == STUDY and value['cohort'] == COHORT
    assert value['counts'] == {'sources': 5, 'waiting_entry': 0, 'decided': 5, 'kept': 2,
        'rejected': 3, 'unscorable': 0, 'open_controls': 1, 'closed_pairs': 4}
    assert value['pnl'] == {'control_net_usdt': 2., 'recheck_net_usdt': 3., 'delta_usdt': 1.,
        'avoided_losses_usdt': 5., 'lost_profits_usdt': 4.}
    assert value['quality_rule'] == {'min_volume_ratio': 2.5, 'min_breakout_atr': .3,
        'comparison': 'AND', 'inclusive': True}
    buckets = {b['bucket']: b for b in value['quality_buckets']}
    assert set(buckets) == {'BOTH_PASS', 'VOLUME_ONLY_FAIL', 'BREAKOUT_ONLY_FAIL', 'BOTH_FAIL'}
    assert buckets['BOTH_PASS']['n_sources'] == 2 and buckets['BOTH_PASS']['n_closed'] == 1
    assert sum(b['n_sources'] for b in buckets.values()) == 5
    assert sum(b['control_net_usdt'] for b in buckets.values()) == 2.
    assert buckets['BREAKOUT_ONLY_FAIL']['lost_profits_usdt'] == 4.
    assert value['review']['accepted_closed'] == 1 and not value['review']['ready']
    db.close()


def test_quality_decision_committed_before_any_outcome_or_terminal_status_query(tmp_path, source, code_root, monkeypatch):
    import trading_bot.research.sqz_entry_quality_shadow as worker
    db, reg = start(tmp_path, source, code_root)
    add(source, state='CLOSED')  # Catch-up must not use the already known outcome.
    original = worker.readonly
    checks = []

    def traced(path):
        conn = original(path)
        def trace(sql):
            if sql.startswith('SELECT status'):
                with sqlite3.connect(tmp_path/'out'/worker.DATABASE) as independent:
                    checks.append(independent.execute('SELECT decision FROM sources').fetchone()[0] is not None)
        conn.set_trace_callback(trace)
        return conn
    monkeypatch.setattr(worker, 'readonly', traced)
    cycle(db, source, reg, NOW)
    assert checks and all(checks)
    db.close()


def test_raw_open_inside_range_is_diagnostic_not_third_gate(tmp_path, source, code_root):
    db, reg = start(tmp_path, source, code_root)
    add(source, opened=99.9, observed_market=99.9, volume='2.5', breakout='.3')
    value = cycle(db, source, reg, NOW)
    row = db.execute('SELECT decision,entry_evidence FROM sources').fetchone()
    assert json.loads(row['decision'])['kept'] is True
    assert json.loads(row['entry_evidence'])['raw_breakout_preserved'] is False
    assert value['pnl']['recheck_net_usdt'] == value['pnl']['control_net_usdt'] == 3.
    assert value['transitions'] == {'INSIDE->INSIDE': 1}
    db.close()


def test_decision_exists_while_exact_entry_minute_missing_without_shifting(tmp_path, source, code_root):
    db, reg = start(tmp_path, source, code_root)
    _, bar = add(source, with_bar=False)
    wrong = list(bar); wrong[0] += 60000; wrong[6] += 60000
    with sqlite3.connect(source) as src:
        src.execute('INSERT INTO execution_candles VALUES(?,?,?)', ('COIN1USDT', wrong[0], canonical(wrong)))
    first = cycle(db, source, reg, NOW)
    assert first['counts']['decided'] == 1 and first['counts']['waiting_entry'] == 1
    assert first['counts']['open_controls'] == 1 and first['counts']['closed_pairs'] == 0
    before = db.execute('SELECT decision FROM sources').fetchone()[0]
    with sqlite3.connect(source) as src:
        src.execute('INSERT INTO execution_candles VALUES(?,?,?)', ('COIN1USDT', bar[0], canonical(bar)))
    second = cycle(db, source, reg, NOW+1)
    assert second['counts']['closed_pairs'] == 1
    assert db.execute('SELECT decision FROM sources').fetchone()[0] == before
    db.close()


def test_watermark_old_signal_and_future_race_are_not_mixed_into_new_series(tmp_path, source, code_root):
    add(source, pid=1)
    db, reg = start(tmp_path, source, code_root)
    assert reg['source_watermark'] == 1
    add(source, pid=2, bar_close=REGISTERED-1)
    future = NOW+60000
    add(source, pid=3, observed=future, bar_close=future-1)
    first = cycle(db, source, reg, NOW)
    assert first['counts']['sources'] == 0
    assert db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0] == '2'
    second = cycle(db, source, reg, future+120000)
    assert second['counts']['sources'] == 1 and second['counts']['closed_pairs'] == 1
    db.close()


def test_invalid_source_no_cash_and_restart_cannot_double_count(tmp_path, source, code_root):
    db, reg = start(tmp_path, source, code_root)
    add(source, pid=1, state='INVALID_ENTRY', opened=80., pnl=-999.)
    add(source, pid=2, pnl=3.)
    before = source.read_bytes()
    first = cycle(db, source, reg, NOW)
    assert first['counts']['unscorable'] == 1 and first['counts']['closed_pairs'] == 1
    assert first['pnl']['control_net_usdt'] == 3.
    db.close()
    db, reg2 = start(tmp_path, source, code_root, NOW)
    second = cycle(db, source, reg2, NOW+1)
    assert first['pnl'] == second['pnl'] and first['counts'] == second['counts']
    assert source.read_bytes() == before
    with readonly(source) as ro:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute('DELETE FROM positions')
    db.close()


@pytest.mark.parametrize('field,value', [('volume_ratio', None), ('breakout_atr', 'NaN'),
    ('atr_pct', None), ('atr_pct', '0'), ('source_hour_close_time', None)])
def test_missing_or_invalid_source_metadata_visible_unscorable(tmp_path, source, code_root, field, value):
    db, reg = start(tmp_path, source, code_root)
    add(source)
    with sqlite3.connect(source) as src:
        compressed = src.execute('SELECT payload_zlib FROM observations').fetchone()[0]
        payload = json.loads(zlib.decompress(compressed))
        payload['signal']['metadata'][field] = value
        raw = canonical(payload).encode()
        src.execute('UPDATE observations SET payload_sha256=?,payload_zlib=?',
                    (hashlib.sha256(raw).hexdigest(), zlib.compress(raw)))
    value = cycle(db, source, reg, NOW)
    assert value['counts']['unscorable'] == 1 and value['counts']['closed_pairs'] == 0
    assert value['health'] == 'DEGRADED' and all(v == 0 for v in value['pnl'].values())
    db.close()


@pytest.mark.parametrize('name', ['scan', 'monitor'])
def test_stale_source_does_not_advance_or_hide_dead_producer(tmp_path, source, code_root, name):
    db, reg = start(tmp_path, source, code_root)
    add(source)
    with sqlite3.connect(source) as src:
        saved = src.execute('SELECT value FROM meta WHERE key=?', (name,)).fetchone()[0]
        value = json.loads(saved)
        value['finished_ms' if name == 'scan' else 'at_ms'] = NOW-1200001
        src.execute('UPDATE meta SET value=? WHERE key=?', (canonical(value), name))
    first = cycle(db, source, reg, NOW)
    assert first['health'] == 'DEGRADED' and first['counts']['sources'] == 0
    assert db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0] == '0'
    with sqlite3.connect(source) as src:
        src.execute('UPDATE meta SET value=? WHERE key=?', (saved, name))
    assert cycle(db, source, reg, NOW+1)['counts']['closed_pairs'] == 1
    db.close()


@pytest.mark.parametrize('changed', [{'entry_price': 999.}, {'fees_R': 99.}, {'model_pnl_usdt': 999.}])
def test_invalid_effective_entry_or_components_not_imported(tmp_path, source, code_root, changed):
    db, reg = start(tmp_path, source, code_root)
    add(source)
    with sqlite3.connect(source) as src:
        result = json.loads(src.execute('SELECT result FROM positions').fetchone()[0])
        result.update(changed)
        src.execute('UPDATE positions SET result=?', (canonical(result),))
    value = cycle(db, source, reg, NOW)
    assert value['counts']['unscorable'] == 1 and value['counts']['closed_pairs'] == 0
    assert all(v == 0 for v in value['pnl'].values())
    db.close()


def test_frozen_identity_and_payload_limit(tmp_path, source, code_root):
    db, reg = start(tmp_path, source, code_root)
    db.close()
    (code_root/'src/trading_bot/research/paper_execution.py').write_text('# different model\n')
    with pytest.raises(ValueError, match='FROZEN'):
        start(tmp_path, source, code_root)
    raw = b' '*(MAX_PAYLOAD+1)
    with pytest.raises(ValueError, match='TOO_LARGE'):
        decode_observation({'payload_zlib': zlib.compress(raw), 'payload_sha256': hashlib.sha256(raw).hexdigest()})
