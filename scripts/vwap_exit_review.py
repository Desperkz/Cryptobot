"""Two preregistered VWAP exit replays, with exact original-control parity."""
import argparse
from datetime import datetime,timezone
import gzip
import hashlib
import json
import math
from pathlib import Path

from trading_bot.research import diversified_v4_common as c
from trading_bot.research import vwap_exit_execution as e

DECLARATION_SHA256='1d043c78556362db2a12ba54ac9405f2f83a4731bc07ca662a4e858af5dbd93e'
MINUTES_SHA256='967800774b67bbb067258f3e30f91d2ee16fe0c94ffec51cb71b7d71cf1e122c'


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def load(path):return json.loads(Path(path).read_text(encoding='utf-8'))


def parity(replay,original):
    if replay is None:raise ValueError('CONTROL_REPLAY_INCOMPLETE')
    if any(replay[k]!=original[k] for k in ('closed_ms','reason')):raise ValueError('CONTROL_REPLAY_SEQUENCE_MISMATCH')
    for k in ('exit_price','net_usdt'):
        if not math.isclose(replay[k],original[k],rel_tol=1e-10,abs_tol=1e-9):raise ValueError('CONTROL_REPLAY_PNL_MISMATCH')
    a=json.loads(replay['costs']);b=original['costs']
    if set(a)!=set(b):raise ValueError('CONTROL_COST_SCHEMA_MISMATCH')
    for k in a:
        if isinstance(a[k],str):
            if a[k]!=b[k]:raise ValueError('CONTROL_COST_CONTRACT_MISMATCH')
        elif not math.isclose(a[k],b[k],rel_tol=1e-10,abs_tol=1e-9):raise ValueError('CONTROL_REPLAY_COST_MISMATCH')


def stats(rows,key):
    nets=[r[key] for r in rows];wins=sum(max(n,0) for n in nets);losses=-sum(min(n,0) for n in nets)
    return {'closed':len(rows),'net_usdt':sum(nets),'average_r':sum(nets)/len(nets)/2 if nets else None,
            'wins':sum(n>0 for n in nets),'losses':sum(n<0 for n in nets),'profit_factor':wins/losses if losses else None,
            'utc_days':len({datetime.fromtimestamp(r['opened_ms']/1000,timezone.utc).date().isoformat() for r in rows})}


