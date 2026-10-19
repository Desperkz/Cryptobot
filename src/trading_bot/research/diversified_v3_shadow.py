"""Isolated V3 follower of the unchanged V2 public-market snapshot feed.

No V2 trade outcomes are read. New observations are selected and committed
before any execution minutes are requested. Only future, fresh source snapshots
after registration are eligible. Primary execution retains original 5bps costs.
"""
import argparse
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import zlib

from trading_bot.research.diversified_shadow import (
    PublicClient,connect,decode,fill_plan,admit,monitor,status,canonical)
from trading_bot.strategy_engine.diversified_v3 import RULES,evaluate,expense_fraction,MAX_EXPENSE_FRACTION

SOURCE=Path('/root/bot_diversified_shadow/data/shadow.sqlite3')
SOURCE_STATUS=SOURCE.with_name('status.json')
SOURCE_HASH='37b05125027155f2262454ecd41ee4e7939280c5731e5f0604891a52a0e5cf1a'
SOURCE_COHORT='2026-10-15-diversified-shadow-v1'
MAX_SOURCE_AGE_MS=120000


def open_source(path=SOURCE):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    db.set_progress_handler((lambda deadline=time.monotonic()+10:int(time.monotonic()>deadline)),1000)
    return db


def validate_source(source):
    row=source.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
    if not row or hashlib.sha256(row[0].encode()).hexdigest()!=SOURCE_HASH:raise ValueError('SOURCE_MANIFEST_CHANGED')
    value=json.loads(row[0])
    if value['cohort']!=SOURCE_COHORT or value['venue']!='https://fapi.binance.com' or value['order_capability'] is not False:
        raise ValueError('UNEXPECTED_SOURCE')


def unpack(packed):
    if len(packed)>2*1024**2:raise ValueError('OVERSIZED_SOURCE')
    dec=zlib.decompressobj();raw=dec.decompress(packed,8*1024**2+1)
    if len(raw)>8*1024**2 or not dec.eof or dec.unused_data:raise ValueError('INVALID_SOURCE_PAYLOAD')
    return json.loads(raw)


def registration(db,source_path=SOURCE):
    if db.execute("SELECT 1 FROM meta WHERE key='feed_cursor'").fetchone():return
    with closing(open_source(source_path)) as src:
        validate_source(src);watermark=src.execute('SELECT coalesce(max(id),0) FROM snapshots').fetchone()[0]
    with db:
        db.execute('INSERT INTO meta VALUES(?,?)',('feed_cursor',str(watermark)))
        db.execute('INSERT INTO meta VALUES(?,?)',('registration_watermark',str(watermark)))


