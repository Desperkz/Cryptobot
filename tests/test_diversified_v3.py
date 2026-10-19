import asyncio
from dataclasses import replace
from decimal import Decimal as D
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import zlib
import pytest
from trading_bot.research.diversified_shadow import connect,fill_plan,canonical
from trading_bot.strategy_engine.diversified_v3 import RULES,evaluate,expense_fraction
import trading_bot.research.diversified_v3_shadow as follower
from test_diversified_shadow import candle,series,reflected

ROOT=Path(__file__).resolve().parents[1]
SETTINGS=json.loads((ROOT/'diversified_v3_settings.json').read_text())


def setup(name):
    c15,c1,c4=series(2 if name=='RANGE_REBOUND_V3' else 1);t=c15[-1].open_time
    if name=='RANGE_REBOUND_V3':
        c15=[candle(c.open_time,100+2*math.sin(i*math.pi/8)-.05,100+2*math.sin(i*math.pi/8)+.2,100+2*math.sin(i*math.pi/8)-.2,100+2*math.sin(i*math.pi/8)) for i,c in enumerate(c15)]
    if name.startswith(('MEAN','VWAP')):
        c15[-2]=candle(t-900000,94.3,94.6,93.8,94,160)
        c15[-1]=candle(t,94.5,95.7,94.3,95.5,140)
    elif name=='RANGE_REBOUND_V3':
        c15[-2]=candle(t-900000,98.05,98.1,97.7,98,100)
        c15[-1]=candle(t,98.05,98.4,98,98.35,110)
    elif name=='SWEEP_RECLAIM_V3':
        c15[-2]=candle(t-900000,99,99.1,98.4,98.85,160)
        c15[-1]=candle(t,98.9,99.5,98.8,99.35,110)
    else:
        from trading_bot.strategy_engine.indicators import atr
        if name=='SESSION_RANGE_BREAKOUT_V2':
            day=c15[-1].open_time//86400000*86400000
            upper=max(float(c.high) for c in c15 if day<=c.open_time<day+6*3600000)
        else:upper=max(float(c.high) for c in c15[-22:-2])
        a=atr(c15,14)[-1]
        c15[-2]=candle(t-900000,upper-.2*a,upper,upper-.4*a,upper-.05*a,100)
        c15[-1]=candle(t,upper-.2*a,upper+.35*a,upper-.3*a,upper+.25*a,170)
        c4=[candle(c.open_time,80+i*.1,80.2+i*.1,79.8+i*.1,80+i*.1,tf=14400000) for i,c in enumerate(c4)]
    return c15,c1,c4


@pytest.mark.parametrize('name',RULES)
@pytest.mark.parametrize('short',[False,True])
def test_all_revised_setups_have_reachable_long_and_short_entries(name,short):
    c15,c1,c4=setup(name)
    if short:c15,c1,c4=map(reflected,(c15,c1,c4))
    plan,diag=evaluate(name,'BTCUSDT',c15,c1,c4)
    assert plan,diag
    assert evaluate(name,'BTCUSDT',c15,c1,c4,_validated=True)==(plan,diag)
    assert plan.direction==('SHORT' if short else 'LONG')
    assert (plan.target<plan.entry<plan.stop) if short else (plan.stop<plan.entry<plan.target)
    assert diag['reward_risk']>=1.2


@pytest.mark.parametrize('issue',['future','gap','nan','negative_volume','bad_ohlc','stale','partial'])
def test_noncausal_or_invalid_input_is_rejected(issue):
    c15,c1,c4=setup('SWEEP_RECLAIM_V3')
    if issue=='future':c1[-1]=replace(c1[-1],close_time=c15[-1].close_time+1)
    elif issue=='gap':c15.pop(10)
    elif issue=='nan':c15[-1]=replace(c15[-1],high=D('NaN'))
    elif issue=='negative_volume':c15[-1]=replace(c15[-1],volume=D(-1))
    elif issue=='bad_ohlc':c15[-1]=replace(c15[-1],high=D(1))
    elif issue=='stale':c1=c1[:-1]
    else:c15[-1]=replace(c15[-1],close_time=c15[-1].close_time-1)
    assert evaluate('SWEEP_RECLAIM_V3','BTCUSDT',c15,c1,c4)[0] is None


def test_one_turn_without_breaking_setup_extreme_is_not_a_reversal():
    c15,c1,c4=setup('MEAN_REVERSION_V3');c15[-2]=replace(c15[-2],high=D('96.2'))
    assert evaluate('MEAN_REVERSION_V3','BTCUSDT',c15,c1,c4)[1]['reason']=='reversal_not_confirmed'


