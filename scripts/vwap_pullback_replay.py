"""Compare new VWAP pullback entries to V2 on the same saved minute dataset."""
import argparse,collections,gzip,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from trading_bot.models import Candle
from trading_bot.strategy_engine.diversified import evaluate as reversion
from trading_bot.strategy_engine.vwap_pullback import evaluate as pullback,VERSION,RULE
from trading_bot.research.strategy_replay import fill,execute,statistics

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--data-dir',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
raw=json.loads(gzip.decompress((args.data_dir/'raw.json.gz').read_bytes()))
settings={'risk_usdt':2.,'max_drift_r':.35,'min_net_rr':.8}
trades=[];diagnostics=collections.defaultdict(collections.Counter)
for symbol,data in raw['symbols'].items():
    c15=[Candle.from_binance(b) for b in data['15m'] if int(b[6])<=raw['at_ms']]
    c1=[Candle.from_binance(b) for b in data['1h']];c4=[Candle.from_binance(b) for b in data['4h']]
    minutes=json.loads(gzip.decompress((args.data_dir/(symbol+'_1m.json.gz')).read_bytes()))
    for name in ['VWAP_REVERSION_V2',VERSION]:
        cached={}
        for bps in [5,10]:
            busy=0
            for i in range(205,len(c15)-1):
                stamp=c15[i].close_time
                if stamp<raw.get('sample_start_ms',0):continue
                if stamp<busy:continue
                if i not in cached:
                    h1=[c for c in c1 if c.close_time<=stamp][-300:]
                    h4=[c for c in c4 if c.close_time<=stamp][-300:]
                    window=c15[max(0,i-299):i+1]
                    cached[i]=reversion(name,symbol,window,h1,h4) if name=='VWAP_REVERSION_V2' else pullback(symbol,window,h1,h4)
                plan,diag=cached[i]
                key=name+':'+str(bps)
                diagnostics[key][diag['reason']]+=1
                if plan is None:continue
                entry=c15[i+1].open_time
                if entry<int(minutes[0][0]) or entry>int(minutes[-1][0]):
                    raise ValueError('ENTRY_OUTSIDE_MINUTE_COVERAGE')
                position,reason=fill(plan,float(c15[i+1].open),entry,settings,bps)
                if reason:
                    diagnostics[key][reason]+=1;continue
                position['next_ms']=entry
                offset=(entry-int(minutes[0][0]))//60000
                bars=minutes[offset:offset+plan.horizon_hours*60+1]
                outcome=execute(position,bars,bps)
                row={**position,'bps':bps,'signal_bar_ms':stamp,'diagnostic':diag,
                    'status':'CLOSED' if outcome else 'OPEN'}
                if outcome:
                    row.update(outcome);busy=outcome['closed_ms']+2700000
                else:busy=raw['at_ms']+1
                trades.append(row)
        print(symbol,name,'replayed',flush=True)
groups={name:{str(bps):statistics([r for r in trades if r['strategy']==name and r['bps']==bps])
    for bps in [5,10]} for name in ['VWAP_REVERSION_V2',VERSION]}
candidate=groups[VERSION];baseline=groups['VWAP_REVERSION_V2']['5']
# A small shadow launch needs some activity and cost robustness, not proof of edge.
checks={'min_10_closed':candidate['5']['closed']>=10,
    'min_3_symbols':len(candidate['5']['symbols'])>=3,
    'positive_after_costs':candidate['5']['net_usdt']>0,
    'nonnegative_10bps':candidate['10']['net_usdt']>=0 and candidate['10']['closed']>=5,
    'better_avg_r':candidate['5']['avg_r'] is not None and candidate['5']['avg_r']>baseline['avg_r'],
    'not_worse_closed_drawdown':candidate['5']['max_closed_drawdown_usdt']>=baseline['max_closed_drawdown_usdt']}
report={'at_ms':raw['at_ms'],'rule':RULE,'groups':groups,'trades':trades,
    'replay_settings':{**settings,'sample_start_ms':raw.get('sample_start_ms'),
        'entry':'next 15m open after signal; identical prices for both strategies',
        'exit':'contiguous closed 1m; stop first when ambiguous; adverse stop gaps; eight-hour timeout',
        'costs':'4bps per side, adverse 1bp/8h funding, separately recomputed 5/10bps slippage',
        'closed_drawdown':'sum simultaneous exit-minute events; no invented intraminute equity ordering'},
    'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [args.data_dir/'raw.json.gz',*sorted(args.data_dir.glob('*_1m.json.gz'))]},
    'diagnostics':{k:dict(v) for k,v in diagnostics.items()},'shadow_launch_checks':checks,
    'shadow_launch_supported':all(checks.values()),
    'code_sha256':hashlib.sha256(Path(__file__).parents[1].joinpath('src/trading_bot/strategy_engine/vwap_pullback.py').read_bytes()).hexdigest(),
    'scope':'Exploratory independent entries on identical saved bars. No parameter sweep, no portfolio addition. Closed drawdown only. No historical bid/ask fills or claim of held-out validation.'}
args.output.write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
print(json.dumps({'groups':groups,'shadow_launch_checks':checks},indent=2))
