"""Evaluate previously declared V4 gates; never fit thresholds or alter a feed."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from trading_bot.research import diversified_v4_gates as g
from trading_bot.research import diversified_v4_common as c

FROZEN_DECLARATION_SHA256='6ec3fe45d1a60b928a5df3b250db07c2903db2e5ec8df52fd263253fd81f449b'
FROZEN_FEATURES_SHA256='d51f41f23e6145d11f38c222063570647f60dacb3745a2c400e5e3f771db0234'


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stats(rows, key='net_usdt'):
    nets=[r[key] for r in rows]
    gain=sum(x for x in nets if x>0);loss=-sum(x for x in nets if x<0)
    return {'closed':len(rows),'net_usdt':sum(nets),
            'average_r':sum(nets)/len(nets)/2 if nets else None,
            'wins':sum(x>0 for x in nets),'losses':sum(x<0 for x in nets),
            'profit_factor':gain/loss if loss else None,
            'gross_wins':gain,'gross_losses':loss,
            'utc_days':len({datetime.fromtimestamp(r['opened_ms']/1000,timezone.utc).date().isoformat() for r in rows})}


def review(declaration_path,features_path,outcomes_path):
    if sha(declaration_path)!=FROZEN_DECLARATION_SHA256:raise ValueError('FROZEN_DECLARATION_CHANGED')
    if sha(features_path)!=FROZEN_FEATURES_SHA256:raise ValueError('FROZEN_FEATURE_EXPORT_CHANGED')
    declaration=load(declaration_path);features=load(features_path);outcomes=load(outcomes_path)
    if (declaration['gate_code_sha256']!=sha(g.__file__)
            or features['gate_code_sha256']!=declaration['gate_code_sha256']
            or declaration['source_manifest_sha256']!=c.SOURCE_HASH
            or features['source_manifest_sha256']!=c.SOURCE_HASH
            or declaration['variants']!=g.RULES or declaration['historical_checks']!=g.HISTORICAL_CHECKS
            or declaration['before_results'] is not True
            or features['source_outcome_reads_denied_by_sqlite_authorizer'] is not True):
        raise ValueError('FROZEN_DECLARATION_MISMATCH')
    if len(features['rows'])!=features['plans'] or features['unknown']:
        raise ValueError('INCOMPLETE_INPUT_FEATURE_EXPORT')
    source={r['id']:r for r in outcomes['v3_positions']}
    if len(source)!=len(outcomes['v3_positions']):raise ValueError('DUPLICATE_SOURCE_ID')
    joined=[]
    for item in features['rows']:
        plan=item['plan'];row=source[item['id']]
        if any(plan[k]!=row[k] for k in c.PLAN_FIELDS):raise ValueError('SOURCE_PLAN_CHANGED')
        if g.evaluate(item['features'])!=item['decision']:raise ValueError('INPUT_DECISION_CHANGED')
        diagnostic=json.loads(row['detail'])
        if diagnostic['hourly_trend']!=item['features']['hourly_trend']:
            raise ValueError('ORIGINAL_HOURLY_TREND_PARITY_FAILED')
        c.validate_plan(plan,item['snapshot'],features['at_ms'])
        result=c.validate_control(plan,row,outcomes['at_ms'])
        if result is None:raise ValueError('UNFINISHED_SOURCE_CONTROL')
        side=1 if plan['direction']=='LONG' else -1
        trigger=result['exit_price']/(1-side*.0005)
        stress=c.expected_settlement(plan,trigger,result['closed_ms'],10)
        costs=result['costs'];expenses=sum(costs[k] for k in ('fees','funding','entry_slippage','exit_slippage'))
        joined.append({'source_id':item['id'],'source_strategy':plan['strategy'],
                       'variant':item['features']['variant'],'symbol':plan['symbol'],'direction':plan['direction'],
                       'opened_ms':plan['opened_ms'],'closed_ms':result['closed_ms'],
                       'accepted':item['decision']['accepted'],'reason':item['decision']['reason'],
                       'net_usdt':result['net_usdt'],'expenses_usdt':expenses,
                       'price_before_expenses_usdt':result['net_usdt']+expenses,
                       'stress10_net_usdt':stress['net_usdt'],
                       'close_reason':result['reason'],'features_sha256':item['decision']['features_sha256']})
    variants=[]
    for source_name,name in g.VARIANTS.items():
        control=[r for r in joined if r['source_strategy']==source_name]
        accepted=[r for r in control if r['accepted']];rejected=[r for r in control if not r['accepted']]
        # Calendar split fixed by the source interval, not an optimized trade-count split.
        start=min(r['opened_ms'] for r in control);end=max(r['opened_ms'] for r in control);mid=(start+end)//2
        halves=[stats([r for r in accepted if r['opened_ms']<=mid]),stats([r for r in accepted if r['opened_ms']>mid])]
        a=stats(accepted);b=stats(control);stress=stats(accepted,'stress10_net_usdt')
        without_best=a['net_usdt']-max((r['net_usdt'] for r in accepted),default=0)
        retained=len(accepted)/len(control);delta=a['net_usdt']-b['net_usdt']
        checks={'min_accepted_closed':len(accepted)>=g.HISTORICAL_CHECKS['min_accepted_closed'],
                'min_retained_fraction':retained>=g.HISTORICAL_CHECKS['min_retained_fraction'],
                'min_accepted_utc_days':a['utc_days']>=g.HISTORICAL_CHECKS['min_accepted_utc_days'],
                'positive_primary_net':a['net_usdt']>0,'positive_paired_delta':delta>0,
                'nonnegative_10bps_net':stress['net_usdt']>=0,
                'nonnegative_both_calendar_halves':all(h['closed']>0 and h['net_usdt']>=0 for h in halves),
                'nonnegative_without_best_trade':bool(accepted) and without_best>=0}
        variants.append({'variant':name,'source_strategy':source_name,'control':b,'accepted':a,
                         'control_expenses_usdt':sum(r['expenses_usdt'] for r in control),
                         'accepted_expenses_usdt':sum(r['expenses_usdt'] for r in accepted),
                         'control_price_before_expenses_usdt':sum(r['price_before_expenses_usdt'] for r in control),
                         'accepted_price_before_expenses_usdt':sum(r['price_before_expenses_usdt'] for r in accepted),
                         'rejected':stats(rejected),'retained_fraction':retained,'paired_delta_usdt':delta,
                         'prevented_losses_usdt':-sum(min(r['net_usdt'],0) for r in rejected),
                         'missed_profit_usdt':sum(max(r['net_usdt'],0) for r in rejected),
                         'missed_profitable_trades':sum(r['net_usdt']>0 for r in rejected),
                         'stress10':stress,'without_best_net_usdt':without_best,
                         'calendar_start_ms':start,'calendar_end_ms':end,'calendar_split_ms':mid,'halves':halves,
                         'checks':checks,'failed_checks':[k for k,v in checks.items() if not v],
                         'historical_launch_checks_passed':all(checks.values()),'paper_promotion':False})
    return {'mode':'EXPLORATORY_PREVIOUSLY_REVIEWED_HISTORY','orders_possible':False,
            'source_manifest_sha256':c.SOURCE_HASH,'source_watermark':features['source_watermark'],
            'source_snapshot_ms':outcomes['at_ms'],'feature_snapshot_ms':features['at_ms'],
            'declaration_sha256':sha(declaration_path),'features_sha256':sha(features_path),
            'outcomes_sha256':sha(outcomes_path),'gate_code_sha256':sha(g.__file__),
            'parity_validated':len(joined),'unknown':0,'original_hourly_trend_parity':len(joined),
            'scope':'Matched V3 source IDs, source capacity retained, no replacement entries. Historical gate checks do not authorize paper promotion.',
            'stress_scope':'Same-ID cost stress: same exit trigger/time; 10bps adverse entry and exit, quantity recalculated for same 2USDT full-stop-and-expense budget.',
            'variants':variants,'rows':joined}


def main():
    p=argparse.ArgumentParser();p.add_argument('--declaration',required=True);p.add_argument('--features',required=True)
    p.add_argument('--outcomes',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    result=review(args.declaration,args.features,args.outcomes)
    Path(args.output).write_bytes((json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8'))
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
