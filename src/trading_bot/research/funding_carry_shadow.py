"""Future-only public-data funding carry SHADOW. Has no exchange order methods."""
from __future__ import annotations
import argparse,copy,hashlib,json,math,sqlite3,time,urllib.parse,urllib.request
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING
from pathlib import Path
from .funding_carry_ledger import CarryLedger,finite

DAY=86400000;HOUR=3600000;WINDOW=600000
STUDY='funding-carry-shadow-v1';COHORT='2026-11-09-btc-eth-funding-carry-v1'
SYMBOLS=('BTCUSDT','ETHUSDT')
FILTERS={'BTCUSDT':{'step':.001,'perp_min':50.,'spot_min':5.},
         'ETHUSDT':{'step':.001,'perp_min':20.,'spot_min':5.}}
RULE={'study':STUDY,'cohort':COHORT,'mode':'SHADOW','order_capability':False,
 'combined_with_main_balance':False,'capital_usdt':1000,'target_spot_notional_usdt':100,
 'symbols':list(SYMBOLS),'spot_fee_bps':10,'perp_fee_bps':4,'adverse_slippage_bps_each_leg':5,
 'exchange_filter_snapshot':FILTERS,
 'quantity':'matched underlying units, current common LOT_SIZE and minimum notional',
 'rebalance':'previousUTCdayclose notional below50/above150; future01UTCobservedquotes within10minwindow',
 'funding':'actualsettledrate*historicalmark*heldshortqty; negativefundingpaid; no backdatedentries',
 'valuation':'cash+spot+shortUPNL lessremainingfees,spreadandadverseslip; separatefrommainpaper',
 'fee_settlement':'quote-equivalent model fees and matched net units; gross-order/base-fee/dust execution not claimed verified',
 'promotion':'measurement only; no automatic production promotion'}

def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False)
def code_hashes():
 files=(Path(__file__),Path(__file__).with_name('funding_carry_ledger.py'))
 return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
