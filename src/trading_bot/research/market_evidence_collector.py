"""Public market observations only: no keys, order API, account access or PnL."""
from __future__ import annotations
import argparse,asyncio,hashlib,json,math,shutil,signal,sqlite3,time,urllib.parse,urllib.request,uuid
from pathlib import Path

SYMBOLS=('BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','LTCUSDT','ADAUSDT')
MINUTE=60000
RULE={'study':'market-evidence-20261110-v1','mode':'OBSERVATION_ONLY','order_capability':False,
 'symbols':list(SYMBOLS),'websocket':'wss://fstream.binance.com/market/stream',
 'rest':'https://fapi.binance.com','rest_interval_seconds':60,'flush_seconds':10,
 'storage_limit_bytes':256*1024*1024,'minimum_free_disk_bytes':1024*1024*1024,
 'data':'minute aggressor-volume segments; sampled OI, five-level book, mark and funding indication; raw liquidation snapshots',
 'limits':'Liquidation stream is sampled, not total liquidations. Book snapshots do not prove maker fills or queue position. Unknown/missing intervals are not zero. No backfilled observations or strategy decisions.'}

def now_ms():return int(time.time()*1000)
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False)
def number(x,positive=False):
 if isinstance(x,bool):raise ValueError('BOOLEAN_NUMBER')
 f=float(x)
 if not math.isfinite(f) or (positive and f<=0):raise ValueError('INVALID_NUMBER')
 return f
def integer(x):
 if isinstance(x,bool) or not isinstance(x,int) or x<0:raise ValueError('INVALID_INTEGER')
 return x
def event_time(at,received):
 integer(at);integer(received)
 if not -5000<=received-at<=30000:raise ValueError('EVENT_TIME_OUTSIDE_OBSERVATION_WINDOW')

def resource_check(directory):
 if shutil.disk_usage(directory).free<RULE['minimum_free_disk_bytes']:raise RuntimeError('DISK_HEADROOM_LOW')
 used=sum(p.stat().st_size for name in ('market_evidence.sqlite3','market_evidence.sqlite3-wal') if (p:=directory/name).exists())
 if used>=RULE['storage_limit_bytes']:raise RuntimeError('OBSERVATION_STORAGE_LIMIT')

