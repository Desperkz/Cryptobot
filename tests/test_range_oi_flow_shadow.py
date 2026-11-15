import json,hashlib,sqlite3
from pathlib import Path
import pytest
from trading_bot.research import range_oi_flow_shadow as w

AT=1800000020000
SOURCE_META={'cohort':w.SOURCE_COHORT,'venue':'https://fapi.binance.com','mode':'SHADOW_ONLY','order_capability':False,
 'settings':{'risk_usdt':2,'symbols':list(w.SYMBOLS)}}
MARKET_META={'registered_ms':AT-1000000,'identity':{'rule':{'mode':'OBSERVATION_ONLY','order_capability':False}}}
def code_hash(v):return hashlib.sha256(w.canonical(v).encode()).hexdigest()
def fixture(tmp_path,monkeypatch):
 src=tmp_path/'source'/'shadow.sqlite3';mkt=tmp_path/'market'/'market_evidence.sqlite3'
 src.parent.mkdir();mkt.parent.mkdir()
 with sqlite3.connect(src) as c:
  c.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE snapshots(id INTEGER PRIMARY KEY,symbol TEXT,observed_ms INTEGER,bar_ms INTEGER); CREATE TABLE positions(id INTEGER PRIMARY KEY,strategy TEXT,symbol TEXT,direction TEXT,opened_ms INTEGER,entry_mid REAL,entry REAL,stop REAL,target REAL,quantity REAL,risk_usdt REAL,expires_ms INTEGER,snapshot_id INTEGER,status TEXT,closed_ms INTEGER,exit_price REAL,net_usdt REAL,reason TEXT,costs TEXT);')
  c.execute('INSERT INTO meta VALUES(?,?)',('manifest',w.canonical(SOURCE_META)))
 with sqlite3.connect(mkt) as c:
  c.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE snapshots(id INTEGER PRIMARY KEY,symbol TEXT,kind TEXT,received_ms INTEGER,payload TEXT); CREATE TABLE segments(session TEXT,symbol TEXT,minute_ms INTEGER,payload TEXT); CREATE TABLE connections(session TEXT,started_ms INTEGER,last_received_ms INTEGER,ended_ms INTEGER,reason TEXT); CREATE TABLE issues(at_ms INTEGER,kind TEXT,symbol TEXT,detail TEXT);')
  c.execute('INSERT INTO meta VALUES(?,?)',('manifest',w.canonical(MARKET_META)))
 monkeypatch.setattr(w,'SOURCE_HASH',code_hash(SOURCE_META));monkeypatch.setattr(w,'MARKET_HASH',code_hash(MARKET_META))
 for p in (src,mkt):p.with_name('status.json').write_text(w.canonical({'health':'OK','generated_at_ms':AT}))
 c,reg=w.open_store(tmp_path/'own',src,mkt,AT)
 return src,mkt,c,reg
def add_source(src,sid=1,opened=AT+60000):
 sign=1;mid=100.;entry=mid*1.0005;stop=99.;target=103.;effective_stop=stop*.9995
 qty=2/(entry-effective_stop+(entry+effective_stop)*.0004+entry*.0001)
 plan={'id':sid,'strategy':w.STRATEGY,'symbol':'BTCUSDT','direction':'LONG','opened_ms':opened,
 'entry_mid':mid,'entry':entry,'stop':stop,'target':target,'quantity':qty,'risk_usdt':2.,'expires_ms':opened+8*3600000,'snapshot_id':sid}
 with sqlite3.connect(src) as c:
  c.execute('INSERT INTO snapshots VALUES(?,?,?,?)',(sid,'BTCUSDT',opened,opened-1000))
  c.execute('INSERT INTO positions('+','.join(plan)+',status) VALUES('+','.join('?' for _ in plan)+',?)',(*plan.values(),'OPEN'))
 return plan
