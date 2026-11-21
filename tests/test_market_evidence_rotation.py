from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

import market_evidence_rotation as r
from trading_bot.research.market_evidence_collector import Store

AT=1800000000000


@pytest.fixture
def source(tmp_path):
    data=tmp_path/'active';data.mkdir()
    store=Store(data,AT-7200000)
    store.begin(AT-7200000)
    for sid,at in ((100,AT-7200000),(101,AT-100000)):
        store.event({'e':'aggTrade','E':at,'T':at,'s':'BTCUSDT','a':sid,'p':'100','q':'2','m':False},at)
        store.snapshot('BTCUSDT','open_interest',{'symbol':'BTCUSDT','openInterest':'50','time':at},at,at)
        store.issue('FIXTURE','BTCUSDT',{'id':sid},at)
        store.flush(at)
    # Equal-timestamp ordering must survive rebuilding the active cache.
    store.issue('TIE','BTCUSDT',{'order':1},AT-1)
    store.issue('TIE','BTCUSDT',{'order':2},AT-1)
    store.close(AT)
    path=data/'market_evidence.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('CREATE INDEX market_evidence_segments_symbol_minute ON segments(symbol,minute_ms,session)')
        db.execute('CREATE INDEX market_evidence_issues_time_id ON issues(at_ms,id)')
        value=db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()[0]
    return path,hashlib.sha256(value.encode()).hexdigest()


def test_full_history_archived_tail_contexts_and_metadata_preserved(source,tmp_path):
    path,manifest=source
    with closing(r.ro(path)) as db:
        original=r.rows_fingerprint(db);meta=dict(db.execute('SELECT * FROM meta'))
    record=r.prepare(path,tmp_path/'archives',AT,expected_manifest=manifest)
    assert record['full_tables']==original
    assert record['active_tables']['segments']['rows']<original['segments']['rows']
    assert record['archive_verified'] and len(record['recent_context_samples'])==24
    full=tmp_path/'offline.sqlite3'
    with gzip.open(record['archive'],'rb') as src,full.open('wb') as dst:
        import shutil
        shutil.copyfileobj(src,dst)
    with closing(r.ro(full)) as db:assert r.rows_fingerprint(db)==original
    with closing(r.ro(record['candidate'])) as db:
        assert dict(db.execute('SELECT * FROM meta'))==meta
        assert [x[0] for x in db.execute("SELECT detail FROM issues WHERE kind='TIE' ORDER BY at_ms")]==['{"order":1}','{"order":2}']
    r.activate(record)
    with closing(r.ro(path)) as db:assert r.rows_fingerprint(db)==record['active_tables']
    assert Path(record['previous']).is_file()


def test_restarted_frozen_collector_keeps_counters_and_trade_id_watermarks(source,tmp_path):
    path,manifest=source
    with closing(r.ro(path)) as db:state=json.loads(dict(db.execute('SELECT * FROM meta'))['state'])
    record=r.prepare(path,tmp_path/'archives',AT,expected_manifest=manifest);r.activate(record)
    store=Store(path.parent,AT+1000);store.begin(AT+1000)
    assert store.manifest['registered_ms']==AT-7200000
    assert store.state['counts']==state['counts']
    event={'e':'aggTrade','E':AT+1000,'T':AT+1000,'s':'BTCUSDT','a':101,'p':'100','q':'2','m':False}
    assert not store.event(event,AT+1000)
    assert store.event({**event,'a':102},AT+1000)
    assert store.state['counts']['agg_trades']==state['counts']['agg_trades']+1
    store.close(AT+2000)


def test_empty_recent_window_keeps_integer_id_anchors(source,tmp_path):
    path,manifest=source
    record=r.prepare(path,tmp_path/'archives',AT+7200000,expected_manifest=manifest)
    with closing(r.ro(path)) as old,closing(r.ro(record['candidate'])) as new:
        for table in ('issues','snapshots'):
            assert new.execute('SELECT count(*) FROM '+table).fetchone()[0]==1
            assert new.execute('SELECT max(id) FROM '+table).fetchone()[0]==old.execute('SELECT max(id) FROM '+table).fetchone()[0]


def test_tampered_archive_cannot_replace_source(source,tmp_path):
    path,manifest=source
    record=r.prepare(path,tmp_path/'archives',AT,expected_manifest=manifest)
    archive=Path(record['archive']);archive.chmod(0o600)
    archive.write_bytes(archive.read_bytes()+b'tamper')
    before=r.file_sha(path)
    with pytest.raises(AssertionError):r.activate(record)
    assert r.file_sha(path)==before


def test_wrong_protocol_refuses_rotation(source,tmp_path):
    path,_=source;before=r.file_sha(path)
    with pytest.raises(AssertionError,match='UNEXPECTED_COLLECTOR'):
        r.prepare(path,tmp_path/'archives',AT,expected_manifest='0'*64)
    assert r.file_sha(path)==before


def test_live_wal_must_be_quiesced(source,tmp_path):
    path,manifest=source
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA journal_mode=WAL');db.execute('BEGIN IMMEDIATE')
        with pytest.raises(AssertionError,match='Close every'):
            r.prepare(path,tmp_path/'archives',AT,expected_manifest=manifest)


def test_activation_failure_restores_original_file(source,tmp_path,monkeypatch):
    path,manifest=source
    record=r.prepare(path,tmp_path/'archives',AT,expected_manifest=manifest)
    original=Path.rename
    def fail_candidate(self,target):
        if str(self)==record['candidate']:raise OSError('fixture rename failure')
        return original(self,target)
    monkeypatch.setattr(Path,'rename',fail_candidate)
    with pytest.raises(OSError):r.activate(record)
    assert path.exists()
    with closing(r.ro(path)) as db:assert r.rows_fingerprint(db)==record['full_tables']


def test_capacity_check_below_threshold_does_not_touch_services(source,tmp_path,monkeypatch):
    import market_evidence_rotation_maintenance as m
    path,_=source
    monkeypatch.setattr(m,'SOURCE',path);monkeypatch.setattr(m,'OPS',tmp_path/'ops')
    monkeypatch.setattr(m,'ARCHIVES',tmp_path/'archives');monkeypatch.setattr(m,'MARKER',tmp_path/'ops/data/recovery.json')
    monkeypatch.setattr(m,'command',lambda *args:pytest.fail('No service mutations below trigger'))
    m.main()
    value=json.loads((tmp_path/'ops/data/status.json').read_text())
    assert value['health']=='OK' and value['action']=='CHECK_ONLY'


def test_crash_between_file_swaps_recovers_previous_before_resuming(source,tmp_path,monkeypatch):
    import market_evidence_rotation_maintenance as m
    path,manifest=source
    record=r.prepare(path,tmp_path/'archives',AT,expected_manifest=manifest);r.activate(record)
    path.unlink()  # Emulate a missing active file; the verified old file remains.
    marker=tmp_path/'ops/data/recovery.json';marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({'source':str(path),'previous':record['previous'],
                                 'resume_collector':False,'resume_observer':False}))
    monkeypatch.setattr(m,'SOURCE',path);monkeypatch.setattr(m,'MARKER',marker)
    monkeypatch.setattr(r,'MANIFEST_SHA',manifest)
    monkeypatch.setattr(m,'command',lambda *args:pytest.fail('No unrequested restart'))
    m.recover()
    assert path.exists() and not marker.exists()
    with closing(r.ro(path)) as db:assert r.rows_fingerprint(db)==record['full_tables']