def review(directory,source_directory):
    directory=Path(directory);source_directory=Path(source_directory)
    if sha(directory/'declaration.json')!=DECLARATION_SHA256:raise ValueError('FROZEN_DECLARATION_CHANGED')
    if sha(directory/'minutes.json.gz')!=MINUTES_SHA256:raise ValueError('FROZEN_MINUTES_CHANGED')
    declaration=load(directory/'declaration.json')
    if declaration['rules']!=e.RULES or declaration['checks']!=e.CHECKS:raise ValueError('FROZEN_RULE_CHANGED')
    for name,value in declaration['execution_sha256'].items():
        if sha(Path(e.__file__).with_name(name))!=value:raise ValueError('EXECUTOR_CHANGED: '+name)
    for name,value in declaration['source_inputs_sha256'].items():
        if sha(source_directory/name)!=value:raise ValueError('SOURCE_INPUT_CHANGED: '+name)
    feature_rows=load(source_directory/'historical_features.json')['rows']
    by_id={r['id']:r for r in feature_rows}
    source=load(source_directory/'source_outcomes.json');outcomes={r['id']:r for r in source['v3_positions']}
    minutes=json.loads(gzip.decompress((directory/'minutes.json.gz').read_bytes()))
    if minutes['declaration_sha256']!=DECLARATION_SHA256:raise ValueError('MINUTE_DECLARATION_MISMATCH')
    windows={r['source_id']:r for r in minutes['windows']}
    ids=declaration['fixed_source_ids']
    if len(windows)!=len(minutes['windows']) or set(windows)!=set(ids):raise ValueError('MINUTE_COHORT_MISMATCH')
    rows=[];unknown=[];diags=[]
    for sid in ids:
        f=by_id[sid];p=f['plan'];window=windows[sid];original=outcomes[sid]
        if p['strategy']!='VWAP_REVERSION_V3_RECLAIM' or not f['decision']['accepted']:raise ValueError('INELIGIBLE_SOURCE')
        if any(p[k]!=original[k] for k in c.PLAN_FIELDS):raise ValueError('SOURCE_PLAN_CHANGED')
        c.validate_plan(p,f['snapshot'],source['at_ms']);control=c.validate_control(p,original,source['at_ms'])
        try:
            if window['symbol']!=p['symbol'] or len(window['bars'])!=480:raise ValueError('INCOMPLETE_WINDOW')
            result=e.replay(p,window['bars']);parity(result['control'],control)
            if any(result[name] is None for name in e.RULES):raise ValueError('VARIANT_INCOMPLETE')
            diag=e.diagnostics(p,window['bars'],control);diags.append({'id':sid,'control_net_usdt':control['net_usdt'],**diag})
            side=1 if p['direction']=='LONG' else -1
            ctrl_trigger=control['exit_price']/(1-side*.0005)
            control10=c.expected_settlement(p,ctrl_trigger,control['closed_ms'],10)['net_usdt']
            for name in e.RULES:
                variant=result[name];trigger=variant['exit_price']/(1-side*.0005)
                stress=c.expected_settlement(p,trigger,variant['closed_ms'],10)['net_usdt']
                rows.append({'source_id':sid,'variant':name,'symbol':p['symbol'],'direction':p['direction'],
                             'opened_ms':p['opened_ms'],'control_net_usdt':control['net_usdt'],'variant_net_usdt':variant['net_usdt'],
                             'delta_usdt':variant['net_usdt']-control['net_usdt'],
                             'control10_net_usdt':control10,'stress10_net_usdt':stress,
                             'control_closed_ms':control['closed_ms'],'variant_closed_ms':variant['closed_ms'],
                             'control_reason':control['reason'],'variant_reason':variant['reason'],
                             'armed_ms':result['protected_state']['armed_ms'] if name=='VWAP_PROTECT_1R' else None,
                             'variant_costs':json.loads(variant['costs'])})
        except ValueError as exc:
            unknown.append({'source_id':sid,'reason':str(exc),'source_control_net_usdt':control['net_usdt']})
    # Midpoint depends only on the complete fixed input cohort, never on outcome/coverage.
    start=min(by_id[i]['plan']['opened_ms'] for i in ids);end=max(by_id[i]['plan']['opened_ms'] for i in ids);mid=(start+end)//2
    variants=[]
    for name in e.RULES:
        r=[r for r in rows if r['variant']==name];a=stats(r,'variant_net_usdt');b=stats(r,'control_net_usdt');stress=stats(r,'stress10_net_usdt')
        halves=[stats([x for x in r if x['opened_ms']<=mid],'variant_net_usdt'),stats([x for x in r if x['opened_ms']>mid],'variant_net_usdt')]
        without_best=a['net_usdt']-max((x['variant_net_usdt'] for x in r),default=0)
        delta=a['net_usdt']-b['net_usdt'];delta_without_best=delta-max((x['delta_usdt'] for x in r),default=0)
        checks={'min_closed_pairs':len(r)>=e.CHECKS['min_closed_pairs'],'min_coverage_fraction':len(r)/len(ids)>=e.CHECKS['min_coverage_fraction'],
                'min_utc_days':a['utc_days']>=e.CHECKS['min_utc_days'],'positive_primary_net':a['net_usdt']>0,
                'positive_paired_delta':delta>0,'nonnegative_stress10_net':stress['net_usdt']>=0,
                'nonnegative_both_calendar_halves':all(h['closed']>0 and h['net_usdt']>=0 for h in halves),
                'nonnegative_net_without_best_trade':bool(r) and without_best>=0,
                'nonnegative_delta_without_best_improvement':bool(r) and delta_without_best>=0}
        variants.append({'variant':name,'control':b,'result':a,'paired_delta_usdt':delta,'stress10':stress,
                         'control10':stats(r,'control10_net_usdt'),'coverage_fraction':len(r)/len(ids),
                         'improved_pairs':sum(x['delta_usdt']>1e-9 for x in r),'worsened_pairs':sum(x['delta_usdt']<-1e-9 for x in r),
                         'unchanged_pairs':sum(abs(x['delta_usdt'])<=1e-9 for x in r),
                         'gained_usdt':sum(max(x['delta_usdt'],0) for x in r),'given_up_usdt':-sum(min(x['delta_usdt'],0) for x in r),
                         'without_best_net_usdt':without_best,'delta_without_best_improvement_usdt':delta_without_best,
                         'halves':halves,'checks':checks,'failed_checks':[k for k,v in checks.items() if not v],
                         'historical_launch_checks_passed':all(checks.values()),'paper_promotion':False})
    losers=[d for d in diags if d['control_net_usdt']<0]
    return {'study':'vwap-exits-20261122','mode':'EXPLORATORY_PREVIOUSLY_REVIEWED_COHORT','orders_possible':False,
            'declaration_sha256':DECLARATION_SHA256,'minutes_sha256':sha(directory/'minutes.json.gz'),
            'source_ids':ids,'parity_validated':len(diags),'unknown':unknown,'source_snapshot_ms':source['at_ms'],
            'calendar_start_ms':start,'calendar_end_ms':end,'calendar_split_ms':mid,
            'diagnosis':{'source_losers':len(losers),'losers_closed_at_1r_before_exit':sum(x['closed_above_1r_before_exit'] for x in losers),
                         'losers_closed_at_half_r_before_exit':sum(x['closed_above_half_r_before_exit'] for x in losers)},
            'variants':variants,'rows':rows,'diagnostic_rows':diags}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--directory',required=True);p.add_argument('--source-directory',required=True);a=p.parse_args()
    result=review(a.directory,a.source_directory)
    Path(a.directory,'review.json').write_bytes((json.dumps(result,sort_keys=True,indent=2,allow_nan=False)+'\n').encode())
    print(json.dumps({k:v for k,v in result.items() if k not in ('rows','diagnostic_rows')},indent=2))
