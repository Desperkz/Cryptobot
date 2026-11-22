"""Frozen V3 contract and cost accounting for matched V4 labels; no orders."""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import zlib

from .diversified_v4_gates import canonical,digest,integer,number,VARIANTS

SOURCE=Path('/root/bot_diversified_v3/data/shadow.sqlite3')
SOURCE_HASH='7254d8a4904cb905ae8c6f0b8dc7f87f2ab286c8352d6e6afb04c78d43e1b7a0'
SOURCE_COHORT='2026-10-21-diversified-v3-shadow-r2'
SYMBOLS=('BTCUSDT','BNBUSDT','XRPUSDT','SOLUSDT','DOGEUSDT','LTCUSDT')
HOURS={'VWAP_REVERSION_V3_RECLAIM':8,'SWEEP_RECLAIM_V3':6,'MOMENTUM_CONTINUATION_V3':12}
PLAN_FIELDS=('id','strategy','symbol','direction','opened_ms','entry_mid','entry','stop','target','quantity','risk_usdt','expires_ms','snapshot_id')


def readonly(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('PRAGMA cache_size=-512');db.execute('BEGIN')
    db.set_progress_handler(lambda until=time.monotonic()+8:int(time.monotonic()>until),1000)
    return db


def validate_source(db):
    row=db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
    if row is None or hashlib.sha256(row[0].encode()).hexdigest()!=SOURCE_HASH:raise ValueError('FROZEN_SOURCE_MANIFEST_CHANGED')
    value=json.loads(row[0])
    if (value.get('cohort')!=SOURCE_COHORT or value.get('mode')!='SHADOW_ONLY'
            or value.get('order_capability') is not False or value.get('venue')!='https://fapi.binance.com'
            or value.get('settings',{}).get('risk_usdt')!=2
            or value['settings'].get('symbols')!=list(SYMBOLS)):raise ValueError('UNEXPECTED_SOURCE_PROTOCOL')
    return value


def unpack(blob):
    if not isinstance(blob,bytes) or len(blob)>2*1024**2:raise ValueError('INVALID_SOURCE_BLOB')
    decoder=zlib.decompressobj();raw=decoder.decompress(blob,8*1024**2+1)
    if len(raw)>8*1024**2 or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:raise ValueError('INVALID_SOURCE_PAYLOAD')
    return json.loads(raw)


def quantity(plan,bps=5):
    if bps not in (5,10):raise ValueError('UNDECLARED_SLIPPAGE')
    side=1 if plan['direction']=='LONG' else -1
    entry=plan['entry_mid']*(1+side*bps/10000)
    stop=plan['stop']*(1-side*bps/10000)
    reserve=(plan['expires_ms']-plan['opened_ms'])/28800000
    unit=side*(entry-stop)+(entry+stop)*.0004+entry*.0001*reserve
    if unit<=0:raise ValueError('INVALID_STOP_BUDGET')
    return plan['risk_usdt']/unit


def validate_plan(plan,snapshot,now):
    for key in ('id','opened_ms','expires_ms','snapshot_id'):integer(plan[key])
    if plan['strategy'] not in VARIANTS or plan['direction'] not in ('LONG','SHORT') or plan['symbol'] not in SYMBOLS:raise ValueError('INVALID_SOURCE_PLAN')
    for key in ('entry_mid','entry','stop','target','quantity','risk_usdt'):
        if number(plan[key])<=0:raise ValueError('INVALID_SOURCE_PRICE_OR_SIZE')
    side=1 if plan['direction']=='LONG' else -1
    if (plan['risk_usdt']!=2 or plan['expires_ms']!=plan['opened_ms']+HOURS[plan['strategy']]*3600000
            or min(side*(plan['entry']-plan['stop']),side*(plan['target']-plan['entry']))<=0
            or not math.isclose(plan['entry'],plan['entry_mid']*(1+side*.0005),rel_tol=1e-12,abs_tol=1e-10)
            or not math.isclose(plan['quantity'],quantity(plan),rel_tol=1e-10,abs_tol=1e-9)):
        raise ValueError('SOURCE_EXECUTION_OR_BUDGET_CHANGED')
    for key in ('id','observed_ms','bar_ms'):integer(snapshot[key])
    if (snapshot['id']!=plan['snapshot_id'] or snapshot['symbol']!=plan['symbol']
            or snapshot['observed_ms']!=plan['opened_ms'] or not snapshot['bar_ms']<plan['opened_ms']<=now):raise ValueError('SOURCE_SNAPSHOT_MISMATCH')


def expected_settlement(plan,trigger,when,bps=5):
    side=1 if plan['direction']=='LONG' else -1
    entry=plan['entry_mid']*(1+side*bps/10000);exit_price=trigger*(1-side*bps/10000)
    qty=plan['quantity'] if bps==5 else quantity(plan,bps)
    fees=(entry+exit_price)*qty*.0004
    funding=entry*qty*.0001*(when-plan['opened_ms'])/28800000
    costs={'fees':fees,'funding':funding,'funding_source':'adverse_1bp_per8h_buffer',
           'entry_slippage':abs(entry-plan['entry_mid'])*qty,'exit_slippage':abs(trigger-exit_price)*qty,
           'gross_before_exit_slippage':side*(trigger-entry)*qty}
    return {'exit_price':exit_price,'quantity':qty,'net_usdt':side*(exit_price-entry)*qty-fees-funding,'costs':costs}


def validate_control(plan,row,now):
    if row['status']=='OPEN':return None
    if row['status']!='CLOSED':raise ValueError('INVALID_CONTROL_STATUS')
    when=integer(row['closed_ms']);exit_price=number(row['exit_price']);reason=row['reason']
    first=(plan['opened_ms']//60000+2)*60000-1
    if not first<=when<=min(now,plan['expires_ms']+60000) or when%60000!=59999 or exit_price<=0:raise ValueError('INVALID_CONTROL_CLOSE')
    side=1 if plan['direction']=='LONG' else -1;trigger=exit_price/(1-side*.0005)
    if reason not in ('STOP','STOP_FIRST_AMBIGUOUS','TARGET','TIMEOUT'):raise ValueError('INVALID_CONTROL_REASON')
    if reason=='TARGET' and not math.isclose(trigger,plan['target'],rel_tol=1e-10,abs_tol=1e-9):raise ValueError('CONTROL_TARGET_CHANGED')
    if reason.startswith('STOP') and side*(trigger-plan['stop'])>1e-8:raise ValueError('CONTROL_STOP_CHANGED')
    if reason=='TIMEOUT' and when<plan['expires_ms']:raise ValueError('EARLY_CONTROL_TIMEOUT')
    costs=json.loads(row['costs']) if isinstance(row['costs'],str) else row['costs']
    expected=expected_settlement(plan,trigger,when)
    if not math.isclose(number(row['net_usdt']),expected['net_usdt'],rel_tol=1e-10,abs_tol=1e-9):raise ValueError('CONTROL_NET_MISMATCH')
    for key,value in expected['costs'].items():
        if isinstance(value,str):
            if costs.get(key)!=value:raise ValueError('CONTROL_FUNDING_CONTRACT_CHANGED')
        elif not math.isclose(number(costs[key]),value,rel_tol=1e-10,abs_tol=1e-9):raise ValueError('CONTROL_COST_MISMATCH')
    return {'closed_ms':when,'exit_price':exit_price,'net_usdt':number(row['net_usdt']),
            'reason':reason,'costs':costs}