class Store:
 def __init__(self,directory,at=None):
  self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
  at=now_ms() if at is None else integer(at)
  self.db=sqlite3.connect(self.directory/'market_evidence.sqlite3',timeout=1)
  self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
  self.db.execute('PRAGMA cache_size=-512');self.db.execute('PRAGMA journal_size_limit=4194304')
  self.db.execute('PRAGMA max_page_count=65536')
  self.db.executescript('''
   CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
   CREATE TABLE IF NOT EXISTS segments(session TEXT,symbol TEXT,minute_ms INTEGER,payload TEXT NOT NULL,PRIMARY KEY(session,symbol,minute_ms));
   CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY,symbol TEXT,kind TEXT,received_ms INTEGER,payload TEXT NOT NULL);
   CREATE INDEX IF NOT EXISTS snapshot_time ON snapshots(symbol,kind,received_ms);
   CREATE TABLE IF NOT EXISTS liquidations(digest TEXT PRIMARY KEY,received_ms INTEGER,payload TEXT NOT NULL);
   CREATE TABLE IF NOT EXISTS issues(id INTEGER PRIMARY KEY,at_ms INTEGER,kind TEXT,symbol TEXT,detail TEXT);
   CREATE TABLE IF NOT EXISTS connections(session TEXT PRIMARY KEY,started_ms INTEGER,last_received_ms INTEGER,ended_ms INTEGER,reason TEXT);
  ''')
  fixed={'rule':RULE,'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
  old=self.db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
  if old:
   self.manifest=json.loads(old[0])
   if self.manifest['identity']!=fixed:self.db.close();raise ValueError('FROZEN_COLLECTOR_CHANGED')
  else:
   self.manifest={'identity':fixed,'registered_ms':at}
   with self.db:self.db.execute('INSERT INTO meta VALUES(?,?)',('manifest',canonical(self.manifest)))
  saved=self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()
  self.state=json.loads(saved[0]) if saved else {'last_ids':{},'counts':{'agg_trades':0,'liquidation_snapshots':0,'rest_snapshots':0,'id_gaps':0,'invalid_events':0},'last_flush_ms':at}
  self.latest={};self.buckets={};self.pending=[];self.connected=False;self.session=None;self.last_received={};self.last_rest={};self.last_issue=None
  if saved:
   self.issue('PROCESS_RESTART_GAP','',{'last_committed_ms':self.state['last_flush_ms'],'resumed_ms':at},at)
   self.db.execute("UPDATE connections SET ended_ms=?,reason='PROCESS_RESTART' WHERE ended_ms IS NULL",(at,))
 def issue(self,kind,symbol,detail,at=None):
  self.last_issue={'kind':kind,'symbol':symbol,'at_ms':now_ms() if at is None else at}
  self.db.execute('INSERT INTO issues(at_ms,kind,symbol,detail) VALUES(?,?,?,?)',
    (self.last_issue['at_ms'],kind,symbol,canonical(detail)[:2048]))
 def begin(self,at):
  self.session=uuid.uuid4().hex;self.connected=True
  self.db.execute('INSERT INTO connections VALUES(?,?,?,NULL,NULL)',(self.session,at,at))
  self.db.commit()
 def end(self,at,reason):
  self.connected=False
  if self.session:self.db.execute('UPDATE connections SET ended_ms=?,reason=? WHERE session=?',(at,reason,self.session))
  self.issue('WEBSOCKET_DISCONNECTED','',{'reason':reason},at)
 def event(self,event,received):
  if not self.session:raise ValueError('NO_OBSERVATION_SESSION')
  kind=event['e'];symbol=event.get('s',event.get('o',{}).get('s'))
  if symbol not in SYMBOLS:raise ValueError('UNEXPECTED_SYMBOL')
  event_time(event['E'],received)
  if kind=='aggTrade':
   trade_id=integer(event['a']);event_time(event['T'],received)
   if type(event['m']) is not bool:raise ValueError('INVALID_AGGRESSOR_SIDE')
   price=number(event['p'],True);quantity=number(event['q'],True)
   old=self.state['last_ids'].get(symbol)
   if old is not None and trade_id<=old:return False
   if old is not None and trade_id!=old+1:
    self.state['counts']['id_gaps']+=1
    self.issue('AGGREGATE_ID_GAP',symbol,{'previous':old,'current':trade_id},received)
   self.state['last_ids'][symbol]=trade_id
   minute=event['T']//MINUTE*MINUTE;key=(self.session,symbol,minute)
   b=self.buckets.setdefault(key,{'buy_quote':0.,'sell_quote':0.,'buy_base':0.,'sell_base':0.,'messages':0,
     'first_id':trade_id,'last_id':trade_id,'first_event_ms':event['T'],'last_event_ms':event['T'],
     'first_received_ms':received,'last_received_ms':received,'coverage':'OBSERVED_SEGMENT_NOT_CERTIFIED_FULL_MINUTE'})
   side='sell' if event['m'] else 'buy';b[side+'_quote']+=price*quantity;b[side+'_base']+=quantity
   b['messages']+=1;b['last_id']=trade_id;b['last_event_ms']=event['T'];b['last_received_ms']=received
   self.last_received[symbol]=received;self.state['counts']['agg_trades']+=1
  elif kind=='forceOrder':
   order=event['o'];event_time(order['T'],received)
   if order['S'] not in ('BUY','SELL'):raise ValueError('INVALID_LIQUIDATION_SIDE')
   number(order['q'],True)
   if min(number(order['ap']),number(order['z']))<0:raise ValueError('INVALID_LIQUIDATION_EXECUTION')
   raw=canonical(event);digest=hashlib.sha256(raw.encode()).hexdigest()
   self.pending.append((digest,received,raw))
  else:raise ValueError('UNEXPECTED_EVENT_TYPE')
  self.db.execute('UPDATE connections SET last_received_ms=? WHERE session=?',(received,self.session))
  return True
 def snapshot(self,symbol,kind,payload,started,received):
  if symbol not in SYMBOLS or not 0<=received-started<=20000:raise ValueError('INVALID_REST_OBSERVATION')
  if kind=='open_interest':
   if payload['symbol']!=symbol:raise ValueError('SYMBOL_MISMATCH')
   number(payload['openInterest'],True);event_time(payload['time'],received)
  elif kind=='book5':
   bids,asks=payload['bids'],payload['asks']
   if not bids or not asks or len(bids)>5 or len(asks)>5:raise ValueError('INVALID_BOOK')
   for levels in (bids,asks):
    for price,qty in levels:number(price,True);number(qty,True)
   if float(bids[0][0])>float(asks[0][0]):raise ValueError('CROSSED_BOOK')
   if any(float(bids[i][0])>=float(bids[i-1][0]) for i in range(1,len(bids))) or any(float(asks[i][0])<=float(asks[i-1][0]) for i in range(1,len(asks))):raise ValueError('UNSORTED_BOOK')
   event_time(payload['E'],received)
  elif kind=='mark_funding_indication':
   if payload['symbol']!=symbol:raise ValueError('SYMBOL_MISMATCH')
   number(payload['markPrice'],True)
   if abs(number(payload['lastFundingRate']))>.1:raise ValueError('INVALID_FUNDING_INDICATION')
   event_time(payload['time'],received)
  else:raise ValueError('UNKNOWN_SNAPSHOT')
  value={'request_started_ms':started,'received_ms':received,'data':payload}
  self.db.execute('INSERT INTO snapshots(symbol,kind,received_ms,payload) VALUES(?,?,?,?)',(symbol,kind,received,canonical(value)))
  self.latest.setdefault(symbol,{})[kind]=value;self.last_rest[(symbol,kind)]=received
  self.state['counts']['rest_snapshots']+=1
 def flush(self,at):
  for key,b in self.buckets.items():
   self.db.execute('INSERT INTO segments VALUES(?,?,?,?) ON CONFLICT(session,symbol,minute_ms) DO UPDATE SET payload=excluded.payload',(*key,canonical(b)))
  for digest,received,raw in self.pending:
   cur=self.db.execute('INSERT OR IGNORE INTO liquidations VALUES(?,?,?)',(digest,received,raw))
   self.state['counts']['liquidation_snapshots']+=cur.rowcount
  self.state['last_flush_ms']=at
  self.db.execute("INSERT INTO meta VALUES('state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(canonical(self.state),))
  self.db.commit();self.pending.clear()
  self.buckets={k:v for k,v in self.buckets.items() if k[2]>=at//MINUTE*MINUTE-MINUTE}
 def status(self,at):
  stale=[]
  for s in SYMBOLS:
   if not 0<=at-self.last_received.get(s,-10**15)<=90000:stale.append(s+':AGG_TRADE_MISSING_OR_STALE')
   for k in ('open_interest','book5','mark_funding_indication'):
    if not 0<=at-self.last_rest.get((s,k),-10**15)<=150000:stale.append(s+':'+k+'_MISSING_OR_STALE')
  if not self.connected:stale.append('WEBSOCKET_OFFLINE')
  return {'study':RULE['study'],'mode':'OBSERVATION_ONLY','order_capability':False,'generated_at_ms':at,
   'registered_ms':self.manifest['registered_ms'],'health':'OK' if not stale else 'DEGRADED',
   'counts':self.state['counts'],'errors':stale,'last_issue':self.last_issue,'connected':self.connected,
   'last_agg_received_ms':self.last_received,'latest_public_snapshots':self.latest,'limitations':RULE['limits']}
 def publish(self,at):
  self.flush(at);value=self.status(at);p=self.directory/'status.json';tmp=p.with_suffix('.json.tmp')
  tmp.write_text(canonical(value),encoding='utf-8');tmp.replace(p)
  return value
 def close(self,at=None):
  at=now_ms() if at is None else at
  if self.connected:self.end(at,'CLEAN_STOP')
  self.publish(at);self.db.close()

def public_get(symbol,kind):
 paths={'open_interest':('/fapi/v1/openInterest',{'symbol':symbol}),
  'book5':('/fapi/v1/depth',{'symbol':symbol,'limit':5}),
  'mark_funding_indication':('/fapi/v1/premiumIndex',{'symbol':symbol})}
 path,params=paths[kind];url=RULE['rest']+path+'?'+urllib.parse.urlencode(params)
 started=now_ms()
 with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Cryptobot-public-research/1'}),timeout=8) as response:
  raw=response.read(131073)
 if len(raw)>131072:raise ValueError('REST_RESPONSE_TOO_LARGE')
 return json.loads(raw),started,now_ms()

async def stream(store,stop):
 import websockets
 names=[s.lower()+suffix for s in SYMBOLS for suffix in ('@aggTrade','@forceOrder')]
 url=RULE['websocket']+'?streams='+'/'.join(names);backoff=1
 while not stop.is_set():
  try:
   async with websockets.connect(url,open_timeout=12,ping_interval=20,ping_timeout=20,max_size=32768,max_queue=128,compression=None) as ws:
    store.begin(now_ms());backoff=1
    while not stop.is_set():
     try:raw=await asyncio.wait_for(ws.recv(),timeout=30)
     except asyncio.TimeoutError:raise RuntimeError('MARKET_STREAM_NO_MESSAGES')
     try:
      message=json.loads(raw);store.event(message.get('data',message),now_ms())
     except (ValueError,KeyError,TypeError,OverflowError) as e:
      store.state['counts']['invalid_events']+=1
      store.issue('INVALID_STREAM_EVENT','',{'error':str(e)[:160]})
  except asyncio.CancelledError:raise
  except Exception as e:
   store.end(now_ms(),type(e).__name__);store.publish(now_ms())
   try:await asyncio.wait_for(stop.wait(),timeout=backoff)
   except asyncio.TimeoutError:pass
   backoff=min(backoff*2,60)

async def rest(store,stop):
 while not stop.is_set():
  began=time.monotonic()
  for symbol in SYMBOLS:
   for kind in ('open_interest','book5','mark_funding_indication'):
    if stop.is_set():return
    try:
     payload,started,received=await asyncio.to_thread(public_get,symbol,kind)
     store.snapshot(symbol,kind,payload,started,received)
    except Exception as e:store.issue('REST_ERROR',symbol,{'kind':kind,'error':type(e).__name__})
  try:await asyncio.wait_for(stop.wait(),timeout=max(1,60-(time.monotonic()-began)))
  except asyncio.TimeoutError:pass

async def run(directory,duration=0):
 resource_check(directory);store=Store(directory);stop=asyncio.Event();loop=asyncio.get_running_loop()
 for sig in (signal.SIGINT,signal.SIGTERM):
  try:loop.add_signal_handler(sig,stop.set)
  except NotImplementedError:pass
 tasks=[asyncio.create_task(stream(store,stop)),asyncio.create_task(rest(store,stop))]
 began=time.monotonic()
 try:
  while not stop.is_set():
   resource_check(directory);status=store.publish(now_ms())
   print(canonical({'health':status['health'],'counts':status['counts']}),flush=True)
   for task in tasks:
    if task.done():task.result();raise RuntimeError('COLLECTOR_TASK_STOPPED')
   if duration and time.monotonic()-began>=duration:stop.set();break
   try:await asyncio.wait_for(stop.wait(),timeout=10)
   except asyncio.TimeoutError:pass
 finally:
  stop.set()
  for task in tasks:task.cancel()
  await asyncio.gather(*tasks,return_exceptions=True);store.close()

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--data-dir',type=Path,required=True);ap.add_argument('--duration',type=int,default=0);args=ap.parse_args()
 args.data_dir.mkdir(parents=True,exist_ok=True);asyncio.run(run(args.data_dir,args.duration))
if __name__=='__main__':main()
