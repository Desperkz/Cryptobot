"""Isolated future-only matched RANGE labels. No network, keys or order methods."""
from __future__ import annotations
import argparse,hashlib,json,math,sqlite3,time
from contextlib import closing
from pathlib import Path
from .range_oi_flow_gate import (evaluate_gate,RULE as GATE_RULE,MAX_OI_ROWS,
    MAX_SEGMENT_ROWS,MAX_CONNECTION_ROWS,MAX_ISSUE_ROWS)

STUDY='range-oi-flow-shadow-v1';COHORT='2026-11-15-range-oi-flow-shadow-v1'
SOURCE=Path('/root/bot_diversified_v3/data/shadow.sqlite3')
MARKET=Path('/root/bot_market_evidence/data/market_evidence.sqlite3')
SOURCE_HASH='7254d8a4904cb905ae8c6f0b8dc7f87f2ab286c8352d6e6afb04c78d43e1b7a0'
MARKET_HASH='2d4676395c80334c2c89e30585e16df1afdd9c380138227e9b754a244befa60b'
SOURCE_COHORT='2026-10-21-diversified-v3-shadow-r2';STRATEGY='RANGE_REBOUND_V3'
SYMBOLS=('BTCUSDT','BNBUSDT','XRPUSDT','SOLUSDT','DOGEUSDT','LTCUSDT')
PLAN_FIELDS=('id','strategy','symbol','direction','opened_ms','entry_mid','entry','stop','target','quantity','risk_usdt','expires_ms','snapshot_id')
MAX_DISCOVERY_MS=180000
RULE={'study':STUDY,'cohort':COHORT,'mode':'SHADOW','order_capability':False,
 'combined_with_main_balance':False,'source_strategy':STRATEGY,'source_cohort':SOURCE_COHORT,
 'selection':'source id above registration watermark AND source observed_ms/bar_ms at or after registration; no old entries or replacement admissions',
 'timing':'as-of source opened_ms; decisions committed before reading outcomes; source may publish120s late and observer discovery>180s is UNKNOWN; observational labels not executable fills',
 'comparison':'known ACCEPT+REJECT closed controls versus accepted identical closed controls; UNKNOWN controls kept separately',
 'costs':'source unchanged: 2USDT cost-aware risk budget;4bp fees eachside;5bp adverse entry/exit slip;adverse1bp/8h funding; no portfolioROI claim',
 'gate':GATE_RULE,'promotion':'none; descriptive first review after14 elapsed days and50known closed pairs, never automatic paper promotion'}
LIMITATIONS=[
 'Признаки доступны к исходному времени входа; метка может публиковаться позже. Это наблюдение, а не доказательство исполнимого входа.',
 'UNKNOWN не считается отказом. Его контрольный результат показан отдельно от сопоставимого сравнения.',
 'Поток aggTrade наблюдаемый; ликвидации выборочные, редкий стакан не доказывает maker-исполнение.',
 'Суммы коррелированных модельных наблюдений не являются доходностью реального портфеля.']

def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(v):return hashlib.sha256(canonical(v).encode()).hexdigest()
def finite(x):
 if isinstance(x,bool):raise ValueError('BOOLEAN_NUMBER')
 x=float(x)
 if not math.isfinite(x):raise ValueError('NONFINITE_NUMBER')
 return x
def integer(x):
 if isinstance(x,bool) or not isinstance(x,int) or x<0:raise ValueError('INVALID_TIMESTAMP_OR_ID')
 return x
def readonly(path):
 c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.5);c.row_factory=sqlite3.Row
 c.execute('PRAGMA query_only=ON');c.execute('PRAGMA cache_size=-512');c.execute('BEGIN')
 c.set_progress_handler(lambda until=time.monotonic()+8:int(time.monotonic()>until),1000)
 return c
def manifest(c):
 raw=c.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
 if raw is None:raise ValueError('SOURCE_MANIFEST_MISSING')
 return hashlib.sha256(raw[0].encode()).hexdigest(),json.loads(raw[0])
def validate_source(src):
 sh,s=manifest(src)
 if sh!=SOURCE_HASH:raise ValueError('FROZEN_SOURCE_MANIFEST_CHANGED')
 if (s.get('cohort')!=SOURCE_COHORT or s.get('venue')!='https://fapi.binance.com'
     or s.get('mode')!='SHADOW_ONLY' or s.get('order_capability') is not False
     or s.get('settings',{}).get('risk_usdt')!=2
     or s.get('settings',{}).get('symbols')!=list(SYMBOLS)):raise ValueError('UNEXPECTED_SOURCE_PROTOCOL')
 return s
