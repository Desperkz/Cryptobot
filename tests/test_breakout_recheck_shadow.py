import hashlib
import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from trading_bot.research.breakout_recheck_shadow import (
    COHORT, STUDY, MAX_PAYLOAD, canonical, cycle, decode_observation,
    entry_decision, open_store, readonly,
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
        observed=OBSERVED, bar_close=None, with_bar=True, observed_market=99.):
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
                          'volume_ratio': '3.5', 'regime': 'RANGE'}},
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


def test_future_watermark_and_old_signal_not_admitted(tmp_path, source, code_root):
    add(source, pid=1)  # Even future-dated existing IDs are excluded at registration.
    db, registration = start(tmp_path, source, code_root)
    assert registration['source_watermark'] == 1
    add(source, pid=2, bar_close=REGISTERED-1)
    add(source, pid=3)
    value = cycle(db, source, registration, NOW)
    assert value['counts']['sources'] == 1 and value['counts']['closed_pairs'] == 1
    assert [r[0] for r in db.execute('SELECT id FROM sources')] == [3]
    assert db.execute('SELECT reason FROM excluded').fetchone()[0] == 'PRE_REGISTRATION'
    db.close()


def test_racing_future_source_keeps_cursor_until_clock_catches_up(tmp_path, source, code_root):
    db, registration = start(tmp_path, source, code_root)
    future = NOW+60000
    add(source, observed=future, bar_close=future-1)
    value = cycle(db, source, registration, NOW)
    assert value['counts']['sources'] == 0
    assert db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0] == '0'
    later = cycle(db, source, registration, future+120000)
    assert later['counts']['closed_pairs'] == 1
    db.close()


def test_exact_missing_minute_waits_and_does_not_shift_entry(tmp_path, source, code_root):
    db, registration = start(tmp_path, source, code_root)
    plan, bar = add(source, with_bar=False)
    wrong = list(bar); wrong[0] += 60000; wrong[6] += 60000
    with sqlite3.connect(source) as src:
        src.execute('INSERT INTO execution_candles VALUES(?,?,?)', ('COIN1USDT', wrong[0], canonical(wrong)))
    first = cycle(db, source, registration, NOW)
    assert first['counts']['waiting_entry'] == 1 and first['counts']['closed_pairs'] == 0
    with sqlite3.connect(source) as src:
        src.execute('INSERT INTO execution_candles VALUES(?,?,?)', ('COIN1USDT', bar[0], canonical(bar)))
    second = cycle(db, source, registration, NOW+1)
    assert second['counts']['closed_pairs'] == 1
    assert json.loads(db.execute('SELECT decision FROM sources').fetchone()[0])['entry_ms'] == ENTRY
    db.close()


def test_lost_winner_avoided_loss_and_unchanged_accepted_result(tmp_path, source, code_root):
    db, registration = start(tmp_path, source, code_root)
    add(source, pid=1, opened=99., pnl=3.)
    add(source, pid=2, opened=100., pnl=-2.2)
    add(source, pid=3, opened=101., pnl=4.)
    value = cycle(db, source, registration, NOW)
    assert value['counts'] == {'sources': 3, 'waiting_entry': 0, 'decided': 3, 'kept': 1,
                               'rejected': 2, 'unscorable': 0, 'open_controls': 0, 'closed_pairs': 3}
    assert value['pnl'] == pytest.approx({'control_net_usdt': 4.8, 'recheck_net_usdt': 4.,
         'delta_usdt': -.8, 'avoided_losses_usdt': 2.2, 'lost_profits_usdt': 3.})
    assert value['transitions'] == {'INSIDE->INSIDE': 2, 'INSIDE->OUTSIDE': 1}
    result = json.loads(db.execute('SELECT control_result FROM sources WHERE id=3').fetchone()[0])
    assert result['model_pnl_usdt'] == 4. and result['R'] == 2.
    assert not value['review']['ready'] and not value['automatic_promotion']
    db.close()


def test_invalid_and_open_controls_never_create_realized_pnl(tmp_path, source, code_root):
    db, registration = start(tmp_path, source, code_root)
    add(source, pid=1, opened=80., pnl=-999., state='INVALID_ENTRY')
    add(source, pid=2, pnl=999., state='OPEN')
    value = cycle(db, source, registration, NOW)
    assert value['counts']['unscorable'] == 1 and value['counts']['open_controls'] == 1
    assert value['counts']['closed_pairs'] == 0 and value['health'] == 'DEGRADED'
    assert all(x == 0 for x in value['pnl'].values())
    db.close()