def initial_state(registered):
 return {'registered_ms':registered,'first_decision_ms':(registered//DAY+1)*DAY,'last_decision_ms':0,
  'books':[CarryLedger(1000).snapshot(),CarryLedger(1000,.002,.0008,.001).snapshot(),CarryLedger(1000,0,0,0).snapshot()],
  'funding_cursors':{s:registered for s in SYMBOLS},'opened_ms':{},'spread_cost':0.,'orders':0,'funding_payments':0,
  'missed_days':0,'minimum_order_skips':0,'blocked':False}

def quote_valid(q,now):
 for key in ('spot_bid','spot_ask','perp_bid','perp_ask','mark'):
  finite(q[key],True)
 for side in ('spot','perp'):
  bid,ask=q[side+'_bid'],q[side+'_ask']
  if not bid<=ask or (ask-bid)/bid>.01:raise ValueError('INVALID_OR_WIDE_QUOTE')
 if not isinstance(q['received_ms'],int) or not 0<=now-q['received_ms']<=30000:raise ValueError('STALE_QUOTE')

def target_quantity(spot,perp,filters):
 step=Decimal(str(filters['step']));target=Decimal('100')
 n=int((target/Decimal(str(spot))/step).to_integral_value(rounding=ROUND_FLOOR))
 minimum=int((Decimal(str(filters['perp_min']))/(Decimal(str(perp))*(1-Decimal('.0005')))/step).to_integral_value(rounding=ROUND_CEILING))
 return max(n,minimum,1)*float(step)

def advance(saved,now,payload):
 """Pure tick; caller atomically stores returned ledger, payments and orders."""
 state=copy.deepcopy(saved);books=[CarryLedger.restore(x) for x in state['books']];payments=[];orders=[]
 if now<state['registered_ms'] or now<state['last_decision_ms']:raise ValueError('CLOCK_BEFORE_COHORT')
 if any(max(b.qty(s) for b in books)-min(b.qty(s) for b in books)>1e-12 for s in SYMBOLS):raise ValueError('UNMATCHED_SAVED_BOOKS')
 errors=list(payload.get('errors',[]));quotes=payload.get('quotes',{});filters=payload.get('filters',{})
 all_quotes=True
 for symbol in SYMBOLS:
  try:quote_valid(quotes[symbol],now)
  except (KeyError,ValueError,TypeError):all_quotes=False;errors.append(symbol+':QUOTE_UNAVAILABLE')
 funding_ok=True
 for symbol in SYMBOLS:
  rows=payload.get('funding',{}).get(symbol)
  if not isinstance(rows,list):funding_ok=False;errors.append(symbol+':FUNDING_UNAVAILABLE');continue
  cursor=state['funding_cursors'][symbol];previous=None
  for row in rows:
   at=row['fundingTime']
   if not isinstance(at,int) or (previous is not None and at<previous):raise ValueError('UNORDERED_FUNDING')
   previous=at
   if at<=state['funding_cursors'][symbol]:continue
   if at>now:break
   rate=finite(row['fundingRate']);mark=finite(row['markPrice'],True)
   if abs(rate)>.05 or row.get('symbol')!=symbol:raise ValueError('INVALID_FUNDING')
   q=books[0].qty(symbol)
   if q and at<state['opened_ms'][symbol]:raise ValueError('PRE_ENTRY_FUNDING')
   values=[b.settlement(symbol,rate,mark,at) for b in books]
   if max(values)-min(values)>1e-10:raise ValueError('UNMATCHED_BOOKS')
   if q:
    payments.append({'symbol':symbol,'at_ms':at,'quantity':q,'rate':rate,'mark':mark,'cash':values[0]});state['funding_payments']+=1
   state['funding_cursors'][symbol]=at
  if len(rows)>=1000:funding_ok=False;errors.append(symbol+':FUNDING_BACKLOG')
 today=now//DAY*DAY;due=today+HOUR;attempt=False
 if today>=state['first_decision_ms'] and today>state['last_decision_ms']:
  if now>due+WINDOW:
   state['last_decision_ms']=today;state['missed_days']+=1
  elif now>=due and all_quotes and funding_ok and not errors and not state['blocked']:
   attempt=True
 def trade(symbol,quantity,reason):
  q=quotes[symbol];old=books[0].qty(symbol);delta=quantity-old
  if abs(delta)<1e-12:return
  midspot=(q['spot_bid']+q['spot_ask'])/2;midperp=(q['perp_bid']+q['perp_ask'])/2
  spot=q['spot_ask'] if delta>0 else q['spot_bid'];perp=q['perp_bid'] if delta>0 else q['perp_ask']
  state['spread_cost']+=abs(delta)*(abs(spot-midspot)+abs(perp-midperp))
  books[0].trade(symbol,quantity,spot,perp,now);books[1].trade(symbol,quantity,spot,perp,now);books[2].trade(symbol,quantity,midspot,midperp,now)
  if old==0 and quantity>0:state['opened_ms'][symbol]=now
  if quantity==0:state['opened_ms'].pop(symbol,None)
  orders.append({**books[0].events[-1],'reason':reason,'source_decision_ms':today,
                 'spot_long_quantity':quantity,'perp_short_quantity':quantity});state['orders']+=1
 if all_quotes:
  marks={s:quotes[s]['mark'] for s in SYMBOLS}
  if min(b.margin_surplus(marks,.01) for b in books[:2])<0:
   for symbol in SYMBOLS:
    if books[0].qty(symbol):trade(symbol,0,'MARGIN_GUARD')
   state['blocked']=True;errors.append('COLLATERAL_GUARD_BLOCKED')
 if attempt and not state['blocked']:
  closes=payload.get('previous_day_close',{})
  if not all(s in closes and closes[s]['close_ms']==today-1 for s in SYMBOLS):errors.append('SOURCE_DAY_UNAVAILABLE')
  else:
   for symbol in SYMBOLS:
    close=finite(closes[symbol]['price'],True);held=books[0].qty(symbol)
    if held and 50<=held*close<=150:continue
    f=filters[symbol];step=finite(f['step'],True);minimum=finite(f['perp_min'],True);spotmin=finite(f['spot_min'],True)
    q=quotes[symbol];quantity=target_quantity(q['spot_ask'],q['perp_bid'],f);delta=quantity-held
    if abs(delta)<1e-12:continue
    if ((not held and quantity*q['spot_ask']>150) or abs(delta)*q['spot_bid']<spotmin
       or (delta>0 and delta*q['perp_bid']*.9995<minimum)):
     state['minimum_order_skips']+=1;continue
    if abs(quantity/step-round(quantity/step))>1e-6:raise ValueError('INVALID_LOT_QUANTITY')
    trade(symbol,quantity,'INITIAL_HEDGE' if held==0 else 'DAILY_REBALANCE')
   state['last_decision_ms']=today
 state['books']=[b.snapshot() for b in books]
 pnl=None;holdings=[{'symbol':s,'spot_long_quantity':books[0].qty(s),'perp_short_quantity':books[0].qty(s),
                    'opened_ms':state['opened_ms'][s]} for s in SYMBOLS if books[0].qty(s)];collateral=None
 if all_quotes and funding_ok and not errors:
  side_prices={s:(quotes[s]['spot_bid'],quotes[s]['perp_ask']) for s in SYMBOLS}
  mid_prices={s:((quotes[s]['spot_bid']+quotes[s]['spot_ask'])/2,(quotes[s]['perp_bid']+quotes[s]['perp_ask'])/2) for s in SYMBOLS}
  eq,_,fees,slip=books[0].liquidation(side_prices);stress=books[1].liquidation(side_prices)[0];gross=books[2].liquidation(mid_prices)[0]
  funding=sum(books[0].funding.values());spread=state['spread_cost']
  for s in SYMBOLS:
   qty=books[0].qty(s);q=quotes[s];sm,pm=mid_prices[s]
   spread+=qty*((sm-q['spot_bid'])+(q['perp_ask']-pm))
   if qty:
    next(row for row in holdings if row['symbol']==s).update(spot_bid=q['spot_bid'],perp_ask=q['perp_ask'],mark=q['mark'])
  basis=gross-1000-funding;net=eq-1000
  if abs(net-(basis+funding-fees-slip-spread))>1e-7:raise ValueError('CASH_DECOMPOSITION_MISMATCH')
  pnl={'balance_usdt':eq,'net_usdt':net,'capital_return_pct':net/10,'actual_funding_cash_usdt':funding,
       'model_fees_usdt':fees,'model_slippage_usdt':slip,'observed_spread_usdt':spread,'basis_pnl_usdt':basis,'stress_net_usdt':stress-1000}
  collateral=min(b.margin_surplus({s:quotes[s]['mark'] for s in SYMBOLS},.01) for b in books[:2])
 next_day=today if today>=state['first_decision_ms'] and today>state['last_decision_ms'] and now<=due+WINDOW else max(state['first_decision_ms'],today+DAY)
 status={**RULE,'registered_ms':state['registered_ms'],'generated_at_ms':now,'health':'DEGRADED' if errors else 'OK',
  'errors':errors,'next_execution_ms':next_day+HOUR,'counts':{k:state[k] for k in ('orders','funding_payments','missed_days','minimum_order_skips')},
  'open_hedge_pairs':sum(books[0].qty(s)>0 for s in SYMBOLS),'pnl':pnl,'holdings':holdings,
  'collateral_surplus_usdt':collateral,'blocked':state['blocked']}
 return state,payments,orders,status

class PublicClient:
 def __init__(self):self.deadline=time.monotonic()+38
 def get(self,venue,path,query):
  if venue not in ('spot','perp'):raise ValueError('UNAPPROVED_VENUE')
  allowed={'spot':('/api/v3/ticker/bookTicker','/api/v3/klines','/api/v3/exchangeInfo'),
           'perp':('/fapi/v1/ticker/bookTicker','/fapi/v1/premiumIndex','/fapi/v1/fundingRate','/fapi/v1/exchangeInfo')}
  if path not in allowed[venue]:raise ValueError('UNAPPROVED_PUBLIC_ENDPOINT')
  remaining=self.deadline-time.monotonic()
  if remaining<=0:raise TimeoutError('CYCLE_DEADLINE')
  base='https://api.binance.com' if venue=='spot' else 'https://fapi.binance.com'
  request=urllib.request.Request(base+path+'?'+urllib.parse.urlencode(query),headers={'User-Agent':'Cryptobot-Shadow-Research/1.0'},method='GET')
  with urllib.request.urlopen(request,timeout=min(5,remaining)) as r:raw=r.read(2*1024*1024+1)
  if len(raw)>2*1024*1024:raise ValueError('OVERSIZED_RESPONSE')
  return json.loads(raw)
 def inputs(self,state,filters,now):
  data={'quotes':{},'funding':{},'filters':filters,'errors':[],'previous_day_close':{}}
  day=now//DAY*DAY;need_day=day>=state['first_decision_ms'] and day>state['last_decision_ms'] and day+HOUR<=now<=day+HOUR+WINDOW
  if need_day:
   try:
    spot_rules=self.get('spot','/api/v3/exchangeInfo',{'symbols':json.dumps(list(SYMBOLS),separators=(',',':'))})
    perp_rules=self.get('perp','/fapi/v1/exchangeInfo',{})
    def rules(data):return {r['symbol']:{f['filterType']:f for f in r['filters']} for r in data['symbols'] if r['symbol'] in SYMBOLS}
    sr,fr=rules(spot_rules),rules(perp_rules)
    current={s:{'step':max(float(sr[s]['LOT_SIZE']['stepSize']),float(fr[s]['LOT_SIZE']['stepSize'])),
                'perp_min':float(fr[s]['MIN_NOTIONAL']['notional']),'spot_min':float(sr[s]['NOTIONAL']['minNotional'])} for s in SYMBOLS}
    if current!=FILTERS:data['errors'].append('EXCHANGE_FILTERS_CHANGED')
   except Exception as exc:data['errors'].append('FILTERS_'+type(exc).__name__)
  for s in SYMBOLS:
   try:
    rows=self.get('perp','/fapi/v1/fundingRate',{'symbol':s,'startTime':state['funding_cursors'][s]+1,'endTime':now,'limit':1000})
    if not isinstance(rows,list):raise ValueError('INVALID_FUNDING_RESPONSE')
    data['funding'][s]=rows
   except Exception as exc:data['errors'].append(s+':FUNDING_'+type(exc).__name__)
   try:
    spot=self.get('spot','/api/v3/ticker/bookTicker',{'symbol':s});perp=self.get('perp','/fapi/v1/ticker/bookTicker',{'symbol':s});mark=self.get('perp','/fapi/v1/premiumIndex',{'symbol':s})
    data['quotes'][s]={'spot_bid':float(spot['bidPrice']),'spot_ask':float(spot['askPrice']),
      'perp_bid':float(perp['bidPrice']),'perp_ask':float(perp['askPrice']),'mark':float(mark['markPrice']),'received_ms':int(time.time()*1000)}
    if need_day:
     bars=self.get('spot','/api/v3/klines',{'symbol':s,'interval':'1d','startTime':day-DAY,'endTime':day-1,'limit':1})
     if len(bars)!=1:raise ValueError('MISSING_CLOSED_DAY')
     data['previous_day_close'][s]={'price':float(bars[0][4]),'close_ms':int(bars[0][6])}
   except Exception as exc:data['errors'].append(s+':QUOTE_'+type(exc).__name__)
  return data

def connect(directory,now):
 directory.mkdir(parents=True,exist_ok=True)
 c=sqlite3.connect(directory/'funding_carry.sqlite3',timeout=.5);c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=FULL')
 c.executescript('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,payload TEXT);CREATE TABLE IF NOT EXISTS payments(symbol TEXT,at_ms INTEGER,payload TEXT,PRIMARY KEY(symbol,at_ms));CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,at_ms INTEGER,payload TEXT);')
 row=c.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
 if row is None:
  manifest={'rule':RULE,'registered_ms':now,'code_sha256':code_hashes()}
  with c:
   c.execute('INSERT INTO meta VALUES(?,?)',('manifest',canonical(manifest)));c.execute('INSERT INTO state VALUES(1,?)',(canonical(initial_state(now)),))
 else:
  manifest=json.loads(row[0])
  if manifest['rule']!=RULE or manifest['code_sha256']!=code_hashes():c.close();raise ValueError('FROZEN_COHORT_CHANGED')
 return c,manifest

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--data-dir',type=Path,required=True);ap.add_argument('--filters',type=Path,required=True);args=ap.parse_args()
 now=int(time.time()*1000);c,manifest=connect(args.data_dir,now)
 try:
  c.execute('BEGIN IMMEDIATE');saved=json.loads(c.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
  filters=json.loads(args.filters.read_text())
  if filters!=FILTERS:raise ValueError('FROZEN_FILTER_CONFIGURATION_CHANGED')
  payload=PublicClient().inputs(saved,filters,now)
  current=int(time.time()*1000);state,payments,orders,status=advance(saved,current,payload)
  c.execute('UPDATE state SET payload=? WHERE id=1',(canonical(state),))
  for payment in payments:c.execute('INSERT INTO payments VALUES(?,?,?)',(payment['symbol'],payment['at_ms'],canonical(payment)))
  for order in orders:c.execute('INSERT INTO orders(at_ms,payload) VALUES(?,?)',(order['at_ms'],canonical(order)))
  c.commit()
  path=args.data_dir/'status.json';temp=path.with_name('status.json.tmp');temp.write_text(canonical(status),encoding='utf-8');temp.replace(path)
  print(canonical({'health':status['health'],'orders':status['counts']['orders'],'funding_payments':status['counts']['funding_payments'],'next_execution_ms':status['next_execution_ms']}))
 except Exception:
  c.rollback();raise
 finally:c.close()
if __name__=='__main__':main()
