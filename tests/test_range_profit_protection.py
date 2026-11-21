import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from trading_bot.research import range_profit_protection as w
from trading_bot.research.range_profit_execution import settle

AT = 1_800_000_000_000


@pytest.fixture
def lab(tmp_path, monkeypatch):
    source = tmp_path/'source'/'shadow.sqlite3'
    source.parent.mkdir()
    meta = {'cohort': w.source_contract.SOURCE_COHORT, 'venue': 'https://fapi.binance.com',
            'mode': 'SHADOW_ONLY', 'order_capability': False,
            'settings': {'risk_usdt': 2, 'symbols': list(w.source_contract.SYMBOLS)}}
    with sqlite3.connect(source) as db:
        db.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE snapshots(id INTEGER PRIMARY KEY,symbol TEXT,observed_ms INTEGER,bar_ms INTEGER);
            CREATE TABLE positions(id INTEGER PRIMARY KEY,strategy TEXT,symbol TEXT,direction TEXT,
            opened_ms INTEGER,entry_mid REAL,entry REAL,stop REAL,target REAL,quantity REAL,risk_usdt REAL,
            expires_ms INTEGER,snapshot_id INTEGER,status TEXT,closed_ms INTEGER,exit_price REAL,
            net_usdt REAL,reason TEXT,costs TEXT);''')
        db.execute('INSERT INTO meta VALUES(?,?)', ('manifest', w.canonical(meta)))
    monkeypatch.setattr(w.source_contract, 'SOURCE_HASH', w.digest(meta))
    source.with_name('status.json').write_text(w.canonical({'health': 'OK', 'generated_at_ms': AT}))
    db, reg = w.open_store(tmp_path/'own', source, AT)
    yield source, db, reg, tmp_path/'own'
    db.close()


def add(source, sid=1, opened=AT+60_000, strategy=w.STRATEGY, bar_ms=None):
    entry = 100*1.0005
    stop = 99.
    effective = stop*.9995
    quantity = 2/(entry-effective+(entry+effective)*.0004+entry*.0001)
    plan = {'id': sid, 'strategy': strategy, 'symbol': 'BTCUSDT', 'direction': 'LONG',
            'opened_ms': opened, 'entry_mid': 100., 'entry': entry, 'stop': stop, 'target': 103.,
            'quantity': quantity, 'risk_usdt': 2., 'expires_ms': opened+8*3600_000, 'snapshot_id': sid}
    with sqlite3.connect(source) as db:
        db.execute('INSERT INTO snapshots VALUES(?,?,?,?)', (sid, 'BTCUSDT', opened, opened-1 if bar_ms is None else bar_ms))
        db.execute('INSERT INTO positions('+','.join(plan)+',status) VALUES('+','.join('?' for _ in plan)+',?)', (*plan.values(), 'OPEN'))
    return plan


def bar(start, o=100.2, h=100.8, l=99.5, c=100.3):
    return [start, str(o), str(h), str(l), str(c), '1', start+59999]


def fetch(bars):
    def get(symbol, start, end):
        selected = [b for b in bars if start <= b[0] and b[6] <= end]
        return selected, hashlib.sha256(w.canonical(selected).encode()).hexdigest(), end+1
    return get


def close_source(source, plan, price, when, reason):
    result = settle(plan, price, when, reason)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE positions SET status='CLOSED',closed_ms=?,exit_price=?,net_usdt=?,reason=?,costs=? WHERE id=?",
                   (*result.values(), plan['id']))
    return result


def ingest(lab, now):
    source, db, reg, _ = lab
    with closing(w.readonly(source)) as src:
        w.ingest(db, src, reg, now)


def test_watermark_time_bar_and_other_strategy_never_backfill(lab):
    source, db, reg, own = lab
    add(source, 1, AT-1)
    add(source, 2, AT+10_000, bar_ms=AT-1)
    add(source, 3, AT+20_000, strategy='OTHER')
    ingest(lab, AT+30_000)
    assert db.execute('SELECT count(*) FROM pairs').fetchone()[0] == 0
    assert {r[0] for r in db.execute('SELECT reason FROM exclusions')} == {
        'PRE_REGISTRATION', 'PRE_REGISTRATION_SIGNAL_BAR', 'OTHER_STRATEGY'}
    status = w.status(db, reg, AT+30_000, {'health': 'OK', 'age_seconds': 0})
    assert status['counts']['observed'] == status['counts']['skipped'] == 1
    new, new_reg = w.open_store(own.parent/'new-study', source, AT+30_000)
    assert new_reg['source_watermark'] == 3
    assert new.execute('SELECT count(*) FROM pairs').fetchone()[0] == 0
    new.close()


def test_admission_committed_before_outcomes_even_if_source_already_closed(lab):
    source, db, reg, own = lab
    plan = add(source)
    close_source(source, plan, plan['target'], plan['opened_ms']+179999, 'TARGET')
    with closing(w.readonly(source)) as src:
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == 'positions' and column in {
                    'status', 'net_usdt', 'closed_ms', 'exit_price', 'reason', 'costs'}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        src.set_authorizer(authorize)
        w.ingest(db, src, reg, plan['opened_ms']+1000)
    with sqlite3.connect(own/'range_profit_protection.sqlite3') as other:
        assert other.execute('SELECT admission FROM pairs').fetchone()[0] == 'ELIGIBLE'


def test_late_unknown_controls_separate_and_never_requests_minutes(lab):
    source, db, reg, _ = lab
    plan = add(source)
    result = close_source(source, plan, plan['target'], plan['opened_ms']+179999, 'TARGET')
    now = plan['opened_ms']+180001
    value = w.cycle(db, reg, source, now, lambda *args: pytest.fail('UNKNOWN may not be retrospectively protected'))
    assert value['counts']['unscorable'] == value['results']['unscorable_controls_closed'] == 1
    assert value['results']['unscorable_control_net_usdt'] == result['net_usdt']
    assert value['counts']['closed_pairs'] == value['counts']['eligible'] == 0
    assert value['results']['paired_delta_usdt'] == 0
    assert value['recent_pairs'][0]['protected_net_usdt'] is None


def test_no_protection_change_reproduces_source_with_identical_costs(lab):
    source, db, reg, own = lab
    plan = add(source)
    ingest(lab, plan['opened_ms']+1000)
    start = plan['opened_ms']+60000
    bars = [bar(start), bar(start+60000, 101, 104, 100, 103)]
    result = close_source(source, plan, plan['target'], bars[-1][6], 'TARGET')
    value = w.cycle(db, reg, source, bars[-1][6]+1, fetch(bars))
    assert value['health'] == 'OK'
    assert value['counts']['closed_pairs'] == value['results']['unchanged_pairs'] == 1
    assert value['results']['paired_control_net_usdt'] == result['net_usdt']
    assert value['results']['paired_delta_usdt'] == 0
    row = db.execute('SELECT * FROM pairs').fetchone()
    assert json.loads(row['protected']) == json.loads(row['control_replay'])
    assert row['parity_valid'] == 1
    assert db.execute('SELECT count(*) FROM minutes').fetchone()[0] == 2
    # The public saved status is accepted by the presentation contract.
    from range_profit_protection_api import api_range_profit_protection_shadow
    w.write_status(own, value)
    assert api_range_profit_protection_shadow(own/'status.json', now_ms=value['generated_at_ms'])['health'] == 'OK'


def test_early_protected_exit_not_scored_until_original_control_closes(lab):
    source, db, reg, _ = lab
    plan = add(source)
    ingest(lab, plan['opened_ms']+1000)
    start = plan['opened_ms']+60000
    bars = [bar(start, 100.5, 102, 99.5, 101.5), bar(start+60000, 101.5, 102, 100, 100.2)]
    first = w.cycle(db, reg, source, bars[-1][6]+1, fetch(bars))
    assert first['counts']['open_pairs'] == 1 and first['counts']['closed_pairs'] == 0
    assert first['results']['protected_net_usdt'] == first['results']['paired_delta_usdt'] == 0
    assert first['recent_pairs'][0]['protected_net_usdt'] < 0
    assert first['recent_pairs'][0]['delta_usdt'] is None
    bars.append(bar(start+120000, 100.2, 100.4, 98, 98.5))
    close_source(source, plan, plan['stop'], bars[-1][6], 'STOP')
    second = w.cycle(db, reg, source, bars[-1][6]+1, fetch(bars))
    assert second['counts']['closed_pairs'] == second['results']['improved_pairs'] == 1
    assert second['results']['paired_delta_usdt'] > 0
    assert second['results']['protected_net_usdt'] < 0


def test_missing_minutes_pending_recoverable_without_losing_admission(lab):
    source, db, reg, _ = lab
    plan = add(source)
    ingest(lab, plan['opened_ms']+1000)
    start = plan['opened_ms']+60000
    original_state = db.execute('SELECT execution_state FROM pairs').fetchone()[0]
    bad = [bar(start+60000, 100, 104, 99.5, 103)]
    close_source(source, plan, plan['target'], bad[-1][6], 'TARGET')
    value = w.cycle(db, reg, source, bad[-1][6]+1, fetch(bad))
    assert value['health'] == 'DEGRADED' and value['counts']['open_pairs'] == 1
    assert db.execute('SELECT execution_state FROM pairs').fetchone()[0] == original_state
    good = [bar(start), *bad]
    recovered = w.cycle(db, reg, source, bad[-1][6]+2, fetch(good))
    assert recovered['health'] == 'OK' and recovered['counts']['closed_pairs'] == 1
    assert db.execute('SELECT admission FROM pairs').fetchone()[0] == 'ELIGIBLE'


def test_source_replay_mismatch_never_enters_totals(lab):
    source, db, reg, _ = lab
    plan = add(source)
    ingest(lab, plan['opened_ms']+1000)
    start = plan['opened_ms']+60000
    bars = [bar(start, 100, 104, 99.5, 103)]
    close_source(source, plan, plan['stop'], bars[-1][6], 'STOP')
    value = w.cycle(db, reg, source, bars[-1][6]+1, fetch(bars))
    assert value['health'] == 'DEGRADED' and value['counts']['closed_pairs'] == 0
    assert value['counts']['open_pairs'] == 1
    assert 'REPLAY_MISMATCH' in value['recent_pairs'][0]['error']


def test_restart_source_rewind_and_frozen_rule_never_reset(lab, monkeypatch):
    source, db, reg, own = lab
    plan = add(source)
    ingest(lab, plan['opened_ms']+1000)
    reopened, again = w.open_store(own, source, AT+1)
    assert again == reg
    assert reopened.execute('SELECT count(*) FROM pairs').fetchone()[0] == 1
    reopened.close()
    original_rule = w.RULE
    monkeypatch.setattr(w, 'RULE', {**original_rule, 'modified': True})
    with pytest.raises(ValueError, match='FROZEN_STUDY_CHANGED'):
        w.open_store(own, source, AT+2)
    monkeypatch.setattr(w, 'RULE', original_rule)
    with sqlite3.connect(source) as src:
        src.execute('DELETE FROM positions')
    with pytest.raises(ValueError, match='WATERMARK_REWOUND'):
        w.open_store(own, source, AT+3)


def test_source_outage_pauses_new_admissions_but_settles_existing_pairs(lab):
    source, db, reg, _ = lab
    plan = add(source)
    ingest(lab, plan['opened_ms']+1000)
    start = plan['opened_ms']+60000
    bars = [bar(start, 100, 104, 99.5, 103)]
    close_source(source, plan, plan['target'], bars[-1][6], 'TARGET')
    add(source, 2, opened=bars[-1][6]-1000)
    source.with_name('status.json').write_text(w.canonical({'health': 'DEGRADED', 'generated_at_ms': bars[-1][6]}))
    value = w.cycle(db, reg, source, bars[-1][6]+1, fetch(bars))
    assert value['health'] == 'DEGRADED' and value['counts']['observed'] == value['counts']['closed_pairs'] == 1


def test_closed_minute_revision_not_overwritten(lab):
    _, db, _, _ = lab
    a = [bar(AT)]
    w.store_minutes(db, 'BTCUSDT', AT, AT+59999, a, 'a'*64, AT+60000)
    with pytest.raises(ValueError, match='CLOSED_MINUTE_CHANGED'):
        w.store_minutes(db, 'BTCUSDT', AT, AT+59999, [bar(AT, c=100.4)], 'b'*64, AT+60000)
    assert db.execute('SELECT count(*) FROM minute_receipts').fetchone()[0] == 1