def test_wick_without_setup_close_reclaim_is_not_a_false_breakout():
    c15,c1,c4=setup('SWEEP_RECLAIM_V3');c15[-2]=replace(c15[-2],close=D('98.6'))
    assert evaluate('SWEEP_RECLAIM_V3','BTCUSDT',c15,c1,c4)[1]['reason']=='sweep_did_not_reclaim'


def test_breakout_with_large_rejection_wick_is_not_a_continuation_entry():
    c15,c1,c4=setup('MOMENTUM_CONTINUATION_V3')
    c15[-1]=replace(c15[-1],high=c15[-1].close+D('2'))
    assert evaluate('MOMENTUM_CONTINUATION_V3','BTCUSDT',c15,c1,c4)[1]['reason']=='weak_breakout_close'


def test_expense_fraction_counts_entry_slippage_once_and_handles_stress():
    plan,_=evaluate('SWEEP_RECLAIM_V3','BTCUSDT',*setup('SWEEP_RECLAIM_V3'))
    fill,_=fill_plan(plan,plan.entry,plan.bar_close_ms+1,SETTINGS)
    assert 0<expense_fraction(fill)<1
    assert expense_fraction(fill,10)>expense_fraction(fill,5)
    assert fill['quantity']>0 and fill['risk_usdt']==2


def fixture(tmp_path,monkeypatch):
    code=tmp_path/'code';(code/'src').mkdir(parents=True)
    (code/'config.yaml').write_text('x');(code/'settings.json').write_text(json.dumps(SETTINGS))
    path=tmp_path/'source.sqlite3';source=sqlite3.connect(path)
    manifest={'cohort':follower.SOURCE_COHORT,'venue':'https://fapi.binance.com','order_capability':False}
    packed=canonical(manifest);monkeypatch.setattr(follower,'SOURCE_HASH',hashlib.sha256(packed.encode()).hexdigest())
    source.executescript('CREATE TABLE meta(key TEXT,value TEXT);CREATE TABLE snapshots(id INTEGER PRIMARY KEY,symbol TEXT,bar_ms INTEGER,observed_ms INTEGER,raw BLOB);')
    source.execute('INSERT INTO meta VALUES(?,?)',('manifest',packed));source.commit()
    db,m=connect(tmp_path/'data',SETTINGS,code,1000,protocol={'rules':RULES})
    follower.registration(db,path)
    status=tmp_path/'status.json';status.write_text(json.dumps({'health':'OK','generated_at_ms':2000,'cohort':follower.SOURCE_COHORT}))
    return source,path,db,m,status


def test_zero_start_does_not_reuse_pre_registration_observations(tmp_path,monkeypatch):
    source,path,db,m,state=fixture(tmp_path,monkeypatch)
    try:
        source.execute('INSERT INTO snapshots VALUES(1,?,?,?,?)',('BTCUSDT',999,2000,b'not decoded'));source.commit()
        follower.scan_feed(db,m,SETTINGS,now_ms=2000,source_path=path,source_status=state)
        assert db.execute('SELECT count(*) FROM positions').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0]==0
        assert int(db.execute("SELECT value FROM meta WHERE key='feed_cursor'").fetchone()[0])==1
    finally:db.close();source.close()


def test_expired_feed_is_skipped_and_source_is_read_only(tmp_path,monkeypatch):
    source,path,db,m,state=fixture(tmp_path,monkeypatch)
    try:
        state.write_text(json.dumps({'health':'OK','generated_at_ms':200000,'cohort':follower.SOURCE_COHORT}))
        source.execute('INSERT INTO snapshots VALUES(1,?,?,?,?)',('BTCUSDT',2000,2001,b'not decoded'));source.commit()
        follower.scan_feed(db,m,SETTINGS,now_ms=200000,source_path=path,source_status=state)
        assert db.execute('SELECT count(*) FROM positions').fetchone()[0]==0
        with follower.open_source(path) as ro:
            with pytest.raises(sqlite3.OperationalError):ro.execute('DELETE FROM snapshots')
    finally:db.close();source.close()


