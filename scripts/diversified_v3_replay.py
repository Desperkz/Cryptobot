"""Fixed V2/V3 common 1m cost-stress replay; no fitting or exchange access."""
import argparse,collections,gzip,hashlib,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from trading_bot.models import Candle
from trading_bot.strategy_engine.diversified import DEFAULTS,evaluate as old
from trading_bot.strategy_engine.diversified_v3 import RULES,evaluate as revised,expense_fraction,MAX_EXPENSE_FRACTION,validate_series
from trading_bot.research.strategy_replay import fill,execute,statistics

p=argparse.ArgumentParser();p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--start-ms',type=int,default=0);args=p.parse_args()
raw=json.loads(gzip.decompress((args.data_dir/'raw.json.gz').read_bytes()))
settings={'risk_usdt':2.,'max_drift_r':.35,'min_net_rr':.8};trades=[];diagnostics=collections.defaultdict(collections.Counter)
for symbol,data in raw['symbols'].items():
    c15=[Candle.from_binance(b) for b in data['15m'] if int(b[6])<=raw['at_ms']]
    c1=[Candle.from_binance(b) for b in data['1h']];c4=[Candle.from_binance(b) for b in data['4h']]
    for candles,interval in ((c15,900000),(c1,3600000),(c4,14400000)):
        problem=validate_series(candles,interval)
        if problem:raise ValueError(problem)
    minutes=json.loads(gzip.decompress((args.data_dir/(symbol+'_1m.json.gz')).read_bytes()))
    higher={}
    for name in list(DEFAULTS)+list(RULES):
        cache={}
        for bps in (5,10):
            busy=0;sessions=set()
            for i in range(205,len(c15)-1):
                stamp=c15[i].close_time
                if stamp<max(raw.get('sample_start_ms',0),args.start_ms) or stamp<busy:continue
                if i not in cache:
                    if i not in higher:higher[i]=([c for c in c1 if c.close_time<=stamp][-300:],[c for c in c4 if c.close_time<=stamp][-300:])
                    h1,h4=higher[i];window=c15[max(0,i-299):i+1]
                    cache[i]=(old(name,symbol,window,h1,h4) if name in DEFAULTS else revised(name,symbol,window,h1,h4,_validated=True))
                plan,diag=cache[i];key=name+':'+str(bps);diagnostics[key][diag['reason']]+=1
                if plan is None:continue
                day=stamp//86400000
                session=name in ('SESSION_RANGE_BREAKOUT_V1','SESSION_RANGE_BREAKOUT_V2')
                if session and (day,plan.direction) in sessions:continue
                entry=c15[i+1].open_time;offset=(entry-int(minutes[0][0]))//60000
                if offset<0 or offset>=len(minutes) or int(minutes[offset][0])!=entry:raise ValueError('MISSING_ENTRY_MINUTE')
                position,reason=fill(plan,float(c15[i+1].open),entry,settings,bps)
                if reason:diagnostics[key][reason]+=1;continue
                if name in RULES and expense_fraction(position,bps)>MAX_EXPENSE_FRACTION:
                    diagnostics[key]['EXPENSE_BUDGET_LIMIT']+=1;continue
                position['next_ms']=entry
                outcome=execute(position,minutes[offset:offset+plan.horizon_hours*60+1],bps)
                row={**position,'bps':bps,'signal_bar_ms':stamp,'status':'CLOSED' if outcome else 'OPEN'}
                if outcome:row.update(outcome);busy=outcome['closed_ms']+2700000
                else:busy=raw['at_ms']+1
                trades.append(row)
                if session:sessions.add((day,plan.direction))
        print(symbol,name,'complete',flush=True)
groups={name:{str(bps):statistics([r for r in trades if r['strategy']==name and r['bps']==bps]) for bps in (5,10)} for name in list(DEFAULTS)+list(RULES)}
report={'at_ms':raw['at_ms'],'sample_start_ms':max(raw.get('sample_start_ms',0),args.start_ms),
 'rules':RULES,'groups':groups,'trades':trades,'diagnostics':{k:dict(v) for k,v in diagnostics.items()},
 'method':{'costs':'Original 4bps each-side fee, 5/10bps adverse slip, 1bp/8h adverse funding buffer; full stop-plus-expenses risk 2 USDT',
    'entry':'Next 15m open, closed inputs, no source-feed publication delay in retrospective replay',
    'execution':'Contiguous closed 1m, stop first if ambiguous, adverse gap stops, 45min cooldown, one session entry per direction/day',
    'scope':'Fixed-rule exploratory comparison. Not proof of profit or an independent held-out test. No parameter search. Source-feed freshness and real quotes are evaluated prospectively.'},
 'source_sha256':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in [args.data_dir/'raw.json.gz',*sorted(args.data_dir.glob('*_1m.json.gz'))]},
 'rule_sha256':hashlib.sha256(Path(__file__).parents[1].joinpath('src/trading_bot/strategy_engine/diversified_v3.py').read_bytes()).hexdigest()}
args.output.write_text(json.dumps(report,indent=2,allow_nan=False))
print(json.dumps({'groups':groups}))
