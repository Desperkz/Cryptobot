"""Owner-run, index-only migration for an existing stopped collector database.

No trade decisions, rules, rows or watermarks are changed. The caller must stop
the collector cleanly and pause its observer timer before running this helper.
"""
from __future__ import annotations
import argparse,hashlib,json,sqlite3,time
from contextlib import closing
from pathlib import Path

TABLES=('meta','segments','snapshots','liquidations','issues','connections')
INDEXES={
 'market_evidence_segments_symbol_minute':'CREATE INDEX market_evidence_segments_symbol_minute ON segments(symbol,minute_ms,session)',
 'market_evidence_issues_time_id':'CREATE INDEX market_evidence_issues_time_id ON issues(at_ms,id)',
}
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
def logical_fingerprint(c):
 out={}
 for table in TABLES:
  h=hashlib.sha256();n=0
  for row in c.execute('SELECT * FROM '+table+' ORDER BY rowid'):
   h.update(canonical(list(row)).encode());h.update(b'\n');n+=1
  out[table]={'rows':n,'sha256':h.hexdigest()}
 return out
def sample_contexts(c,cases):
 result={}
 for case in cases:
  at=case['opened_ms'];symbol=case['symbol'];target=(at//60000-1)*60000
  segments=c.execute('SELECT session,symbol,minute_ms,payload FROM segments WHERE symbol=? AND minute_ms BETWEEN ? AND ? ORDER BY minute_ms,session LIMIT 25',(symbol,target-60000,target+60000)).fetchall()
  issues=c.execute("SELECT at_ms,kind,symbol,detail FROM issues WHERE at_ms BETWEEN ? AND ? AND (symbol=? OR symbol='') ORDER BY at_ms LIMIT 513",(at-420000,at,symbol)).fetchall()
  result[str(case['id'])]=hashlib.sha256(canonical({'segments':segments,'issues':issues}).encode()).hexdigest()
 return result
def plans(c,case):
 at=case['opened_ms'];target=(at//60000-1)*60000;symbol=case['symbol']
 return {'segments':[list(x) for x in c.execute('EXPLAIN QUERY PLAN SELECT session,symbol,minute_ms,payload FROM segments WHERE symbol=? AND minute_ms BETWEEN ? AND ? ORDER BY minute_ms,session LIMIT 25',(symbol,target-60000,target+60000))],
 'issues':[list(x) for x in c.execute("EXPLAIN QUERY PLAN SELECT at_ms,kind,symbol,detail FROM issues WHERE at_ms BETWEEN ? AND ? AND (symbol=? OR symbol='') ORDER BY at_ms LIMIT 513",(at-420000,at,symbol))]}
def migrate(database,expected_manifest,cases):
 database=Path(database).resolve(strict=True)
 if not database.is_file() or len(expected_manifest)!=64:raise ValueError('INVALID_EXISTING_DATABASE_OR_MANIFEST_HASH')
 if not cases:raise ValueError('NEED_CONTEXT_CASES')
 started=time.monotonic()
 with closing(sqlite3.connect(database.as_uri()+'?mode=rw',uri=True,timeout=3)) as c:
  c.execute('PRAGMA cache_size=-2048');c.execute('PRAGMA temp_store=FILE')
  if c.execute('PRAGMA journal_mode').fetchone()[0]!='wal':raise ValueError('EXPECTED_EXISTING_WAL_DATABASE')
  raw=c.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
  if not raw or hashlib.sha256(raw[0].encode()).hexdigest()!=expected_manifest:raise ValueError('UNEXPECTED_COLLECTOR_MANIFEST')
  before=logical_fingerprint(c);contexts=sample_contexts(c,cases);old_plans=plans(c,cases[0])
  for name,sql in INDEXES.items():
   saved=c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(name,)).fetchone()
   if saved and ''.join(saved[0].lower().split())!=''.join(sql.lower().split()):raise ValueError('EXISTING_INDEX_DEFINITION_DIFFERS:'+name)
  c.execute('BEGIN IMMEDIATE')
  try:
   for name,sql in INDEXES.items():
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",(name,)).fetchone():c.execute(sql)
   after=logical_fingerprint(c);new_contexts=sample_contexts(c,cases)
   if after!=before or new_contexts!=contexts:raise ValueError('INDEX_MIGRATION_CHANGED_LOGICAL_INPUTS')
   c.commit()
  except Exception:c.rollback();raise
  if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('DATABASE_QUICK_CHECK_FAILED')
  new_plans=plans(c,cases[0])
  if not any('market_evidence_segments_symbol_minute' in str(x) for x in new_plans['segments']):raise ValueError('SEGMENT_INDEX_NOT_USED')
  if not any('market_evidence_issues_time_id' in str(x) for x in new_plans['issues']):raise ValueError('ISSUE_INDEX_NOT_USED')
 return {'passed':True,'database':str(database),'manifest_sha256':expected_manifest,
  'logical_tables_before':before,'logical_tables_after':after,'context_digests':contexts,
  'contexts_identical':True,'indexes':INDEXES,'query_plans_before':old_plans,'query_plans_after':new_plans,
  'elapsed_seconds':time.monotonic()-started,'data_or_protocol_rows_modified':False}
def main():
 p=argparse.ArgumentParser();p.add_argument('--database',type=Path,required=True);p.add_argument('--expected-manifest',required=True);p.add_argument('--cases',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 result=migrate(a.database,a.expected_manifest,json.loads(a.cases.read_text()));a.output.write_bytes((json.dumps(result,indent=2)+'\n').encode());print(json.dumps(result))
if __name__=='__main__':main()
