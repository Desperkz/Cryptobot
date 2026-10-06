import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import paper_pilots_api as api

NOW = 1791262200000


def make_db(directory, rows=(), *, age=10, mode='LOCAL_PAPER_ONLY'):
    directory.mkdir(parents=True)
    path = directory / 'mainnet_paper_lab.sqlite3'
    conn = sqlite3.connect(path)
    conn.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE positions(id INTEGER PRIMARY KEY,source_id TEXT,policy TEXT,arm TEXT,
            symbol TEXT,direction TEXT,entry_ms INTEGER,status TEXT,plan TEXT,result TEXT);
        CREATE TABLE sources(id TEXT); CREATE TABLE observations(id INTEGER);''')
    manifest = {'mode': mode, 'data_venue': 'https://fapi.binance.com', 'cohort': 'fixed-cohort',
        'arms': ['CONTROL', 'TREATMENT', 'NO_ENTRIES'], 'policies': ['FIRST_OBSERVATION', 'FIRST_ADMISSIBLE'],
        'scan_interval_sec': 300, 'monitor_interval_sec': 30}
    conn.executemany('INSERT INTO meta VALUES(?,?)', [(key, json.dumps(value)) for key,value in {
        'manifest': manifest, 'scan': {'status': 'OK', 'finished_ms': NOW-age*1000},
        'monitor': {'status': 'OK', 'at_ms': NOW-age*1000}}.items()])
    for index,(policy,arm,status,pnl) in enumerate(rows,1):
        result = {'status': status, 'model_pnl_usdt': pnl, 'last_ms': NOW-1000} if pnl is not None else None
        conn.execute('INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)', (index, 'same-source',
            policy, arm, 'BTCUSDT', 'LONG', NOW-60000, status, json.dumps({'risk_usdt': 2}), json.dumps(result) if result else None))
    conn.commit();conn.close()
    return path


def test_policies_and_correlated_arms_are_separate_and_zero_entry_arms_present(tmp_path):
    make_db(tmp_path/'data', [('FIRST_OBSERVATION','CONTROL','CLOSED',4),
        ('FIRST_OBSERVATION','CONTROL','CLOSED',-2), ('FIRST_OBSERVATION','TREATMENT','CLOSED',4),
        ('FIRST_ADMISSIBLE','CONTROL','CLOSED',9), ('FIRST_OBSERVATION','CONTROL','OPEN',-1)])
    result=api.read_pilot('pilot','Label',tmp_path/'data',now_ms=NOW)
    groups={(g['policy'],g['arm']):g for g in result['groups']}
    control=groups['FIRST_OBSERVATION','CONTROL']
    assert control['closed']==2 and control['open']==1 and control['net_usdt']==2
    assert control['profit_factor']==2 and control['winrate']==50
    assert groups['FIRST_OBSERVATION','TREATMENT']['net_usdt']==4
    assert groups['FIRST_ADMISSIBLE','CONTROL']['net_usdt']==9
    assert groups['FIRST_OBSERVATION','NO_ENTRIES']['closed']==0
    assert groups['FIRST_OBSERVATION','NO_ENTRIES']['profit_factor'] is None
    assert 'net_usdt' not in result and result['health']=='OK'


def test_read_only_missing_does_not_create_files_and_failure_is_local(tmp_path):
    result=api.build_paper_pilots(tmp_path,now_ms=NOW)
    assert result['combined_pnl'] is None and len(result['pilots'])==6
    assert all(p['error']=='DATABASE_MISSING' for p in result['pilots'])
    assert not list(tmp_path.iterdir())


def test_reader_does_not_modify_database_or_manifest(tmp_path):
    path=make_db(tmp_path/'data',[('FIRST_OBSERVATION','CONTROL','CLOSED',1)])
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    result=api.read_pilot('pilot','Label',path.parent,now_ms=NOW)
    assert result['manifest']['cohort']=='fixed-cohort'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_live_wal_is_visible_and_open_entries_survive_recent_page_limit(tmp_path):
    path=make_db(tmp_path/'data')
    writer=sqlite3.connect(path);writer.execute('PRAGMA journal_mode=WAL')
    for i in range(1,81):
        status='OPEN' if i==1 else 'CLOSED'
        writer.execute('INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)',(i,'source-'+str(i),
            'FIRST_OBSERVATION','CONTROL','BTCUSDT','LONG',NOW-60000,status,'{"risk_usdt":2}',json.dumps({'status':status,'model_pnl_usdt':1})))
    writer.commit()
    result=api.read_pilot('pilot','Label',path.parent,now_ms=NOW)
    assert len(result['recent_positions'])==50 and result['counts']['positions']==80
    assert result['open_positions'][0]['id']==1 and result['groups'][0]['closed']==79
    writer.close()


@pytest.mark.parametrize('age,mode,expected',[(600,'LOCAL_PAPER_ONLY','STALE'),(1,'LIVE','UNAVAILABLE')])
def test_stale_and_nonpaper_are_not_claimed_healthy(tmp_path,age,mode,expected):
    make_db(tmp_path/'data',age=age,mode=mode)
    assert api.read_pilot('pilot','Label',tmp_path/'data',now_ms=NOW)['health']==expected


def test_corrupt_and_nan_closed_results_are_not_silently_zero(tmp_path):
    path=make_db(tmp_path/'data',[('FIRST_OBSERVATION','CONTROL','CLOSED',float('nan'))])
    conn=sqlite3.connect(path);conn.execute("UPDATE positions SET result='broken-json'");conn.commit();conn.close()
    result=api.read_pilot('pilot','Label',path.parent,now_ms=NOW)
    assert result['health']=='DEGRADED'
    assert result['groups'][0]['net_usdt'] is None and result['groups'][0]['invalid_results']==1
    assert result['recent_positions'][0]['data_error']=='INVALID_POSITION_JSON'


def test_no_loss_pf_not_fake_infinity_and_result_r_fallback(tmp_path):
    path=make_db(tmp_path/'data',[('FIRST_OBSERVATION','CONTROL','CLOSED',4)])
    conn=sqlite3.connect(path);conn.execute('UPDATE positions SET result=?', (json.dumps({'status':'CLOSED','R':2}),));conn.commit();conn.close()
    result=api.read_pilot('pilot','Label',path.parent,now_ms=NOW)
    assert result['groups'][0]['net_usdt']==4 and result['groups'][0]['profit_factor'] is None


def test_cache_coalesces_dashboard_reads(monkeypatch):
    calls=[]
    monkeypatch.setattr(api,'_CACHE',{'data':None,'expires':0})
    monkeypatch.setattr(api,'build_paper_pilots',lambda: calls.append(1) or {'pilots':[]})
    assert api.api_paper_pilots()==api.api_paper_pilots()
    assert len(calls)==1


def test_control_route_has_no_pilot_mutation_endpoint():
    import bot_control_v2
    assert bot_control_v2.ROUTES['/paper-pilots'] is api.api_paper_pilots
    assert not any('pilot' in path for path in bot_control_v2.POST_ROUTES)


@pytest.mark.parametrize('raw', ['{"status":"CLOSED","model_pnl_usdt":NaN}',
    '{"status":"CLOSED","model_pnl_usdt":1e999}',
    '{"status":"CLOSED","R":1e308}'])
def test_nonfinite_json_and_overflow_fail_closed(tmp_path, raw):
    path = make_db(tmp_path/'data', [('FIRST_OBSERVATION','CONTROL','CLOSED',1)])
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE positions SET result=?', (raw,))
    result = api.read_pilot('pilot','Label',path.parent,now_ms=NOW)
    assert result['health'] == 'DEGRADED'
    assert result['groups'][0]['net_usdt'] is None
    json.dumps(result, allow_nan=False)


def test_one_corrupt_pilot_does_not_hide_the_other_five(tmp_path):
    for _, _, directory in api.PILOTS:
        make_db(tmp_path/directory/'data')
    with sqlite3.connect(tmp_path/api.PILOTS[0][2]/'data/mainnet_paper_lab.sqlite3') as conn:
        conn.execute("UPDATE meta SET value='invalid' WHERE key='manifest'")
    result = api.build_paper_pilots(tmp_path, now_ms=NOW)
    assert result['pilots'][0]['health'] == 'UNAVAILABLE'
    assert all(p['health'] == 'OK' for p in result['pilots'][1:])
