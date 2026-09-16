import hashlib
import importlib.util
import json
from pathlib import Path
import zlib

from trading_bot.research.mainnet_paper import Store

spec = importlib.util.spec_from_file_location('mainnet_paper_report', Path(__file__).parents[1] / 'scripts/mainnet_paper_report.py')
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def test_empty_report_has_no_claimed_returns_and_is_readonly(tmp_path):
    store = Store(tmp_path, {'cohort': 'test'})
    store.db.close()
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    report = reporter.build_report(store.path, now_ms=1_000_000)
    assert len(report['arms']) == 8
    assert all(a['mean_R'] is None and a['realized_model_pnl_usdt'] is None for a in report['arms'])
    assert report['freshness']['scan']['state'] == 'UNKNOWN'
    assert report['sources'] == 0
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
    assert 'NO_DATA' in reporter.markdown(report)


def test_correlated_copies_open_marks_and_missing_results(tmp_path):
    store = Store(tmp_path, {'cohort': 'test'})
    rows = [('s1','BASELINE_2R',0,'CLOSED', {'status':'CLOSED','R':1,'model_pnl_usdt':2}),
            ('s1','CURRENT_GATE_2R',0,'CLOSED', {'status':'CLOSED','R':-1,'model_pnl_usdt':-2}),
            ('s2','BASELINE_2R',60_000,'OPEN', {'status':'OPEN','R':100,'model_pnl_usdt':200}),
            ('s3','BASELINE_2R',3_600_000,'CLOSED', None)]
    for source, arm, stamp, state, result in rows:
        store.db.execute('INSERT INTO positions(source_id,policy,arm,symbol,direction,entry_ms,status,result) VALUES(?,?,?,?,?,?,?,?)',
                         (source,'FIRST_OBSERVATION',arm,'BTCUSDT','LONG',stamp,state,json.dumps(result)))
    store.db.commit()
    # Visible committed WAL data must be included without closing the writer.
    report = reporter.build_report(store.path)
    baseline, current = report['arms'][:2]
    assert baseline['mean_R'] == 1 and baseline['realized_model_pnl_usdt'] == 2
    assert baseline['closed_missing_result'] == 1
    assert current['mean_R'] == -1
    assert report['filled_unique_sources'] == 3
    assert report['first_per_60m_source_proxy'] == 2
    store.db.close()


def test_oi_zero_freshness_and_first_observation_reasons(tmp_path):
    store = Store(tmp_path, {'cohort': 'test'})
    store.coverage('BTCUSDT', {'metrics': {'open_interest_change_pct':'0'}, 'status':'ELIGIBLE'})
    store.coverage('ETHUSDT', {'metrics': {}, 'status':'ERROR'})
    store.set('scan', {'finished_ms': 1000, 'status':'OK'})
    store.set('monitor', {'at_ms': 999_000, 'status':'DEGRADED'})
    for reason in ('first', 'later'):
        payload = {'decisions': {'current_all_gate_failures':[reason,reason],
                                'p8_all_gates': {'relative_strength':False}}}
        store.db.execute('INSERT INTO observations(source_id,observed_ms,payload_zlib) VALUES(?,?,?)',
                         ('s1',0,zlib.compress(json.dumps(payload).encode())))
    store.db.commit()
    report = reporter.build_report(store.path, now_ms=1_000_000)
    assert sum(c['oi_present'] for c in report['coverage']) == 1
    assert report['freshness']['scan']['state'] == 'STALE'
    assert report['freshness']['monitor']['last_status'] == 'DEGRADED'
    assert report['first_observation_gate_failures'] == {'current:first':1,'p8:relative_strength':1}
    store.db.close()