def test_restart_frozen_identity_readonly_and_no_double_count(tmp_path, source, code_root):
    db, registration = start(tmp_path, source, code_root)
    add(source)
    before = source.read_bytes()
    first = cycle(db, source, registration, NOW)
    db.close()
    db, second_registration = start(tmp_path, source, code_root, NOW)
    second = cycle(db, source, second_registration, NOW+1)
    assert registration == second_registration and first['pnl'] == second['pnl']
    assert second['counts']['closed_pairs'] == 1 and source.read_bytes() == before
    with readonly(source) as ro:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute('DELETE FROM positions')
    db.close()
    (code_root/'src/trading_bot/research/paper_execution.py').write_text('# changed\n')
    with pytest.raises(ValueError, match='FROZEN'):
        start(tmp_path, source, code_root)


@pytest.mark.parametrize('direction,raw,boundary,expected', [
    ('LONG', 99.99, 100., False), ('LONG', 100., 100., False),
    ('LONG', 100.01, 100., True), ('SHORT', 100.01, 100., False),
    ('SHORT', 100., 100., False), ('SHORT', 99.99, 100., True),
])
def test_gate_uses_raw_open_not_slipped_fill(direction, raw, boundary, expected):
    # 5bps would move these prices across the boundary; it must not affect gate.
    plan = {'direction': direction, 'entry_ms': ENTRY, 'compression_low': boundary,
            'compression_high': boundary, 'stop': 90 if direction == 'LONG' else 110,
            'observed_market': None}
    bar = [ENTRY, raw, raw+1, raw-1, raw, 1, ENTRY+59999]
    assert entry_decision(plan, bar, NOW)['kept'] == expected


def test_decision_committed_before_outcome_read(tmp_path, source, code_root, monkeypatch):
    import trading_bot.research.breakout_recheck_shadow as worker
    db, registration = start(tmp_path, source, code_root)
    add(source)
    statements = []
    committed_checks = []
    original = worker.readonly

    def traced(path):
        conn = original(path)

        def trace(sql):
            statements.append(sql)
            if sql.startswith('SELECT status,result'):
                # A separate reader sees a durable decision, not an uncommitted write.
                with sqlite3.connect(tmp_path/'out'/worker.DATABASE) as independent:
                    committed_checks.append(independent.execute('SELECT decision FROM sources').fetchone()[0] is not None)
        conn.set_trace_callback(trace)
        return conn
    monkeypatch.setattr(worker, 'readonly', traced)
    cycle(db, source, registration, NOW)
    assert any(s.startswith('SELECT status,result') for s in statements)
    assert committed_checks and all(committed_checks)
    assert db.execute('SELECT control_result FROM sources').fetchone()[0] is not None
    db.close()


def test_bad_payload_visible_unscorable_and_manifest_changes_rejected(tmp_path, source, code_root):
    db, registration = start(tmp_path, source, code_root)
    add(source)
    with sqlite3.connect(source) as src:
        src.execute("UPDATE observations SET payload_sha256='broken'")
    value = cycle(db, source, registration, NOW)
    assert value['counts']['unscorable'] == 1 and value['health'] == 'DEGRADED'
    assert value['counts']['closed_pairs'] == 0
    with sqlite3.connect(source) as src:
        src.execute('UPDATE meta SET value=?', (canonical({**MANIFEST, 'changed': True}),))
    with pytest.raises(ValueError, match='FROZEN'):
        cycle(db, source, registration, NOW)
    db.close()


def test_payload_decompression_is_bounded():
    raw = b' '*(MAX_PAYLOAD+1)
    row = {'payload_zlib': zlib.compress(raw), 'payload_sha256': hashlib.sha256(raw).hexdigest()}
    with pytest.raises(ValueError, match='TOO_LARGE'):
        decode_observation(row)


def test_waiting_missing_observation_preserves_source_cursor(tmp_path, source, code_root):
    db, registration = start(tmp_path, source, code_root)
    add(source)
    with sqlite3.connect(source) as src:
        observation = src.execute('SELECT * FROM observations').fetchone()
        src.execute('DELETE FROM observations')
    assert cycle(db, source, registration, NOW)['counts']['sources'] == 0
    assert db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0] == '0'
    with sqlite3.connect(source) as src:
        src.execute('INSERT INTO observations VALUES(?,?,?,?,?)', observation)
    assert cycle(db, source, registration, NOW+1)['counts']['closed_pairs'] == 1
    db.close()