def scan_feed(db,manifest,settings,*,now_ms=None,source_path=SOURCE,source_status=SOURCE_STATUS):
    now=int(time.time()*1000) if now_ms is None else now_ms
    health=json.loads(source_status.read_text())
    age=now-health['generated_at_ms']
    if health['health']!='OK' or not 0<=age<=900000 or health['cohort']!=SOURCE_COHORT:
        raise ValueError('SOURCE_NOT_FRESH_AND_HEALTHY')
    cursor=int(db.execute("SELECT value FROM meta WHERE key='feed_cursor'").fetchone()[0])
    with closing(open_source(source_path)) as src:
        validate_source(src)
        batch=[dict(row) for row in src.execute('SELECT * FROM snapshots WHERE id>? ORDER BY id LIMIT 6',(cursor,))]
    for row in batch:
        with db:
            cursor=row['id']
            eligible=(row['bar_ms']>manifest['registered_ms'] and row['observed_ms']>=manifest['registered_ms']
                and 0<=now-row['observed_ms']<=MAX_SOURCE_AGE_MS and row['symbol'] in settings['symbols'])
            if eligible:
                raw=unpack(row['raw'])
                if raw['observed_ms']!=row['observed_ms']:raise ValueError('SOURCE_TIME_MISMATCH')
                c15,c1,c4,metrics,mid=decode(raw,row['symbol'],raw['observed_ms'])
                if c15[-1].close_time!=row['bar_ms']:raise ValueError('SOURCE_BAR_MISMATCH')
                db.execute('INSERT INTO snapshots(id,symbol,bar_ms,observed_ms,raw) VALUES(?,?,?,?,?)',
                    (row['id'],row['symbol'],row['bar_ms'],row['observed_ms'],row['raw']))
                for name in RULES:
                    if not RULES[name].get('forward_enabled',True):
                        plan=None;detail={'reason':'PAUSED_AFTER_REPLAY','strategy':name}
                    else:plan,detail=evaluate(name,row['symbol'],c15,c1,c4)
                    reason=detail['reason']
                    if plan:
                        fill,reason=fill_plan(plan,mid,row['observed_ms'],settings)
                        if fill:
                            share=expense_fraction(fill);detail['stop_budget_expense_fraction']=share
                            if share>MAX_EXPENSE_FRACTION:reason='EXPENSE_BUDGET_LIMIT'
                            elif metrics.spread_bps>8:reason='SPREAD_LIMIT'
                            elif name=='SESSION_RANGE_BREAKOUT_V2' and db.execute(
                                'SELECT 1 FROM positions WHERE strategy=? AND symbol=? AND direction=? AND opened_ms>=?',
                                (name,row['symbol'],plan.direction,plan.bar_close_ms//86400000*86400000)).fetchone():reason='SESSION_ALREADY_TAKEN'
                            else:reason=admit(db,plan,mid,row['observed_ms'],settings,row['id'])
                    db.execute('INSERT INTO observations(snapshot_id,strategy,symbol,bar_ms,reason,detail) VALUES(?,?,?,?,?,?)',
                        (row['id'],name,row['symbol'],row['bar_ms'],reason,canonical(detail)))
            db.execute("UPDATE meta SET value=? WHERE key='feed_cursor'",(str(cursor),))
    return {'source_cohort':SOURCE_COHORT,'source_age_ms':age,'feed_cursor':cursor,
            'processed_snapshots':len(batch),'maximum_entry_publication_delay_ms':MAX_SOURCE_AGE_MS}


async def cycle(directory,code_root,*,init_only=False):
    settings=json.loads((code_root/'settings.json').read_text());now=int(time.time()*1000)
    if settings['parameters']:raise ValueError('V3 rules are fixed; new parameters require a new named cohort')
    protocol={'comparison':'Six revised shadow hypotheses; unchanged V2 feed is the separate control',
        'scope':'Prospective source-feed observations only; timestamped model entries may publish up to 120 seconds later; no actual orders',
        'rules':RULES,'source_manifest_sha256':SOURCE_HASH,'previous_cohort':SOURCE_COHORT}
    db,manifest=connect(directory,settings,code_root,now,protocol=protocol)
    client=PublicClient();errors=[];feed={}
    try:
        registration(db)
        if not init_only:
            # Commit input-based decisions before reading any outcome minutes.
            backlog=db.execute("SELECT 1 FROM positions WHERE status='OPEN' AND next_ms<? LIMIT 1",(now-900000,)).fetchone()
            size=sum(p.stat().st_size for p in directory.glob('shadow.sqlite3*'))
            if backlog:errors.append('MINUTE_BACKLOG: new admissions paused')
            elif size>settings['max_store_mb']*1024**2:errors.append('STORAGE_LIMIT: new admissions paused')
            else:
                try:feed=scan_feed(db,manifest,settings,now_ms=now)
                except Exception as exc:db.rollback();errors.append('FEED: '+str(exc)[:200])
            try:await monitor(db,client,now)
            except Exception as exc:db.rollback();errors.append('MONITOR: '+str(exc)[:200])
        result=status(db,manifest,directory,errors,strategies=list(RULES),extra={
            'rules_version':'V3_REVISED','statistics_since_ms':manifest['registered_ms'],
            'previous_cohort':SOURCE_COHORT,'source_feed':feed,
            'changes':{k:v['change'] for k,v in RULES.items()},
            'paused_strategies':[k for k,v in RULES.items() if not v.get('forward_enabled',True)],
            'cost_budget_limit_fraction':MAX_EXPENSE_FRACTION})
        print(canonical({'health':result['health'],'entries':sum(g['entries'] for g in result['groups']),'errors':errors}))
        return result
    finally:db.close();await client.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--code-root',type=Path,required=True);p.add_argument('--init-only',action='store_true');args=p.parse_args()
    if args.data_dir.resolve()==SOURCE.parent.resolve() or args.data_dir.resolve()==args.code_root.resolve() or args.data_dir.name!='data':
        raise ValueError('Use a separate V3 data directory')
    asyncio.run(cycle(args.data_dir,args.code_root,init_only=args.init_only))


if __name__=='__main__':main()
