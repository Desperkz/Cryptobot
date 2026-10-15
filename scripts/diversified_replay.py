"""Exploratory rule replay. Input: raw.json.gz and optional SYMBOL_1m.json.gz.

Example: python scripts/diversified_replay.py --data-dir evidence --minute
Raw snapshots must have the public kline structure recorded by the audit.
This does not connect to an exchange or mutate running cohort state.
"""
import argparse,collections,gzip,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from trading_bot.models import Candle
from trading_bot.strategy_engine.diversified import DEFAULTS,evaluate
from trading_bot.research.diversified_shadow import fill_plan,advance

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--data-dir',type=Path,required=True)
parser.add_argument('--minute',action='store_true')
args=parser.parse_args()
out=args.data_dir
raw=json.loads(gzip.decompress((out/'raw.json.gz').read_bytes()))
settings=json.loads((ROOT/'diversified_shadow_settings.json').read_text())
trades=[];rejects=collections.defaultdict(collections.Counter)
minute_mode=args.minute
for symbol,data in raw['symbols'].items():
    raw15=[r for r in data['15m'] if int(r[6])<=raw['at_ms']]
    c15=[Candle.from_binance(r) for r in raw15]
    c1=[Candle.from_binance(r) for r in data['1h']];c4=[Candle.from_binance(r) for r in data['4h']]
    minutes=json.loads(gzip.decompress((out/(symbol+'_1m.json.gz')).read_bytes())) if minute_mode else None
    for name in DEFAULTS:
        busy_until=0;session_days=set()
        for i in range(205,len(c15)-1):
            now=c15[i].close_time
            if now<busy_until:continue
            h1=[c for c in c1 if c.close_time<=now][-300:];h4=[c for c in c4 if c.close_time<=now][-300:]
            plan,diag=evaluate(name,symbol,c15[max(0,i-299):i+1],h1,h4)
            if plan is None:continue
            day=now//86400000
            if name=='SESSION_RANGE_BREAKOUT_V1' and (day,plan.direction) in session_days:continue
            entry_ms=int(raw15[i+1][0]);entry_mid=float(raw15[i+1][1])
            fill,reason=fill_plan(plan,entry_mid,entry_ms,settings)
            if reason:rejects[name][reason]+=1;continue
            fill['next_ms']=entry_ms # Retrospective entry at the known next 15m open.
            execution=minutes[(entry_ms-int(minutes[0][0]))//60000:] if minute_mode else raw15[i+1:]
            outcome,cursor=advance(fill,execution,interval_ms=60000 if minute_mode else 900000)
            row={'strategy':name,'symbol':symbol,'direction':plan.direction,'entry_ms':entry_ms,'risk_usdt':settings['risk_usdt'],'status':'CLOSED' if outcome else 'OPEN'}
            if outcome:row.update(outcome);row['r']=outcome['net_usdt']/settings['risk_usdt'];busy_until=outcome['closed_ms']+2700000
            else:busy_until=raw['at_ms']+1
            trades.append(row);session_days.add((day,plan.direction))
groups={}
for name in DEFAULTS:
    rows=[r for r in trades if r['strategy']==name];closed=[r for r in rows if r['status']=='CLOSED']
    gain=sum(max(r['net_usdt'],0) for r in closed);loss=-sum(min(r['net_usdt'],0) for r in closed)
    groups[name]={'entries':len(rows),'closed':len(closed),'open':len(rows)-len(closed),'net_usdt':sum(r['net_usdt'] for r in closed),'avg_r':sum(r['r'] for r in closed)/len(closed) if closed else None,'profit_factor':gain/loss if loss else None,'wins':sum(r['net_usdt']>0 for r in closed)}
report={'groups':groups,'admission_rejections':dict(rejects),'trades':trades,
    'scope':'Exploratory retrospective '+('1m' if minute_mode else '15m')+' OHLC execution replay, next-15m-open fills, stop-first ordering and conservative lab costs. Not a live-fill replay or independently held-out validation. Open positions excluded from realized profit. No parameter sweep or automatic promotion.'}
(out/('retrospective_1m.json' if minute_mode else 'retrospective_15m.json')).write_text(json.dumps(report,indent=2))
print(json.dumps({'groups':groups,'admission_rejections':dict(rejects)},indent=2))
