import hashlib,importlib.util,json,sqlite3
from pathlib import Path
import pytest

P=Path(__file__).resolve().parents[1]/'scripts/market_evidence_query_indexes.py'
spec=importlib.util.spec_from_file_location('market_query_indexes',P);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
AT=1800000020000
def fixture(tmp_path):
 p=tmp_path/'market.sqlite3';manifest=json.dumps({'fixed':'collector'});sha=hashlib.sha256(manifest.encode()).hexdigest()
 c=sqlite3.connect(p);c.execute('PRAGMA journal_mode=WAL')
 c.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE segments(session TEXT,symbol TEXT,minute_ms INTEGER,payload TEXT,PRIMARY KEY(session,symbol,minute_ms)); CREATE TABLE snapshots(id INTEGER PRIMARY KEY,payload TEXT); CREATE TABLE liquidations(digest TEXT PRIMARY KEY,payload TEXT); CREATE TABLE issues(id INTEGER PRIMARY KEY,at_ms INTEGER,kind TEXT,symbol TEXT,detail TEXT); CREATE TABLE connections(session TEXT PRIMARY KEY,payload TEXT);')
 c.execute('INSERT INTO meta VALUES(?,?)',('manifest',manifest));c.execute('INSERT INTO meta VALUES(?,?)',('state','{"watermark":123}'))
 minute=(AT//60000-1)*60000
 # Source primary key begins with session, not symbol/minute; include unrelated history.
 c.executemany('INSERT INTO segments VALUES(?,?,?,?)',[(f'session{i}','BTCUSDT',minute-i*60000,'x'*500) for i in range(4000)])
 c.executemany('INSERT INTO issues VALUES(?,?,?,?,?)',[(1,AT-1000,'GAP','BTCUSDT','one'),(2,AT-1000,'GAP','','two'),(3,AT-1000,'GAP','BTCUSDT','three')])
 c.commit();c.close();return p,sha,[{'id':1,'symbol':'BTCUSDT','opened_ms':AT}]
def test_index_migration_preserves_all_rows_meta_and_tied_issue_order(tmp_path):
 p,sha,cases=fixture(tmp_path);v=m.migrate(p,sha,cases)
 assert v['passed'] and v['logical_tables_before']==v['logical_tables_after']
 assert v['contexts_identical'] and v['data_or_protocol_rows_modified'] is False
 assert 'SCAN' in str(v['query_plans_before']['segments'])
 assert 'SEARCH' in str(v['query_plans_after']['segments'])
 with sqlite3.connect(p) as c:assert [x[0] for x in c.execute('SELECT detail FROM issues ORDER BY at_ms')]==['one','two','three']
def test_migration_is_idempotent(tmp_path):
 p,sha,cases=fixture(tmp_path);first=m.migrate(p,sha,cases);second=m.migrate(p,sha,cases)
 assert first['logical_tables_after']==second['logical_tables_after']
def test_wrong_manifest_cannot_modify_schema(tmp_path):
 p,sha,cases=fixture(tmp_path)
 with pytest.raises(ValueError,match='UNEXPECTED_COLLECTOR'):m.migrate(p,'0'*64,cases)
 with sqlite3.connect(p) as c:assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='market_evidence_segments_symbol_minute'").fetchone()
def test_conflicting_named_index_is_not_replaced(tmp_path):
 p,sha,cases=fixture(tmp_path)
 with sqlite3.connect(p) as c:c.execute('CREATE INDEX market_evidence_segments_symbol_minute ON segments(session)')
 with pytest.raises(ValueError,match='DEFINITION_DIFFERS'):m.migrate(p,sha,cases)
def test_identical_query_needs_fewer_vm_steps_after_indexes(tmp_path):
 p,sha,cases=fixture(tmp_path)
 def work():
  c=sqlite3.connect(p);ticks=[0]
  def progress():ticks[0]+=1;return 0
  c.set_progress_handler(progress,100)
  m.sample_contexts(c,cases);c.close();return ticks[0]
 before=work();m.migrate(p,sha,cases);after=work()
 assert before>100 and after<before//10