def validate_market(market):
 mh,m=manifest(market)
 if mh!=MARKET_HASH:raise ValueError('FROZEN_MARKET_MANIFEST_CHANGED')
 if (m.get('identity',{}).get('rule',{}).get('mode')!='OBSERVATION_ONLY'
     or m['identity']['rule'].get('order_capability') is not False):raise ValueError('UNEXPECTED_INPUT_PROTOCOL')
 return m
def validate_manifests(src,market):return validate_source(src),validate_market(market)
def identity(source,market):
 return {'rule':RULE,'source_path':str(Path(source).resolve()),'market_path':str(Path(market).resolve()),
  'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__),Path(__file__).with_name('range_oi_flow_gate.py'))}}
def open_store(directory,source=SOURCE,market=MARKET,now_ms=None):
 now=int(time.time()*1000) if now_ms is None else integer(now_ms);directory=Path(directory).resolve()
 if directory in (Path(source).resolve().parent,Path(market).resolve().parent):raise ValueError('USE_SEPARATE_DIRECTORY')
 directory.mkdir(parents=True,exist_ok=True)
 fixed=identity(source,market)
 c=sqlite3.connect(directory/'range_oi_flow.sqlite3',timeout=.5);c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=FULL');c.execute('PRAGMA cache_size=-512')
 c.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS pairs(id INTEGER PRIMARY KEY,opened_ms INTEGER NOT NULL,plan TEXT NOT NULL,
   plan_sha256 TEXT NOT NULL,snapshot TEXT NOT NULL,decision TEXT NOT NULL,saved_ms INTEGER NOT NULL,
   control TEXT,last_checked_ms INTEGER NOT NULL DEFAULT 0,error TEXT);
  CREATE TABLE IF NOT EXISTS exclusions(id INTEGER PRIMARY KEY,reason TEXT NOT NULL);
 ''')
 row=c.execute("SELECT value FROM meta WHERE key='registration'").fetchone()
 try:
  with closing(readonly(source)) as src:
   validate_source(src);watermark=src.execute('SELECT coalesce(max(id),0) FROM positions').fetchone()[0]
 except Exception:
  c.close();raise
 if row:
  registration=json.loads(row[0])
  if registration['identity']!=fixed:c.close();raise ValueError('FROZEN_STUDY_CHANGED')
  cursor=int(c.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0])
  if watermark<max(cursor,registration['source_watermark']):c.close();raise ValueError('SOURCE_WATERMARK_REWOUND')
 else:
  try:
   with closing(readonly(market)) as prices:m=validate_market(prices)
  except Exception:
   c.close();raise
  registration={'identity':fixed,'registered_ms':now,'source_watermark':watermark,
   'source_manifest_sha256':SOURCE_HASH,'market_manifest_sha256':MARKET_HASH,
   'collector_registered_ms':integer(m['registered_ms'])}
  with c:
   c.execute('INSERT INTO meta VALUES(?,?)',('registration',canonical(registration)))
   c.execute('INSERT INTO meta VALUES(?,?)',('cursor',str(watermark)))
 return c,registration
def bounded(c,sql,args,limit):
 return [dict(r) for r in c.execute(sql+' LIMIT ?',(*args,limit+1))]
def read_context(market,symbol,at,registered):
 target=(at//60000-1)*60000
 return {'oi_samples':bounded(market,"SELECT id,symbol,received_ms,payload FROM snapshots WHERE symbol=? AND kind='open_interest' AND received_ms BETWEEN ? AND ? ORDER BY received_ms,id",(symbol,at-420000,at),MAX_OI_ROWS),
  'segments':bounded(market,'SELECT session,symbol,minute_ms,payload FROM segments WHERE symbol=? AND minute_ms BETWEEN ? AND ? ORDER BY minute_ms,session',(symbol,target-60000,target+60000),MAX_SEGMENT_ROWS),
  'connections':bounded(market,'SELECT * FROM connections WHERE started_ms<=? AND (ended_ms IS NULL OR ended_ms>=?) ORDER BY started_ms',(at,at-420000),MAX_CONNECTION_ROWS),
  'issues':bounded(market,"SELECT at_ms,kind,symbol,detail FROM issues WHERE at_ms BETWEEN ? AND ? AND (symbol=? OR symbol='') ORDER BY at_ms",(at-420000,at,symbol),MAX_ISSUE_ROWS),
  'collector_registered_ms':registered}
def validate_plan(plan,snapshot,now):
 for key in ('id','opened_ms','expires_ms','snapshot_id'):integer(plan[key])
 if plan['strategy']!=STRATEGY or plan['symbol'] not in SYMBOLS or plan['direction'] not in ('LONG','SHORT'):raise ValueError('INVALID_SOURCE_IDENTITY')
 for key in ('entry_mid','entry','stop','target','quantity','risk_usdt'):
  if finite(plan[key])<=0:raise ValueError('INVALID_SOURCE_PRICE_OR_SIZE')
 sign=1 if plan['direction']=='LONG' else -1
 if (plan['risk_usdt']!=2 or not math.isclose(plan['entry'],plan['entry_mid']*(1+sign*.0005),rel_tol=1e-12,abs_tol=1e-10)
   or min(sign*(plan['entry']-plan['stop']),sign*(plan['target']-plan['entry']))<=0
   or plan['expires_ms']!=plan['opened_ms']+8*3600000):raise ValueError('SOURCE_EXECUTION_CONTRACT_CHANGED')
 stop_effective=plan['stop']*(1-sign*.0005)
 loss_per_unit=sign*(plan['entry']-stop_effective)+(plan['entry']+stop_effective)*.0004+plan['entry']*.0001
 if not math.isclose(plan['quantity']*loss_per_unit,2.,rel_tol=1e-10,abs_tol=1e-9):raise ValueError('SOURCE_RISK_BUDGET_CHANGED')
 for key in ('id','observed_ms','bar_ms'):integer(snapshot[key])
 if (snapshot['id']!=plan['snapshot_id'] or snapshot['symbol']!=plan['symbol']
   or snapshot['observed_ms']!=plan['opened_ms'] or not snapshot['bar_ms']<plan['opened_ms']<=now):raise ValueError('SOURCE_SNAPSHOT_MISMATCH')
def unknown(reason,at):return {'decision':'UNKNOWN','accepted':None,'data_quality':'UNKNOWN','reason':reason,'evidence':{'decision_ms':at}}
def ingest(c,src,market,registration,now):
 cursor=int(c.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0])
 watermark=src.execute('SELECT coalesce(max(id),0) FROM positions').fetchone()[0]
 if watermark<cursor:raise ValueError('SOURCE_WATERMARK_REWOUND')
 rows=src.execute('SELECT '+','.join(PLAN_FIELDS)+' FROM positions WHERE id>? ORDER BY id LIMIT 128',(cursor,)).fetchall()
 for raw in rows:
  plan=dict(raw);sid=integer(plan['id'])
  if plan['opened_ms']>now:break
  reason=None
  if plan['strategy']!=STRATEGY:reason='OTHER_STRATEGY'
  elif plan['opened_ms']<registration['registered_ms']:reason='PRE_REGISTRATION'
  snapshot=src.execute('SELECT id,symbol,observed_ms,bar_ms FROM snapshots WHERE id=?',(plan['snapshot_id'],)).fetchone() if reason is None else None
  if reason is None and snapshot is None:raise ValueError('SOURCE_SNAPSHOT_MISSING')
  if reason is None and snapshot['bar_ms']<registration['registered_ms']:reason='PRE_REGISTRATION_SIGNAL_BAR'
  if reason:
   with c:
    c.execute('INSERT OR IGNORE INTO exclusions VALUES(?,?)',(sid,reason));c.execute("UPDATE meta SET value=? WHERE key='cursor'",(str(sid),))
   continue
  snapshot=dict(snapshot);validate_plan(plan,snapshot,now)
  if now-plan['opened_ms']>MAX_DISCOVERY_MS:decision=unknown('SOURCE_DISCOVERY_AFTER_180_SECONDS',plan['opened_ms'])
  else:
   context=read_context(market,plan['symbol'],plan['opened_ms'],registration['collector_registered_ms'])
   decision=evaluate_gate(plan['symbol'],plan['direction'],plan['opened_ms'],**context)
   decision['context_sha256']=digest(context)
  decision['source_publication_observer_delay_ms']=now-plan['opened_ms']
  # The committed gate has no source status, close time, cost or outcome inputs.
  with c:
   c.execute('INSERT INTO pairs(id,opened_ms,plan,plan_sha256,snapshot,decision,saved_ms) VALUES(?,?,?,?,?,?,?)',
    (sid,plan['opened_ms'],canonical(plan),digest(plan),canonical(snapshot),canonical(decision),now))
   c.execute("UPDATE meta SET value=? WHERE key='cursor'",(str(sid),))
def update_control(c,src,pair,now):
 plan=json.loads(pair['plan'])
 current=src.execute('SELECT '+','.join(PLAN_FIELDS)+' FROM positions WHERE id=?',(pair['id'],)).fetchone()
 if current is None or digest(dict(current))!=pair['plan_sha256']:raise ValueError('SOURCE_PLAN_CHANGED')
 # Gate row is already committed. Outcome is read only after that commitment.
 row=src.execute('SELECT status,closed_ms,exit_price,net_usdt,reason,costs FROM positions WHERE id=?',(pair['id'],)).fetchone()
 if row['status']=='OPEN':
  with c:c.execute('UPDATE pairs SET last_checked_ms=? WHERE id=?',(now,pair['id']))
  return
 if row['status']!='CLOSED':raise ValueError('UNEXPECTED_CONTROL_STATUS')
 when=integer(row['closed_ms']);exit_price=finite(row['exit_price']);net=finite(row['net_usdt']);costs=json.loads(row['costs'])
 first_close=(plan['opened_ms']//60000+2)*60000-1
 if not first_close<=when<=min(now,plan['expires_ms']+60000) or when%60000!=59999 or exit_price<=0:raise ValueError('INVALID_CONTROL_CLOSE_TIME_OR_PRICE')
 sign=1 if plan['direction']=='LONG' else -1;entry=plan['entry'];q=plan['quantity']
 fees=(entry+exit_price)*q*.0004;funding=entry*q*.0001*(when-plan['opened_ms'])/28800000
 expected=sign*(exit_price-entry)*q-fees-funding
 trigger=exit_price/(1-sign*.0005)
 reason=row['reason']
 if reason not in ('STOP','STOP_FIRST_AMBIGUOUS','TARGET','TIMEOUT'):raise ValueError('UNEXPECTED_CONTROL_CLOSE_REASON')
 if reason=='TARGET' and not math.isclose(trigger,plan['target'],rel_tol=1e-10,abs_tol=1e-9):raise ValueError('CONTROL_TARGET_CHANGED')
 if reason.startswith('STOP') and sign*(trigger-plan['stop'])>1e-8:raise ValueError('CONTROL_STOP_CHANGED')
 if reason=='TIMEOUT' and when<plan['expires_ms']:raise ValueError('EARLY_CONTROL_TIMEOUT')
 if (not math.isclose(net,expected,rel_tol=1e-10,abs_tol=1e-9)
  or not math.isclose(finite(costs['fees']),fees,rel_tol=1e-10,abs_tol=1e-9)
  or not math.isclose(finite(costs['funding']),funding,rel_tol=1e-10,abs_tol=1e-9)
  or not math.isclose(finite(costs['entry_slippage']),abs(entry-plan['entry_mid'])*q,rel_tol=1e-10,abs_tol=1e-9)
  or not math.isclose(finite(costs['exit_slippage']),abs(trigger-exit_price)*q,rel_tol=1e-10,abs_tol=1e-9)
  or not math.isclose(finite(costs['gross_before_exit_slippage']),sign*(trigger-entry)*q,rel_tol=1e-10,abs_tol=1e-9)
  or costs.get('funding_source')!='adverse_1bp_per8h_buffer'):raise ValueError('CONTROL_COST_ACCOUNTING_MISMATCH')
 result={'closed_ms':when,'net_usdt':net,'reason':row['reason'],'costs':costs,'exit_price':exit_price}
 with c:c.execute('UPDATE pairs SET control=?,last_checked_ms=?,error=NULL WHERE id=? AND control IS NULL',(canonical(result),now,pair['id']))
def status(c,registration,now,source,errors=()):
 errors=list(errors)
 counts={k:0 for k in ('observed','accepted','rejected','unscorable','pending','closed_pairs')}
 results={k:0. for k in ('control_net_usdt','accepted_control_net_usdt','prevented_losses_usdt','missed_profits_usdt','paired_delta_usdt','all_control_net_usdt','unscorable_control_net_usdt')}
 results.update({'known_closed_pairs':0,'unscorable_closed_pairs':0});reasons={}
 for row in c.execute('SELECT id,decision,control,error FROM pairs'):
  d=json.loads(row['decision']);decision=d['decision'];counts['observed']+=1
  counts[{'ACCEPT':'accepted','REJECT':'rejected','UNKNOWN':'unscorable'}[decision]]+=1
  reasons[d['reason']]=reasons.get(d['reason'],0)+1
  if row['error']:
   marker=str(row['id'])+':'+row['error']
   if marker not in errors:errors.append(marker)
  if row['control'] is None:counts['pending']+=1;continue
  net=finite(json.loads(row['control'])['net_usdt']);counts['closed_pairs']+=1;results['all_control_net_usdt']+=net
  if decision=='UNKNOWN':results['unscorable_closed_pairs']+=1;results['unscorable_control_net_usdt']+=net;continue
  results['known_closed_pairs']+=1;results['control_net_usdt']+=net
  if decision=='ACCEPT':results['accepted_control_net_usdt']+=net
  else:results['prevented_losses_usdt']+=max(-net,0);results['missed_profits_usdt']+=max(net,0)
 results['paired_delta_usdt']=results['accepted_control_net_usdt']-results['control_net_usdt']
 assert math.isclose(results['paired_delta_usdt'],results['prevented_losses_usdt']-results['missed_profits_usdt'],abs_tol=1e-9,rel_tol=1e-10)
 elapsed=max(0,(now-registration['registered_ms'])/86400000)
 return {'study':STUDY,'cohort':COHORT,'mode':'SHADOW','order_capability':False,'combined_with_main_balance':False,
  'registered_ms':registration['registered_ms'],'generated_at_ms':now,'health':'DEGRADED' if errors or source.get('health')!='OK' else 'OK',
  'source':source,'counts':counts,'results':results,'errors':list(errors),'limitations':LIMITATIONS,
  'decision_reasons':reasons,'review':{'elapsed_days':elapsed,'required_days':14,'required_known_closed_pairs':50,
    'ready':elapsed>=14 and results['known_closed_pairs']>=50,'automatic_promotion':False}}
def input_health(source,market,now):
 telemetry={};errors=[]
 for label,path,limit in (('strategy',Path(source).with_name('status.json'),900000),('collector',Path(market).with_name('status.json'),180000)):
  try:
   if path.stat().st_size>131072:raise ValueError('STATUS_TOO_LARGE')
   v=json.loads(path.read_text());age=now-integer(v['generated_at_ms'])
   if v.get('health')!='OK' or not 0<=age<=limit:raise ValueError('STALE_OR_UNHEALTHY')
   telemetry[label]={'health':'OK','age_seconds':age/1000}
  except (OSError,ValueError,KeyError,TypeError):telemetry[label]={'health':'UNAVAILABLE','age_seconds':None};errors.append(label.upper()+'_NOT_FRESH_AND_HEALTHY')
 return {'health':'OK' if not errors else 'DEGRADED','age_seconds':max((x['age_seconds'] for x in telemetry.values() if x['age_seconds'] is not None),default=None),'inputs':telemetry},errors
def cycle(c,registration,source=SOURCE,market=MARKET,now_ms=None):
 now=int(time.time()*1000) if now_ms is None else integer(now_ms);telemetry,errors=input_health(source,market,now)
 if telemetry['inputs']['strategy']['health']=='OK':
  try:
   with closing(readonly(source)) as src:
    validate_source(src)
    if telemetry['inputs']['collector']['health']=='OK':
     try:
      with closing(readonly(market)) as prices:
       validate_market(prices);ingest(c,src,prices,registration,now)
     except (sqlite3.Error,OSError,ValueError,KeyError,TypeError) as exc:errors.append('COLLECTOR:'+type(exc).__name__+':'+str(exc)[:160])
    rows=c.execute('SELECT * FROM pairs WHERE control IS NULL ORDER BY last_checked_ms,id LIMIT 64').fetchall()
    for row in rows:
     try:update_control(c,src,row,now)
     except (ValueError,TypeError,KeyError) as exc:
      with c:c.execute('UPDATE pairs SET error=?,last_checked_ms=? WHERE id=?',(str(exc)[:200],now,row['id']))
      errors.append(str(row['id'])+':'+str(exc)[:160])
  except (sqlite3.Error,OSError,ValueError,KeyError,TypeError) as exc:errors.append(type(exc).__name__+':'+str(exc)[:200])
 return status(c,registration,now,telemetry,errors)
def write_status(directory,value):
 path=Path(directory)/'status.json';temp=path.with_suffix('.json.tmp');temp.write_text(canonical(value),encoding='utf-8');temp.replace(path)
def main():
 p=argparse.ArgumentParser();p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--source',type=Path,default=SOURCE);p.add_argument('--market',type=Path,default=MARKET);args=p.parse_args()
 c,registration=open_store(args.data_dir,args.source,args.market)
 try:
  value=cycle(c,registration,args.source,args.market);write_status(args.data_dir,value)
  print(canonical({'health':value['health'],'counts':value['counts'],'errors':value['errors']}))
 finally:c.close()
if __name__=='__main__':main()
