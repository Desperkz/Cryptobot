"""Isolated public-GET shadow lab. Never imports TradingBot or OrderManager.

No source DB is opened. Only this cohort's directory is writable. Rule versions
and evidence are immutable; tuning requires a new cohort/data directory.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import zlib

from trading_bot.config import load_config
from trading_bot.data_provider.binance_usdm import BinanceUSDMClient
from trading_bot.market_regime_detector import MarketRegimeDetector
from trading_bot.models import Candle, MarketMetrics
from trading_bot.strategy_engine.candidate_strategies import LiquiditySweepReversalStrategy, VwapReversionStrategy, RangeGridStrategy, MomentumContinuationStrategy
from trading_bot.strategy_engine.mean_reversion import MeanReversionStrategy
from trading_bot.strategy_engine.edge import EdgeAnalyzer
from trading_bot.strategy_engine.diversified import DEFAULTS, ResearchPlan, evaluate, parameters

VENUE='https://fapi.binance.com'
PATHS=frozenset({'/fapi/v1/klines','/fapi/v1/ticker/bookTicker','/fapi/v1/depth','/fapi/v1/aggTrades','/fapi/v1/fundingRate'})
MINUTE=60000
BASELINES=('MEAN_REVERSION','VWAP_REVERSION','RANGE_GRID','LIQUIDITY_SWEEP_REVERSAL','MOMENTUM_CONTINUATION')

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False,default=str)
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

class PublicClient(BinanceUSDMClient):
    def __init__(self):super().__init__(VENUE,timeout_sec=8,max_retries=1)
    async def _request(self,method,path,params=None,signed=False):
        if method!='GET' or signed or path not in PATHS or self.base_url!=VENUE or self.api_key is not None or self.api_secret is not None:
            raise PermissionError('Only credential-free allowlisted public market GETs are allowed')
        return await super()._request(method,path,params,signed=False)

def validate_settings(settings):
    required={'cohort','symbols','risk_usdt','max_open','max_store_mb','max_drift_r','min_net_rr','scan_interval_sec','parameters'}
    if set(settings)!=required or not isinstance(settings['cohort'],str) or not settings['cohort']:
        raise ValueError('Invalid cohort settings schema')
    symbols=settings['symbols']
    if not isinstance(symbols,list) or not 1<=len(symbols)<=8 or len(set(symbols))!=len(symbols) or any(not s.isalnum() or not s.endswith('USDT') for s in symbols):
        raise ValueError('Invalid fixed research universe')
    for key in required-{'cohort','symbols','parameters'}:
        x=settings[key]
        if isinstance(x,bool) or not isinstance(x,(float,int)) or not math.isfinite(x) or x<=0:raise ValueError('Invalid '+key)
    if settings['risk_usdt']>2 or settings['max_open']>24 or settings['scan_interval_sec']<300 or settings['max_store_mb']>512 or settings['max_drift_r']>.35 or settings['min_net_rr']<.5:
        raise ValueError('Research safety limits exceeded')
    if any(int(settings[k])!=settings[k] for k in ('max_open','max_store_mb','scan_interval_sec')):raise ValueError('Counts and intervals must be integers')
    if set(settings['parameters'])-DEFAULTS.keys():raise ValueError('Unknown rule')
    for name in DEFAULTS:parameters(name,settings['parameters'].get(name))

def code_hashes(code_root):
    files=list((code_root/'src').rglob('*.py'))+[code_root/'config.yaml',code_root/'settings.json']
    return {str(p.relative_to(code_root)):sha(p) for p in files}

def connect(directory,settings,code_root,now):
    validate_settings(settings);directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    db=sqlite3.connect(directory/'shadow.sqlite3',timeout=3);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA busy_timeout=3000')
    db.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, symbol TEXT, bar_ms INTEGER, observed_ms INTEGER, raw BLOB, UNIQUE(symbol,bar_ms));
    CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, snapshot_id INTEGER, strategy TEXT, symbol TEXT, bar_ms INTEGER, reason TEXT, detail TEXT, UNIQUE(strategy,symbol,bar_ms));
    CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY, strategy TEXT, symbol TEXT, direction TEXT, opened_ms INTEGER, entry_mid REAL, entry REAL, stop REAL, target REAL, quantity REAL, risk_usdt REAL, expires_ms INTEGER, next_ms INTEGER, snapshot_id INTEGER, status TEXT, last_ms INTEGER, closed_ms INTEGER, exit_price REAL, net_usdt REAL, reason TEXT, costs TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS one_open ON positions(strategy,symbol) WHERE status='OPEN';''')
    hashes=code_hashes(code_root);row=db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
    if row:
        manifest=json.loads(row[0])
        if manifest['settings']!=settings or manifest['code_sha256']!=hashes:
            db.close();raise ValueError('Frozen cohort changed; create a new cohort directory')
    else:
        manifest={'registered_ms':now,'cohort':settings['cohort'],'settings':settings,'code_sha256':hashes,'venue':VENUE,'mode':'SHADOW_ONLY','order_capability':False,'auto_promotion':False,
            'cost_model':'4bps each-side fee, 5bps adverse entry and exit slippage, adverse 1bp/8h funding buffer; gap losses may exceed budget',
            'execution':'Fresh observed book midpoint; exits use contiguous closed 1m bars starting at first FULL minute after entry; stop first if ambiguous; timeout at closed bar',
            'comparison':'Native baseline signal rules plus separately named V2 hypotheses; common lab execution, not a replay of the main bot admission/partial-exit pipeline',
            'scope':'Correlated virtual observations, not an additive portfolio; no historical entries admitted'}
        db.execute('INSERT INTO meta VALUES(?,?)',('manifest',canonical(manifest)));db.commit()
    return db,manifest

def fill_plan(plan,mid,now,settings):
    sign=1 if plan.direction=='LONG' else -1
    distance=abs(plan.entry-plan.stop)
    if not all(math.isfinite(x) and x>0 for x in (mid,plan.entry,plan.stop,plan.target)) or distance<=0:return None,'INVALID_PRICE'
    if abs(mid-plan.entry)/distance>settings['max_drift_r']:return None,'ENTRY_DRIFT'
    entry=mid*(1+sign*.0005)
    stop_distance=sign*(entry-plan.stop);reward=sign*(plan.target-entry)
    if min(stop_distance,reward)<=0:return None,'PRICE_CROSSED_STOP_OR_TARGET'
    funding=entry*.0001*plan.horizon_hours/8
    stop_effective=plan.stop*(1-sign*.0005)
    loss_per_unit=sign*(entry-stop_effective)+(entry+stop_effective)*.0004+funding
    target_effective=plan.target*(1-sign*.0005)
    gain_per_unit=sign*(target_effective-entry)-(entry+target_effective)*.0004-funding
    if gain_per_unit/loss_per_unit<settings['min_net_rr']:return None,'INSUFFICIENT_NET_TARGET_SPACE'
    quantity=settings['risk_usdt']/loss_per_unit
    return {'strategy':plan.strategy,'symbol':plan.symbol,'direction':plan.direction,'opened_ms':now,'entry_mid':mid,'entry':entry,'stop':plan.stop,'target':plan.target,'quantity':quantity,'risk_usdt':settings['risk_usdt'],'expires_ms':now+plan.horizon_hours*3600000,'next_ms':(now//MINUTE+1)*MINUTE,'status':'OPEN'},None

def settle(position,price,when,reason):
    sign=1 if position['direction']=='LONG' else -1
    effective=price*(1-sign*.0005);qty=position['quantity'];entry=position['entry']
    fees=(entry+effective)*qty*.0004
    funding=entry*qty*.0001*max(0,when-position['opened_ms'])/28800000
    gross=sign*(effective-entry)*qty
    costs={'fees':fees,'funding':funding,'funding_source':'adverse_1bp_per8h_buffer','entry_slippage':abs(entry-position['entry_mid'])*qty,'exit_slippage':abs(price-effective)*qty,'gross_before_exit_slippage':sign*(price-entry)*qty}
    return {'closed_ms':when,'exit_price':effective,'net_usdt':gross-fees-funding,'reason':reason,'costs':canonical(costs)}

def advance(position,bars,*,interval_ms=MINUTE):
    """Require contiguous bars. Exclude the partially observed entry minute."""
    if interval_ms not in (MINUTE,900000):raise ValueError('Unsupported closed-bar interval')
    cursor=position['next_ms'];sign=1 if position['direction']=='LONG' else -1
    for b in bars:
        if int(b[0])!=cursor:raise ValueError('MISSING_MINUTE: refusing to bridge a data gap')
        o,h,l,close=map(float,b[1:5]);when=int(b[6])
        hit_stop=l<=position['stop'] if sign==1 else h>=position['stop']
        hit_target=h>=position['target'] if sign==1 else l<=position['target']
        if hit_stop:
            price=min(o,position['stop']) if sign==1 else max(o,position['stop'])
            return settle(position,price,when,'STOP_FIRST_AMBIGUOUS' if hit_target else 'STOP'),cursor+interval_ms
        if hit_target:return settle(position,position['target'],when,'TARGET'),cursor+interval_ms
        if when>=position['expires_ms']:return settle(position,close,when,'TIMEOUT'),cursor+interval_ms
        cursor+=interval_ms
    return None,cursor

async def monitor(db,client,now):
    positions=[dict(r) for r in db.execute("SELECT * FROM positions WHERE status='OPEN' ORDER BY id")]
    for symbol in sorted({r['symbol'] for r in positions}):
        group=[r for r in positions if r['symbol']==symbol];start=min(r['next_ms'] for r in group)
        end=now//MINUTE*MINUTE-1
        if start>end:continue
        bars=await client.klines(symbol,'1m',start_time=start,end_time=end,limit=1000)
        bars=[b for b in bars if int(b[6])<=end]
        for p in group:
            outcome,cursor=advance(p,[b for b in bars if int(b[0])>=p['next_ms']])
            if outcome:
                db.execute("UPDATE positions SET status='CLOSED',closed_ms=?,exit_price=?,net_usdt=?,reason=?,costs=?,last_ms=?,next_ms=? WHERE id=?",(*outcome.values(),cursor-1,cursor,p['id']))
            else:db.execute('UPDATE positions SET next_ms=?,last_ms=? WHERE id=?',(cursor,cursor-1,p['id']))
        db.commit()

def baseline_rules(config):
    detector=MarketRegimeDetector(config.strategy);edge=EdgeAnalyzer(config.edge_filters)
    return {'MEAN_REVERSION':MeanReversionStrategy(config.strategy,detector,edge),'VWAP_REVERSION':VwapReversionStrategy(config.strategy),
        'RANGE_GRID':RangeGridStrategy(config.strategy,detector),'LIQUIDITY_SWEEP_REVERSAL':LiquiditySweepReversalStrategy(config.strategy,edge),
        'MOMENTUM_CONTINUATION':MomentumContinuationStrategy(config.strategy,detector,edge)}

async def snapshot(client,symbol,now):
    raw={'requested_ms':{}}
    for interval in ('15m','1h','4h'):
        raw['requested_ms'][interval]=int(time.time()*1000)
        raw[interval]=await client.klines(symbol,interval,limit=300)
    raw['requested_ms']['book']=int(time.time()*1000)
    raw['book']=await client.book_ticker(symbol)
    raw['depth']=await client.depth(symbol,limit=5)
    raw['agg']=await client.agg_trades(symbol,limit=500)
    raw['observed_ms']=int(time.time()*1000)
    return raw

def decode(raw,symbol,now):
    requested=raw.get('requested_ms',{})
    c15=[Candle.from_binance(b) for b in raw['15m'] if int(b[6])<=min(now,requested.get('15m',now))]
    if not c15:raise ValueError('No closed 15m candles')
    stamp=c15[-1].close_time
    c1=[Candle.from_binance(b) for b in raw['1h'] if int(b[6])<=min(stamp,requested.get('1h',now))]
    c4=[Candle.from_binance(b) for b in raw['4h'] if int(b[6])<=min(stamp,requested.get('4h',now))]
    for candles,interval in ((c15,900000),(c1,3600000),(c4,14400000)):
        if len(candles)<205 or any(b.open_time-a.open_time!=interval for a,b in zip(candles[-205:],candles[-204:])) or stamp-candles[-1].close_time>interval:
            raise ValueError('Incomplete or stale closed candle window')
    if now-requested.get('book',now)>10000:raise ValueError('Book snapshot is stale')
    bid,ask=float(raw['book']['bidPrice']),float(raw['book']['askPrice'])
    if not (math.isfinite(bid) and math.isfinite(ask) and 0<bid<=ask):raise ValueError('Invalid book')
    mid=(bid+ask)/2;depth=raw['depth']
    bids=sum(float(p)*float(q) for p,q in depth.get('bids',[])[:5]);asks=sum(float(p)*float(q) for p,q in depth.get('asks',[])[:5])
    trades=raw['agg'];total=sum(float(t['q']) for t in trades);buys=sum(float(t['q']) for t in trades if not t['m']);ratio=buys/total if total else .5
    metrics=MarketMetrics(symbol,Decimal('0'),Decimal(str((ask-bid)/mid*10000)),Decimal(str(bids+asks)),order_book_imbalance=Decimal(str((bids-asks)/(bids+asks) if bids+asks else 0)),taker_buy_ratio=Decimal(str(ratio)),aggressive_buy_sell_delta=Decimal(str(2*ratio-1)))
    return c15,c1,c4,metrics,mid

def admit(db,plan,mid,now,settings,snapshot_id):
    if plan.strategy=='SESSION_RANGE_BREAKOUT_V1':
        day=plan.bar_close_ms//86400000*86400000
        if db.execute('SELECT 1 FROM positions WHERE strategy=? AND symbol=? AND direction=? AND opened_ms>=?',(plan.strategy,plan.symbol,plan.direction,day)).fetchone():return 'SESSION_ALREADY_TAKEN'
    if db.execute("SELECT 1 FROM positions WHERE strategy=? AND symbol=? AND status='OPEN'",(plan.strategy,plan.symbol)).fetchone():return 'POSITION_ALREADY_OPEN'
    last=db.execute('SELECT max(closed_ms) FROM positions WHERE strategy=? AND symbol=?',(plan.strategy,plan.symbol)).fetchone()[0]
    if last and now-last<2700000:return 'COOLDOWN_45M'
    if db.execute("SELECT count(*) FROM positions WHERE status='OPEN'").fetchone()[0]>=settings['max_open']:return 'OPEN_LIMIT'
    fill,reason=fill_plan(plan,mid,now,settings)
    if reason:return reason
    fill['snapshot_id']=snapshot_id
    db.execute('INSERT INTO positions('+','.join(fill)+') VALUES('+','.join('?' for _ in fill)+')',tuple(fill.values()))
    return 'OPENED'

async def scan(db,client,manifest,settings,config,now):
    controls=baseline_rules(config);errors=[]
    for symbol in settings['symbols']:
        try:
            raw=await snapshot(client,symbol,now);observed=raw['observed_ms']
            c15,c1,c4,metrics,mid=decode(raw,symbol,observed);bar=c15[-1].close_time
            if db.execute('SELECT 1 FROM snapshots WHERE symbol=? AND bar_ms=?',(symbol,bar)).fetchone():continue
            # Require a future bar since registration; never backfill prospective entries.
            if bar<=manifest['registered_ms'] or observed-bar>420000:continue
            snap_id=db.execute('INSERT INTO snapshots(symbol,bar_ms,observed_ms,raw) VALUES(?,?,?,?)',(symbol,bar,observed,zlib.compress(canonical(raw).encode()))).lastrowid
            for name in list(BASELINES)+list(DEFAULTS):
                if name in controls:
                    signal=controls[name].generate(symbol,c15,c1,c4,metrics)
                    plan=ResearchPlan(name+'_CONTROL',symbol,signal.direction.value,bar,float(signal.entry_price),float(signal.stop_loss),float(signal.take_profit),8) if signal is not None else None
                    detail={'reason':'SIGNAL' if plan else 'NO_SIGNAL','baseline':True}
                else:plan,detail=evaluate(name,symbol,c15,c1,c4,overrides=settings['parameters'].get(name))
                reason=detail['reason']
                if plan:
                    reason='SPREAD_LIMIT' if metrics.spread_bps>8 else admit(db,plan,mid,observed,settings,snap_id)
                db.execute('INSERT INTO observations(snapshot_id,strategy,symbol,bar_ms,reason,detail) VALUES(?,?,?,?,?,?)',(snap_id,plan.strategy if plan else name+('_CONTROL' if name in controls else ''),symbol,bar,reason,canonical(detail)))
            db.commit()
        except Exception as exc:
            db.rollback();errors.append(symbol+': '+str(exc)[:250])
    return errors

def status(db,manifest,directory,errors):
    now=int(time.time()*1000);groups=[]
    for name in [x+'_CONTROL' for x in BASELINES]+list(DEFAULTS):
        rows=[dict(r) for r in db.execute('SELECT status,net_usdt,risk_usdt FROM positions WHERE strategy=?',(name,))]
        closed=[r for r in rows if r['status']=='CLOSED'];wins=[r['net_usdt'] for r in closed if r['net_usdt']>0];losses=[r['net_usdt'] for r in closed if r['net_usdt']<0]
        groups.append({'strategy':name,'control':name.endswith('_CONTROL'),'entries':len(rows),'closed':len(closed),'open':len(rows)-len(closed),'net_usdt':sum(r['net_usdt'] for r in closed) if closed else None,'avg_r':sum(r['net_usdt']/r['risk_usdt'] for r in closed)/len(closed) if closed else None,'profit_factor':sum(wins)/-sum(losses) if losses else None})
    reasons=[dict(r) for r in db.execute('SELECT strategy,reason,count(*) n FROM observations GROUP BY strategy,reason ORDER BY strategy,n DESC')]
    active=[dict(r) for r in db.execute("SELECT id,strategy,symbol,direction,opened_ms,entry,stop,target,next_ms,last_ms FROM positions WHERE status='OPEN' ORDER BY id LIMIT 24")]
    recent=[dict(r) for r in db.execute("SELECT id,strategy,symbol,direction,opened_ms,closed_ms,net_usdt,reason FROM positions WHERE status='CLOSED' ORDER BY id DESC LIMIT 30")]
    result={'generated_at_ms':now,'health':'DEGRADED' if errors else 'OK','mode':'SHADOW_ONLY','order_capability':False,'auto_promotion':False,'cohort':manifest['cohort'],'registered_ms':manifest['registered_ms'],'venue':VENUE,'combined_pnl':None,'combined_pnl_reason':'CORRELATED_VIRTUAL_OBSERVATIONS','groups':groups,'diagnostics':reasons,'open_positions':active,'recent_positions':recent,'errors':errors,'protocol':manifest,'cycle_interval_sec':manifest['settings']['scan_interval_sec']}
    tmp=directory/'.status.tmp';tmp.write_text(canonical(result));os.replace(tmp,directory/'status.json')
    return result

async def cycle(directory,code_root,settings,*,init_only=False):
    now=int(time.time()*1000);db,manifest=connect(directory,settings,code_root,now)
    client=PublicClient();errors=[]
    try:
        if not init_only:
            try:await monitor(db,client,now)
            except Exception as exc:
                db.rollback();errors.append('MONITOR: '+str(exc)[:250])
            if db.execute("SELECT 1 FROM positions WHERE status='OPEN' AND next_ms<? LIMIT 1",(now-900000,)).fetchone():
                errors.append('MINUTE_BACKLOG: admissions paused until exits catch up')
            size=sum(p.stat().st_size for p in directory.glob('shadow.sqlite3*'))
            if size>settings['max_store_mb']*1024**2:errors.append('STORAGE_LIMIT: admissions paused, exits continue')
            elif not errors:
                config=load_config(code_root/'config.yaml',code_root/'NO_ENV_FILE')
                errors+=await scan(db,client,manifest,settings,config,now)
        result=status(db,manifest,directory,errors)
        print(canonical({'health':result['health'],'errors':errors,'open':len(result['open_positions']),'entries':sum(g['entries'] for g in result['groups'])}))
        return result
    finally:db.close();await client.close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',type=Path,required=True);parser.add_argument('--code-root',type=Path,required=True);parser.add_argument('--init-only',action='store_true');args=parser.parse_args()
    if args.data_dir.resolve()==args.code_root.resolve() or args.data_dir.resolve().name!='data':raise ValueError('Use an isolated cohort data directory')
    settings=json.loads((args.code_root/'settings.json').read_text())
    asyncio.run(cycle(args.data_dir,args.code_root,settings,init_only=args.init_only))

if __name__=='__main__':main()