def close_source(src,plan,net=None):
 when=(plan['opened_ms']//60000+3)*60000-1;fill=plan['target']*.9995;q=plan['quantity'];entry=plan['entry']
 fees=(entry+fill)*q*.0004;funding=entry*q*.0001*(when-plan['opened_ms'])/28800000
 result=(fill-entry)*q-fees-funding
 costs={'fees':fees,'funding':funding,'funding_source':'adverse_1bp_per8h_buffer',
  'entry_slippage':abs(entry-plan['entry_mid'])*q,'exit_slippage':(plan['target']-fill)*q,'gross_before_exit_slippage':(plan['target']-entry)*q}
 with sqlite3.connect(src) as c:c.execute("UPDATE positions SET status='CLOSED',closed_ms=?,exit_price=?,net_usdt=?,reason='TARGET',costs=? WHERE id=?",(when,fill,result if net is None else net,w.canonical(costs),plan['id']))
 return result,when
def gate(decision):return {'decision':decision,'reason':'FIXTURE','accepted':{'ACCEPT':True,'REJECT':False,'UNKNOWN':None}[decision],'data_quality':'UNKNOWN' if decision=='UNKNOWN' else 'PASS','evidence':{}}
def test_registration_excludes_old_positions_and_retains_identity(tmp_path,monkeypatch):
 src,mkt,c,reg=fixture(tmp_path,monkeypatch);p=add_source(src,opened=AT-1000);c.close()
 c2,reg2=w.open_store(tmp_path/'own',src,mkt,AT+2000)
 assert reg2==reg
 with w.readonly(src) as s,w.readonly(mkt) as m:w.ingest(c2,s,m,reg,AT+3000)
 assert c2.execute('SELECT count(*) FROM pairs').fetchone()[0]==0
 assert c2.execute('SELECT reason FROM exclusions').fetchone()[0]=='PRE_REGISTRATION'
 c2.close()
@pytest.mark.parametrize('decision',['ACCEPT','REJECT','UNKNOWN'])
def test_decision_committed_before_any_outcome_read_and_exact_control(tmp_path,monkeypatch,decision):
 src,mkt,c,reg=fixture(tmp_path,monkeypatch);p=add_source(src);net,when=close_source(src,p)
 monkeypatch.setattr(w,'evaluate_gate',lambda *a,**k:gate(decision))
 with w.readonly(src) as s,w.readonly(mkt) as m:
  def authorize(action,arg1,arg2,*_):
   if action==sqlite3.SQLITE_READ and arg1=='positions' and arg2 in ('status','net_usdt','closed_ms','costs','exit_price','reason'):return sqlite3.SQLITE_DENY
   return sqlite3.SQLITE_OK
  s.set_authorizer(authorize);w.ingest(c,s,m,reg,p['opened_ms']+1000)
  with sqlite3.connect(tmp_path/'own/range_oi_flow.sqlite3') as other:assert other.execute('SELECT decision FROM pairs').fetchone()
  s.set_authorizer(None);row=c.execute('SELECT * FROM pairs').fetchone();w.update_control(c,s,row,when+1)
 result=w.status(c,reg,when+1,{'health':'OK','age_seconds':0})
 assert result['counts']['closed_pairs']==1 and result['results']['all_control_net_usdt']==pytest.approx(net)
 assert result['results']['unscorable_control_net_usdt']==pytest.approx(net if decision=='UNKNOWN' else 0)
 assert result['results']['control_net_usdt']==pytest.approx(0 if decision=='UNKNOWN' else net)
 assert result['results']['accepted_control_net_usdt']==pytest.approx(net if decision=='ACCEPT' else 0)
 c.close()
def test_late_discovery_is_unknown_and_does_not_call_gate(tmp_path,monkeypatch):
 src,mkt,c,reg=fixture(tmp_path,monkeypatch);p=add_source(src)
 monkeypatch.setattr(w,'evaluate_gate',lambda *a,**k:pytest.fail('Must not reconstruct late admission'))
 with w.readonly(src) as s,w.readonly(mkt) as m:w.ingest(c,s,m,reg,p['opened_ms']+180001)
 assert json.loads(c.execute('SELECT decision FROM pairs').fetchone()[0])['decision']=='UNKNOWN';c.close()
def test_plan_change_and_cost_mismatch_are_rejected(tmp_path,monkeypatch):
 src,mkt,c,reg=fixture(tmp_path,monkeypatch);p=add_source(src);net,when=close_source(src,p,999)
 with w.readonly(src) as s,w.readonly(mkt) as m:w.ingest(c,s,m,reg,p['opened_ms']+1000)
 row=c.execute('SELECT * FROM pairs').fetchone()
 with w.readonly(src) as s:
  with pytest.raises(ValueError,match='COST_ACCOUNTING'):w.update_control(c,s,row,when+1)
 with sqlite3.connect(src) as x:x.execute('UPDATE positions SET target=104 WHERE id=1')
 with w.readonly(src) as s:
  with pytest.raises(ValueError,match='SOURCE_PLAN_CHANGED'):w.update_control(c,s,row,when+1)
 c.close()
def test_frozen_code_identity_and_source_rewind_do_not_reset(tmp_path,monkeypatch):
 src,mkt,c,reg=fixture(tmp_path,monkeypatch);p=add_source(src)
 with w.readonly(src) as s,w.readonly(mkt) as m:w.ingest(c,s,m,reg,p['opened_ms']+1000)
 c.close()
 with sqlite3.connect(src) as x:x.execute('DELETE FROM positions')
 with pytest.raises(ValueError,match='REWOUND'):w.open_store(tmp_path/'own',src,mkt,AT+2000)
 with sqlite3.connect(tmp_path/'own/range_oi_flow.sqlite3') as x:assert x.execute('SELECT count(*) FROM pairs').fetchone()[0]==1
def test_risk_size_and_horizon_are_checked(tmp_path,monkeypatch):
 src,mkt,c,reg=fixture(tmp_path,monkeypatch);p=add_source(src);snap={'id':1,'symbol':'BTCUSDT','observed_ms':p['opened_ms'],'bar_ms':p['opened_ms']-1}
 with pytest.raises(ValueError,match='RISK_BUDGET'):w.validate_plan({**p,'quantity':p['quantity']*2},snap,p['opened_ms'])
 with pytest.raises(ValueError,match='EXECUTION_CONTRACT'):w.validate_plan({**p,'expires_ms':p['expires_ms']+1},snap,p['opened_ms'])
 c.close()
def test_stored_pair_error_keeps_health_degraded(tmp_path,monkeypatch):
 src,mkt,c,reg=fixture(tmp_path,monkeypatch);p=add_source(src)
 with w.readonly(src) as s,w.readonly(mkt) as m:w.ingest(c,s,m,reg,p['opened_ms']+1000)
 c.execute("UPDATE pairs SET error='CONTROL_INVALID'");c.commit()
 value=w.status(c,reg,p['opened_ms']+2000,{'health':'OK','age_seconds':0})
 assert value['health']=='DEGRADED' and value['counts']['pending']==1
 c.close()

def test_collector_outage_pauses_new_labels_but_settles_existing_controls(tmp_path,monkeypatch):
 src,mkt,c,reg=fixture(tmp_path,monkeypatch);p=add_source(src)
 with w.readonly(src) as s,w.readonly(mkt) as m:w.ingest(c,s,m,reg,p['opened_ms']+1000)
 net,when=close_source(src,p)
 src.with_name('status.json').write_text(w.canonical({'health':'OK','generated_at_ms':when}))
 mkt.with_name('status.json').write_text(w.canonical({'health':'DEGRADED','generated_at_ms':when}))
 add_source(src,sid=2,opened=when-1000)
 value=w.cycle(c,reg,src,mkt,when+1)
 assert value['health']=='DEGRADED'
 assert value['counts']['observed']==value['counts']['closed_pairs']==1
 assert value['results']['all_control_net_usdt']==pytest.approx(net)
 assert value['results']['known_closed_pairs']==0 and value['results']['unscorable_closed_pairs']==1
 c.close()
 # Even a missing collector DB must not reset the registered source/decisions.
 original=w.readonly
 def without_market(path):
  if Path(path)==mkt:raise FileNotFoundError('Simulated collector outage')
  return original(path)
 monkeypatch.setattr(w,'readonly',without_market)
 reopened,again=w.open_store(tmp_path/'own',src,mkt,when+2)
 assert again==reg;reopened.close()
