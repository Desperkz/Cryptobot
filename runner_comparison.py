"""Isolated paired exit study of actual future SQZ paper entries.

Only unsigned market GETs and writes to its own directory. Source DB is read-only.
The frozen monitor copy supplies pure exit/cost functions; its DB writers are never called.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import paper_monitor_v2 as monitor

MINUTE = 60000
ARMS = ('CURRENT_PROFILE', 'RUNNER_TRAILING')
PROFILE = 'TP1:1.0R@0.25/BE|TP2:1.6R@0.35/TR|RUNNER:2.2R@0.4/TR'
MAX_SOURCES = 200
MAX_REQUESTS = 8
MAX_HOURS = 72
RULE = {
    'name': 'main-paper-runner-paired-v1-20261006',
    'scope': 'ACTUAL_FUTURE_SQZ_PAPER_ENTRIES',
    'selection': 'Future source IDs and timestamps only, SQZ, exact saved current exit profile; no new entry filters',
    'treatment': 'Remove fixed RUNNER and final whole-position take-profit; retain TP1, TP2, initial stop, BE, trailing and source quantity/risk',
    'execution': 'Same closed contiguous minute bars, effective source entry, original quantities, conservative stop-first bar ordering; not an exact 15-second monitor replay',
    'entry_alignment': 'First full minute after the saved entry; the partially observed entry minute is excluded in both arms',
    'horizon': 'Common 72h administrative limit; additionally retain 24h diagnostics after actual bot exit, within that horizon',
    'costs': 'Frozen main monitor functions: 4bps each-side fees, 5bps exit slippage, signed entry funding or its signed 1bp/8h fallback; effective entry already includes its slippage',
    'review': 'At least 14 elapsed days and 50 completed pairs; compare net delta, losses, drawdown, duration, overlap and model discrepancy; no automatic promotion',
    'capital': 'Correlated paired virtual observations, not additive portfolios; same-coin treatment overlap is flagged',
    'order_capability': False,
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Nonfinite number')
    return result


def stamp(raw):
    value = monitor._parse_timestamp(raw)
    if value is None:
        raise ValueError('Missing timestamp')
    return int(value.timestamp()*1000)


def make_plan(row, registered_ms):
    metadata = json.loads(row['metadata'])
    signal = metadata.get('signal_metadata', {})
    if row['mode'] != 'PAPER_TRADING' or signal.get('strategy') != 'SQUEEZE_BREAKOUT':
        raise ValueError('NOT_SQZ_PAPER')
    if 'status' in row.keys() and row['status'] not in ('ACCEPTED','OPEN','ACTIVE','CLOSED'):
        raise ValueError('NOT_ACCEPTED_ENTRY')
    if stamp(row['created_at']) < registered_ms:
        raise ValueError('PRE_REGISTRATION')
    if signal.get('exit_profile_signature') != PROFILE:
        raise ValueError('DIFFERENT_EXIT_PROFILE')
    targets = metadata['partial_take_profits']
    if [t['name'] for t in targets] != ['TP1','TP2','RUNNER']:
        raise ValueError('INVALID_TARGETS')
    expected = [('1.0','0.25',True,False),('1.6','0.35',False,True),('2.2','0.4',False,True)]
    for target,(r,f,be,tr) in zip(targets,expected):
        if number(target['reward_risk']) != number(r) or number(target['fraction']) != number(f) or bool(target.get('move_stop_to_breakeven')) != be or bool(target.get('activate_trailing')) != tr:
            raise ValueError('INVALID_TARGET_PROFILE')
        if number(target['price']) <= 0 or number(target['quantity']) <= 0:
            raise ValueError('INVALID_TARGET_NUMBERS')
    original_stop = metadata.get('original_stop_loss')
    if original_stop is None:
        if metadata.get('filled_partial_targets') or metadata.get('stop_moved_to_breakeven') or metadata.get('trailing_active'):
            raise ValueError('ORIGINAL_STOP_MISSING')
        original_stop = row['stop_loss']
    entry = number(row['entry_price']);stop = number(original_stop)
    risk = number(row['risk_amount']);qty = sum((number(t['quantity']) for t in targets),Decimal(0))
    if row['direction'] not in ('LONG','SHORT') or min(entry,stop,risk,qty) <= 0:
        raise ValueError('INVALID_ENTRY')
    sign = 1 if row['direction']=='LONG' else -1
    if (entry-stop)*sign <= 0 or any((number(t['price'])-entry)*sign <= 0 for t in targets):
        raise ValueError('INVALID_STOP_OR_TARGET')
    if metadata.get('original_quantity') is not None and number(metadata['original_quantity']) != qty:
        raise ValueError('QUANTITY_MISMATCH')
    if [number(t['price'])*sign for t in targets] != sorted(number(t['price'])*sign for t in targets):
        raise ValueError('UNORDERED_TARGETS')
    opened = stamp(row['created_at']);start = (opened//MINUTE+1)*MINUTE
    # Copy only original protection inputs, never evolved stops/anchors/filled flags.
    original_metadata = {'signal_metadata':copy.deepcopy(signal),'protection':copy.deepcopy(metadata.get('protection',{}))}
    for key in ('trailing_active','trailing_anchor_price','trailing_stop_price'):
        original_metadata['protection'].pop(key,None)
    funding = monitor._metadata_funding_rate(metadata)
    return {'source_id':row['id'],'symbol':row['symbol'],'direction':row['direction'],
        'entry_price':str(entry),'initial_stop':str(stop),'risk_usdt':str(risk),
        'quantity':str(qty),'opened_ms':opened,'start_ms':start,'end_ms':start+MAX_HOURS*60*MINUTE,
        'take_profit':str(number(row['take_profit'])),'targets':targets,
        'metadata':original_metadata,'funding_rate':str(funding) if funding is not None else None,
        'logic':signal.get('strategy_logic_version'),'regime':signal.get('regime')}


def new_state(plan):
    return {'status':'OPEN','remaining':plan['quantity'],'stop':plan['initial_stop'],
        'filled':[],'metadata':copy.deepcopy(plan['metadata']),
        'costs':{k:'0' for k in ('gross_pnl','fees','slippage_cost','funding_cost','net_pnl')},
        'events':[],'last_ms':None,'mark_net_usdt':None}


def close_piece(plan,state,qty,price,at_ms,reason,target):
    qty = min(number(qty),number(state['remaining']))
    execution = monitor._execution_pnl(plan['direction'],number(plan['entry_price']),number(price),qty,
        opened_at=datetime.fromtimestamp(plan['opened_ms']/1000,timezone.utc),
        closed_at=datetime.fromtimestamp(at_ms/1000,timezone.utc),
        funding_rate_per_8h=number(plan['funding_rate']) if plan['funding_rate'] is not None else None)
    for key in state['costs']:
        state['costs'][key] = str(number(state['costs'][key])+getattr(execution,key))
    state['events'].append({'at_ms':at_ms,'reason':reason,'target':target,'quantity':str(qty),
        'trigger':str(price),'fill':str(execution.effective_close_price),'net_usdt':str(execution.net_pnl)})
    state['remaining'] = str(number(state['remaining'])-qty)
    if number(state['remaining']) == 0:
        state.update(status='CLOSED',closed_ms=at_ms,reason=reason)


def step(plan,state,bar,arm):
    if state['status']=='CLOSED':
        return
    # A completed minute, never an unfinished live candle.
    at = int(bar[0])+MINUTE
    snap = monitor.MarketSnapshot(number(bar[4]),number(bar[2]),number(bar[3]))
    sl,_ = monitor._apply_trailing_stop(plan['direction'],snap,number(state['stop']),state['metadata'])
    state['stop'] = str(sl)
    # Stop-first is shared. A gap is executed at the adverse opening price, not at a fictitious stop fill.
    if monitor._stop_hit(plan['direction'],snap,sl):
        opening=number(bar[1]);price=min(sl,opening) if plan['direction']=='LONG' else max(sl,opening)
        close_piece(plan,state,state['remaining'],price,at,'STOP','FINAL')
    else:
        targets=plan['targets'] if arm=='CURRENT_PROFILE' else plan['targets'][:2]
        # All reached targets may fill in this closed bar, in fixed target order.
        # New BE/trailing levels apply next minute: no invented high/low chronology.
        for target in targets:
            if target['name'] in state['filled'] or not monitor._target_hit(plan['direction'],snap,number(target['price'])):
                continue
            close_piece(plan,state,target['quantity'],target['price'],at,'PARTIAL',target['name'])
            state['filled'].append(target['name'])
            if target.get('move_stop_to_breakeven'):
                be=monitor._breakeven_price(plan['direction'],number(plan['entry_price']),state['metadata'])
                state['stop']=str(max(number(state['stop']),be) if plan['direction']=='LONG' else min(number(state['stop']),be))
            if target.get('activate_trailing') and state['status']=='OPEN':
                state['metadata']['trailing_active']=True
                state['metadata'].setdefault('trailing_anchor_price',target['price'])
        if state['status']=='OPEN' and arm=='CURRENT_PROFILE' and monitor._target_hit(plan['direction'],snap,number(plan['take_profit'])):
            close_piece(plan,state,state['remaining'],plan['take_profit'],at,'FINAL_TARGET','FINAL')
        if state['status']=='OPEN' and at >= plan['end_ms']:
            close_piece(plan,state,state['remaining'],bar[4],at,'HORIZON_72H','FINAL')
    state['last_ms']=at
    unrealized=Decimal(0)
    if state['status']=='OPEN':
        unrealized=monitor._execution_pnl(plan['direction'],number(plan['entry_price']),number(bar[4]),number(state['remaining']),
            opened_at=datetime.fromtimestamp(plan['opened_ms']/1000,timezone.utc),
            closed_at=datetime.fromtimestamp(at/1000,timezone.utc),
            funding_rate_per_8h=number(plan['funding_rate']) if plan['funding_rate'] is not None else None).net_pnl
    state['mark_net_usdt']=str(number(state['costs']['net_pnl'])+unrealized)


def validate_bar(raw,expected):
    if len(raw)<7 or int(raw[0])!=expected or int(raw[6])!=expected+MINUTE-1:
        raise ValueError('MISSING_OR_MISALIGNED_MINUTE')
    o,h,l,c=(number(raw[i]) for i in (1,2,3,4))
    if min(o,h,l,c)<=0 or not l<=min(o,c)<=max(o,c)<=h:
        raise ValueError('INVALID_OHLC')


def source_connection(path):
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.25)
    conn.row_factory=sqlite3.Row;conn.execute('PRAGMA query_only=ON')
    return conn


def manifest(settings,source,at_ms):
    with closing(source_connection(source)) as conn:
        watermark=conn.execute('SELECT coalesce(max(id),0) FROM trades').fetchone()[0]
    return {**RULE,'mode':'LOCAL_PAPER_ONLY','registered_ms':at_ms,'source_max_id':watermark,
        'settings':settings,'arms':list(ARMS),'risk':'Exact source risk, no increase',
        'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'monitor_sha256':hashlib.sha256(Path(monitor.__file__).read_bytes()).hexdigest()}


def connect_store(directory,settings,source,at_ms):
    directory.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(directory/'runner_comparison.sqlite3',timeout=.25)
    db.row_factory=sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY,plan TEXT,state TEXT,next_ms INTEGER,
            actual_closed_ms INTEGER,actual_net REAL,skip TEXT);
        CREATE TABLE IF NOT EXISTS candles(symbol TEXT,open_ms INTEGER,payload TEXT,PRIMARY KEY(symbol,open_ms));''')
    row=db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
    if row is None:
        data=manifest(settings,source,at_ms)
        if data['monitor_sha256']!=settings['monitor_sha256']:
            raise ValueError('Monitor snapshot mismatch')
        with db:db.execute('INSERT INTO meta VALUES(?,?)',('manifest',canonical(data)))
    else:
        data=json.loads(row[0])
        if data['settings']!=settings or data['code_sha256']!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest() or data['monitor_sha256']!=hashlib.sha256(Path(monitor.__file__).read_bytes()).hexdigest():
            db.close();raise ValueError('Frozen study changed')
    return db,data


def ingest(db,source,protocol):
    latest=db.execute('SELECT coalesce(max(id),?) FROM sources',(protocol['source_max_id'],)).fetchone()[0]
    capacity=db.execute('SELECT count(*) FROM sources WHERE skip IS NULL').fetchone()[0]
    with closing(source_connection(source)) as conn:
        rows=conn.execute('SELECT * FROM trades WHERE id>? ORDER BY id LIMIT 256',(latest,)).fetchall()
        for row in rows:
            try:
                if capacity>=MAX_SOURCES:raise ValueError('STUDY_SOURCE_LIMIT')
                plan=make_plan(row,protocol['registered_ms'])
                overlap=False
                for existing in db.execute('SELECT plan,state FROM sources WHERE skip IS NULL'):
                    ep,es=json.loads(existing['plan']),json.loads(existing['state'])
                    if ep['symbol']==plan['symbol'] and es['RUNNER_TRAILING']['status']=='OPEN':overlap=True
                plan['overlap_with_open_runner']=overlap
                state={arm:new_state(plan) for arm in ARMS};state['diagnostics']={'peak_price_r':0,'post_actual_exit_peak_price_r':None}
                with db:db.execute('INSERT INTO sources(id,plan,state,next_ms) VALUES(?,?,?,?)',
                    (row['id'],canonical(plan),canonical(state),plan['start_ms']))
                capacity+=1
            except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
                with db:db.execute('INSERT INTO sources(id,skip) VALUES(?,?)',(row['id'],str(exc)[:100]))
        for saved in db.execute('SELECT id FROM sources WHERE skip IS NULL').fetchall():
            row=conn.execute('SELECT closed_at,realized_pnl,status FROM trades WHERE id=?',(saved['id'],)).fetchone()
            if row and row['status']=='CLOSED' and row['closed_at']:
                with db:db.execute('UPDATE sources SET actual_closed_ms=?,actual_net=? WHERE id=?',
                    (stamp(row['closed_at']),float(number(row['realized_pnl'])),saved['id']))


def report(db,protocol,now_ms,errors=()):
    sources=[];skips=skip_counts(db);lags=[]
    pairs=[]
    groups={arm:{'arm':arm,'closed':0,'open':0,'net_usdt':0,'wins':0,'losses':0,'gross_profit':0,'gross_loss':0} for arm in ARMS}
    for row in db.execute('SELECT * FROM sources WHERE skip IS NULL ORDER BY id'):
        plan,state=json.loads(row['plan']),json.loads(row['state'])
        if any(state[a]['status']=='OPEN' for a in ARMS):
            lags.append(max(0,(min(now_ms//MINUTE*MINUTE,plan['end_ms'])-row['next_ms'])/1000))
        for arm in ARMS:
            s=state[arm];g=groups[arm];g['closed' if s['status']=='CLOSED' else 'open']+=1
            if s['status']=='CLOSED':
                net=float(number(s['costs']['net_pnl']));g['net_usdt']+=net
                g['wins']+=int(net>0);g['losses']+=int(net<0);g['gross_profit']+=max(net,0);g['gross_loss']+=max(-net,0)
        if all(state[a]['status']=='CLOSED' for a in ARMS):
            delta=float(number(state['RUNNER_TRAILING']['costs']['net_pnl'])-number(state['CURRENT_PROFILE']['costs']['net_pnl']))
            pairs.append({'id':row['id'],'delta_usdt':delta,'closed_ms':max(state[a]['closed_ms'] for a in ARMS)})
        sources.append({'id':row['id'],'symbol':plan['symbol'],'direction':plan['direction'],
            'opened_ms':plan['opened_ms'],'risk_usdt':plan['risk_usdt'],'overlap':plan['overlap_with_open_runner'],
            'actual_net_usdt':row['actual_net'],'actual_closed_ms':row['actual_closed_ms'],
            'model_minus_actual_usdt':float(number(state['CURRENT_PROFILE']['costs']['net_pnl']))-row['actual_net'] if state['CURRENT_PROFILE']['status']=='CLOSED' and row['actual_net'] is not None else None,
            'arms':{**{a:{k:v for k,v in state[a].items() if k!='metadata'} for a in ARMS},'diagnostics':state['diagnostics']},
            'processed_through_ms':row['next_ms']})
    for g in groups.values():
        g['profit_factor']=g['gross_profit']/g['gross_loss'] if g['gross_loss'] else None
        g['winrate']=g['wins']/g['closed']*100 if g['closed'] else None
    pairs.sort(key=lambda p:(p['closed_ms'],p['id']))
    paired_groups={}
    for arm in ARMS:
        equity=peak=drawdown=0.0;durations=[];nets=[];risk_returns=[]
        for pair in pairs:
            source=next(s for s in sources if s['id']==pair['id']);s=source['arms'][arm]
            net=float(number(s['costs']['net_pnl']));nets.append(net);equity+=net;peak=max(peak,equity);drawdown=min(drawdown,equity-peak)
            durations.append((s['closed_ms']-source['opened_ms'])/3600000)
            risk_returns.append(net/float(number(source['risk_usdt'])))
        positive=[n for n in nets if n>0];negative=[n for n in nets if n<0]
        paired_groups[arm]={'net_usdt':sum(nets),'avg_r':sum(risk_returns)/len(nets) if nets else None,
            'avg_win_usdt':sum(positive)/len(positive) if positive else None,
            'avg_loss_usdt':sum(negative)/len(negative) if negative else None,
            'closed_pnl_drawdown_usdt':drawdown,'mean_holding_hours':sum(durations)/len(durations) if durations else None}
    age=(now_ms-protocol['registered_ms'])/86400000
    return {'generated_at_ms':now_ms,'health':'DEGRADED' if errors else 'BACKLOG' if any(lag>600 for lag in lags) else 'OK','mode':'LOCAL_PAPER_ONLY',
        'order_capability':False,'protocol':protocol,'sources':len(sources),'skips':skips,
        'groups':list(groups.values()),'complete_pairs':len(pairs),
        'paired_delta_usdt':sum(p['delta_usdt'] for p in pairs),'paired_groups':paired_groups,
        'positive_pairs':sum(p['delta_usdt']>0 for p in pairs),'negative_pairs':sum(p['delta_usdt']<0 for p in pairs),
        'elapsed_days':age,'review_ready':age>=14 and len(pairs)>=50,'auto_promotion':False,
        'recent_sources':sources[-20:],'errors':list(errors),'source_limit':MAX_SOURCES,
        'backlog_sources':sum(lag>600 for lag in lags),'maximum_backlog_seconds':max(lags,default=0)}


def skip_counts(db):
    return {row['skip']:row['n'] for row in db.execute('SELECT skip,count(*) AS n FROM sources WHERE skip IS NOT NULL GROUP BY skip')}


def write_status(directory,data):
    temp=directory/'status.tmp';temp.write_text(canonical(data),encoding='utf-8');temp.replace(directory/'status.json')


def run(source,directory,settings,*,init_only=False):
    now=int(time.time()*1000);db,protocol=connect_store(directory,settings,source,now)
    errors=[]
    try:
        if not init_only:
            if sum(p.stat().st_size for p in directory.glob('runner_comparison.sqlite3*'))>256*1024**2:
                data=report(db,protocol,now,['STORAGE_LIMIT: study paused'])
                write_status(directory,data)
                return data
            # Freeze actual cost globals and market venue; never read credentials.
            for key,value in settings.get('environment',{}).items():
                attribute=key.removeprefix('PAPER_')
                attribute={'PRICE_BASE_URL':'BASE_URL','TRAILING_CALLBACK_RATE_PCT':'TRAILING_CALLBACK_RATE_PCT'}.get(attribute,attribute)
                if hasattr(monitor,attribute):setattr(monitor,attribute,Decimal(str(value)) if attribute!='BASE_URL' else value)
            if hashlib.sha256(Path(settings['source_monitor']).read_bytes()).hexdigest()!=settings['monitor_sha256'] or hashlib.sha256(Path(settings['source_config']).read_bytes()).hexdigest()!=settings['config_sha256']:
                errors.append('SOURCE_CODE_OR_CONFIG_CHANGED: admissions paused')
            else:ingest(db,source,protocol)
            requests=0;deadline=time.monotonic()+45
            with httpx.Client(base_url=settings['venue'],timeout=8) as client:
                for row in db.execute('SELECT * FROM sources WHERE skip IS NULL ORDER BY next_ms,id').fetchall():
                    plan,state=json.loads(row['plan']),json.loads(row['state']);cursor=row['next_ms']
                    model_closed=all(state[a]['status']=='CLOSED' for a in ARMS)
                    diagnostic_end=min(plan['end_ms'],(row['actual_closed_ms']//MINUTE+1)*MINUTE+24*60*MINUTE) if row['actual_closed_ms'] else plan['end_ms']
                    if model_closed and (row['actual_closed_ms'] is None or cursor>=diagnostic_end):continue
                    end=min(now//MINUTE*MINUTE,plan['end_ms'])
                    if model_closed:end=min(end,diagnostic_end)
                    while cursor<end and requests<MAX_REQUESTS and time.monotonic()<deadline:
                        try:
                            # Reuse saved bars for simultaneous same-symbol source entries.
                            cached=db.execute('SELECT payload FROM candles WHERE symbol=? AND open_ms=?',(plan['symbol'],cursor)).fetchone()
                            if cached:
                                bars=[json.loads(cached[0])]
                            else:
                                response=client.get('/fapi/v1/klines',params={'symbol':plan['symbol'],'interval':'1m','startTime':cursor,'endTime':min(end,cursor+500*MINUTE)-1,'limit':500});requests+=1;response.raise_for_status();bars=response.json()
                                if not isinstance(bars,list) or not bars:raise ValueError('NO_CLOSED_MINUTES')
                            for bar in bars:
                                if time.monotonic()>=deadline:break
                                validate_bar(bar,cursor)
                                if cursor+MINUTE>end:raise ValueError('UNFINISHED_MINUTE')
                                for arm in ARMS:step(plan,state[arm],bar,arm)
                                entry,stop=number(plan['entry_price']),number(plan['initial_stop'])
                                fav=(number(bar[2])-entry) if plan['direction']=='LONG' else (entry-number(bar[3]))
                                price_r=float(max(Decimal(0),fav/abs(entry-stop)))
                                diag=state['diagnostics'];diag['peak_price_r']=max(diag['peak_price_r'],price_r)
                                if row['actual_closed_ms'] and cursor>=((row['actual_closed_ms']//MINUTE+1)*MINUTE):
                                    diag['post_actual_exit_peak_price_r']=max(diag['post_actual_exit_peak_price_r'] or 0,price_r)
                                with db:
                                    db.execute('INSERT OR IGNORE INTO candles VALUES(?,?,?)',(plan['symbol'],cursor,canonical(bar)))
                                    cursor+=MINUTE
                                    db.execute('UPDATE sources SET state=?,next_ms=? WHERE id=?',(canonical(state),cursor,row['id']))
                        except (httpx.HTTPError,ValueError,TypeError,KeyError,ArithmeticError) as exc:
                            errors.append({'id':row['id'],'cursor':cursor,'error':str(exc)[:180]});break
                    if requests>=MAX_REQUESTS or time.monotonic()>=deadline:break
            if sum(p.stat().st_size for p in directory.glob('runner_comparison.sqlite3*'))>256*1024**2:
                errors.append('STORAGE_LIMIT')
        data=report(db,protocol,int(time.time()*1000),errors)
        write_status(directory,data)
        print(canonical({k:data[k] for k in ('health','sources','complete_pairs','paired_delta_usdt','errors')}))
        return data
    finally:db.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-db',type=Path,required=True);parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--settings',type=Path,required=True);parser.add_argument('--init-only',action='store_true')
    args=parser.parse_args();settings=json.loads(args.settings.read_text(encoding='utf-8'))
    if settings['venue'] not in ('https://demo-fapi.binance.com','https://fapi.binance.com'):
        raise ValueError('Unapproved public market venue')
    if args.source_db.resolve().parent==args.data_dir.resolve():raise ValueError('Source and study directories must differ')
    run(args.source_db,args.data_dir,settings,init_only=args.init_only)


if __name__=='__main__':main()