def test_unclosed_entry_bar_is_retried_without_using_its_future_path(tmp_path, source, code_root):
    db, registration = start(tmp_path, source, code_root)
    add(source)
    first = cycle(db, source, registration, ENTRY+1)
    assert first['counts']['waiting_entry'] == 1 and first['counts']['unscorable'] == 0
    later = cycle(db, source, registration, NOW)
    assert later['counts']['closed_pairs'] == 1 and later['counts']['kept'] == 1
    db.close()


def test_malformed_closed_result_does_not_erase_frozen_decision(tmp_path, source, code_root):
    db, registration = start(tmp_path, source, code_root)
    add(source)
    with sqlite3.connect(source) as src:
        result = json.loads(src.execute('SELECT result FROM positions').fetchone()[0])
        result['model_pnl_usdt'] = 999
        src.execute('UPDATE positions SET result=?', (canonical(result),))
    value = cycle(db, source, registration, NOW)
    assert value['counts']['closed_pairs'] == 0 and value['counts']['unscorable'] == 1
    row = db.execute('SELECT decision,control_result,error FROM sources').fetchone()
    assert json.loads(row['decision'])['kept'] and row['control_result'] is None
    assert row['error'] == 'INVALID_CLOSED_CONTROL_RESULT'
    assert all(x == 0 for x in value['pnl'].values())
    db.close()


def test_pending_work_is_fair_after_many_missing_entries(tmp_path, source, code_root, monkeypatch):
    import trading_bot.research.breakout_recheck_shadow as worker
    monkeypatch.setattr(worker, 'MAX_NEW', 100)
    monkeypatch.setattr(worker, 'MAX_PENDING', 2)
    db, registration = start(tmp_path, source, code_root)
    add(source, pid=1, with_bar=False)
    add(source, pid=2, with_bar=False)
    add(source, pid=3)
    first = cycle(db, source, registration, NOW)
    assert first['counts']['closed_pairs'] == 0
    second = cycle(db, source, registration, NOW+1)
    assert second['counts']['closed_pairs'] == 1 and second['counts']['waiting_entry'] == 2
    db.close()


@pytest.mark.parametrize('name', ['scan', 'monitor'])
def test_stale_producer_degrades_without_advancing_and_recovers(tmp_path, source, code_root, name):
    db, registration = start(tmp_path, source, code_root)
    add(source)
    with sqlite3.connect(source) as src:
        saved = src.execute('SELECT value FROM meta WHERE key=?', (name,)).fetchone()[0]
        payload = json.loads(saved)
        payload['finished_ms' if name == 'scan' else 'at_ms'] = NOW-1200001
        src.execute('UPDATE meta SET value=? WHERE key=?', (canonical(payload), name))
    first = cycle(db, source, registration, NOW)
    assert first['health'] == 'DEGRADED' and first['errors']
    assert first['source_health'][name]['age_seconds'] > 1200
    assert first['counts']['sources'] == 0 and first['counts']['closed_pairs'] == 0
    assert db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0] == '0'
    with sqlite3.connect(source) as src:
        src.execute('UPDATE meta SET value=? WHERE key=?', (saved, name))
    assert cycle(db, source, registration, NOW+1)['health'] == 'OK'
    db.close()


@pytest.mark.parametrize('name', ['scan', 'monitor'])
def test_producer_error_degrades_even_when_fresh(tmp_path, source, code_root, name):
    db, registration = start(tmp_path, source, code_root)
    add(source)
    with sqlite3.connect(source) as src:
        payload = json.loads(src.execute('SELECT value FROM meta WHERE key=?', (name,)).fetchone()[0])
        payload['status'] = 'ERROR'
        src.execute('UPDATE meta SET value=? WHERE key=?', (canonical(payload), name))
    value = cycle(db, source, registration, NOW)
    assert value['health'] == 'DEGRADED' and value['counts']['sources'] == 0
    assert any(e['error'] == 'SOURCE_'+name.upper()+'_ERROR' for e in value['errors'])
    db.close()


@pytest.mark.parametrize('changed', [{'entry_price': 999.}, {'fees_R': 99.}])
def test_wrong_entry_or_cost_components_are_unscorable(tmp_path, source, code_root, changed):
    db, registration = start(tmp_path, source, code_root)
    add(source)
    with sqlite3.connect(source) as src:
        result = json.loads(src.execute('SELECT result FROM positions').fetchone()[0])
        result.update(changed)
        src.execute('UPDATE positions SET result=?', (canonical(result),))
    value = cycle(db, source, registration, NOW)
    assert value['counts']['closed_pairs'] == 0 and value['counts']['unscorable'] == 1
    assert all(x == 0 for x in value['pnl'].values())
    assert db.execute('SELECT control_result FROM sources').fetchone()[0] is None
    db.close()