def test_fresh_future_snapshot_is_selected_once_without_reading_source_outcomes(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from trading_bot.strategy_engine.diversified import ResearchPlan
    source,path,db,m,state=fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(follower,'decode',lambda *a:([SimpleNamespace(close_time=2000)],[],[],SimpleNamespace(spread_bps=1),100))
    monkeypatch.setattr(follower,'evaluate',lambda name,*a:(ResearchPlan(name,'BTCUSDT','LONG',2000,100,97,106,6),{'reason':'SIGNAL'}))
    try:
        source.execute('INSERT INTO snapshots VALUES(1,?,?,?,?)',('BTCUSDT',2000,2001,zlib.compress(b'{"observed_ms":2001}')));source.commit()
        for _ in range(2):follower.scan_feed(db,m,SETTINGS,now_ms=2001,source_path=path,source_status=state)
        assert db.execute('SELECT count(*) FROM positions').fetchone()[0]==5
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==6
        assert db.execute("SELECT count(*) FROM observations WHERE reason='PAUSED_AFTER_REPLAY'").fetchone()[0]==1
        assert source.execute('SELECT count(*) FROM snapshots').fetchone()[0]==1
        assert all(row[0]==2001 for row in db.execute('SELECT opened_ms FROM positions'))
    finally:db.close();source.close()


def test_frozen_protocol_cannot_be_edited_on_restart(tmp_path):
    code=tmp_path/'code';(code/'src').mkdir(parents=True);(code/'config.yaml').write_text('x');(code/'settings.json').write_text('{}')
    db,_=connect(tmp_path/'data',SETTINGS,code,1000,protocol={'rules':RULES});db.close()
    with pytest.raises(ValueError,match='Frozen cohort'):connect(tmp_path/'data',SETTINGS,code,2000,protocol={'rules':{}})


@pytest.mark.parametrize('problem',['stale','unhealthy','manifest'])
def test_bad_source_pauses_admissions_without_laundering_source_freshness(tmp_path,monkeypatch,problem):
    source,path,db,m,state=fixture(tmp_path,monkeypatch)
    try:
        if problem=='manifest':source.execute("UPDATE meta SET value='{}'");source.commit()
        elif problem=='unhealthy':state.write_text(json.dumps({'health':'DEGRADED','generated_at_ms':2000,'cohort':follower.SOURCE_COHORT}))
        now=1000000 if problem=='stale' else 2000
        with pytest.raises(ValueError):follower.scan_feed(db,m,SETTINGS,now_ms=now,source_path=path,source_status=state)
        assert db.execute('SELECT count(*) FROM positions').fetchone()[0]==0
        assert db.execute("SELECT value FROM meta WHERE key='feed_cursor'").fetchone()[0]=='0'
    finally:db.close();source.close()


def test_expensive_signals_are_rejected_without_lowering_modeled_expenses(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from trading_bot.strategy_engine.diversified import ResearchPlan
    source,path,db,m,state=fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(follower,'decode',lambda *a:([SimpleNamespace(close_time=2000)],[],[],SimpleNamespace(spread_bps=1),100))
    monkeypatch.setattr(follower,'evaluate',lambda name,*a:(ResearchPlan(name,'BTCUSDT','LONG',2000,100,99.8,101,6),{'reason':'SIGNAL'}))
    try:
        source.execute('INSERT INTO snapshots VALUES(1,?,?,?,?)',('BTCUSDT',2000,2001,zlib.compress(b'{"observed_ms":2001}')));source.commit()
        follower.scan_feed(db,m,SETTINGS,now_ms=2001,source_path=path,source_status=state)
        assert db.execute('SELECT count(*) FROM positions').fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM observations WHERE reason='EXPENSE_BUDGET_LIMIT'").fetchone()[0]==5
    finally:db.close();source.close()


def test_registration_watermark_excludes_existing_snapshot_ids(tmp_path,monkeypatch):
    source,path,db,m,state=fixture(tmp_path,monkeypatch)
    try:
        source.execute('INSERT INTO snapshots VALUES(5,?,?,?,?)',('BTCUSDT',2000,2001,b'not decoded'));source.commit()
        db.execute("DELETE FROM meta WHERE key IN ('feed_cursor','registration_watermark')");db.commit()
        follower.registration(db,path)
        follower.scan_feed(db,m,SETTINGS,now_ms=2001,source_path=path,source_status=state)
        assert db.execute("SELECT value FROM meta WHERE key='feed_cursor'").fetchone()[0]=='5'
        assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0]==0
    finally:db.close();source.close()


@pytest.mark.parametrize('available,busy,expected',[(90,False,True),(90,True,False),(140,True,True),(70,False,False)])
def test_small_read_only_follower_does_not_lose_fresh_inputs_behind_busy_pilot(monkeypatch,available,busy,expected):
    import trading_bot.research.diversified_v3_guard as guard
    monkeypatch.setattr(guard.Path,'read_text',lambda self:f'MemAvailable: {available*1024} kB\nSwapFree: 300000 kB\n')
    monkeypatch.setattr(guard.subprocess,'check_output',lambda *a,**k:'activating\n' if busy else 'inactive\n')
    assert guard.ready() is expected
