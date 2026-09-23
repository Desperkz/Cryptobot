"""Isolated counterfactual labels for observer sources. No trading or source writes."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import shutil
import sqlite3
import time
import zlib
from collections import Counter
from pathlib import Path

from trading_bot.research.mainnet_paper import PublicDataClient, canonical, digest
from trading_bot.research.paper_execution import simulate

MINUTE = 60000
DAY = 86400000
PROTOCOL = {
    'version': 'candidate-outcomes-v1', 'mode': 'COUNTERFACTUAL_LABELS_ONLY',
    'entry': 'next minute after first observation finished_ms (all evidence received)',
    'stop': 'unchanged source stop', 'target_R': 2, 'risk_usdt_before_costs': 2,
    'max_minutes': 1440, 'fees_bps_each_side': 4, 'slippage_bps_each_side': 5,
    'funding': 'first observation rate estimate; adverse 1bp/8h buffer if missing',
    'source': 'first observation per hourly source; never retry gate refusals',
    'scope': 'overlapping hypothetical labels, not a deployable portfolio',
    'cluster': 'first source in fixed 24h windows anchored per symbol and direction',
    'missing_candles': 'retry first missing minute; no invented bars or shifted entry',
}


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def source_manifest(path):
    db = readonly(path)
    try:
        value = db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()[0]
        manifest = json.loads(value)
        if manifest['settings']['experiment'] != 'sqz-candidate-observer-v1':
            raise ValueError('Expected registered candidate observer')
        return manifest
    finally:
        db.close()


def decode(row):
    raw = zlib.decompress(row['payload_zlib'])
    if hashlib.sha256(raw).hexdigest() != row['payload_sha256']:
        raise ValueError('Source checksum mismatch')
    p = json.loads(raw)
    if p['source_id'] != row['source_id'] or p['observed_ms'] != row['observed_ms']:
        raise ValueError('Source identity mismatch')
    s = p['signal']; m = s['metadata']
    expected = f"SQZ:{s['symbol']}:{s['direction']}:{m['source_hour_close_time']}"
    observed, finished = p['observed_ms'], p['finished_ms']
    hour = m['source_hour_close_time']
    if (expected != p['source_id'] or s['direction'] not in ('LONG', 'SHORT')
            or not isinstance(hour, int) or (hour+1) % 3600000 or hour >= observed
            or not isinstance(finished, int) or not isinstance(observed, int)
            or finished < observed or finished > int(time.time()*1000)):
        raise ValueError('Invalid source timing or direction')
    entry, stop = float(s['entry_price']), float(s['stop_loss'])
    if (not all(map(math.isfinite, (entry, stop))) or min(entry, stop) <= 0
            or (entry <= stop if s['direction']=='LONG' else entry >= stop)):
        raise ValueError('Invalid source stop')
    funding = (p.get('metrics') or {}).get('funding_rate')
    if funding is not None and not math.isfinite(float(funding)):
        funding = None
    # Retain first-observation decisions only. No later gate/market information.
    return {'symbol': s['symbol'], 'direction': s['direction'], 'stop': stop,
            'entry_ms': (finished//MINUTE+1)*MINUTE, 'finished_ms': finished,
            'observed_ms': observed, 'funding_rate': funding,
            'liquidity_failures': p.get('liquidity_failures'), 'gates': p.get('gates'),
            'impact': p.get('impact'), 'timing': m.get('squeeze_entry_timing'),
            'source_errors': p.get('errors', []), 'signal_entry': entry}


def validate_bars(raw, start, end):
    if not raw:
        raise ValueError('MISSING_EXECUTION_CANDLES')
    for i, b in enumerate(raw):
        stamp = int(b[0]); values = list(map(float, b[1:5]))
        o,h,l,c = values
        if (stamp != start+i*MINUTE or stamp >= end or int(b[6]) != stamp+MINUTE-1
                or not all(map(math.isfinite, values)) or min(values)<=0
                or l>min(o,c) or h<max(o,c)):
            raise ValueError('EXECUTION_CANDLE_GAP_OR_INVALID_OHLC')
    return raw


class OutcomeStore:
    def __init__(self, directory, manifest, started_ms=None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory/'candidate_outcomes.sqlite3', timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS labels(source_id TEXT PRIMARY KEY,observation_id INTEGER,
                source_sha256 TEXT,entry_ms INTEGER,era TEXT,plan TEXT,status TEXT,result TEXT,
                updated_ms INTEGER,error TEXT);
            CREATE TABLE IF NOT EXISTS candles(symbol TEXT,open_ms INTEGER,payload TEXT,
                PRIMARY KEY(symbol,open_ms));
        ''')
        previous = self.db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
        if previous and previous[0] != canonical(manifest):
            self.db.close()
            raise ValueError('Frozen outcome protocol changed; use a new directory')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('manifest',?)", (canonical(manifest),))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('started_ms',?)", (str(started_ms or int(time.time()*1000)),))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('cursor','0')")

    def storage_ok(self):
        return (shutil.disk_usage(self.directory).free > 512*1024**2 and
                sum(p.stat().st_size for p in self.directory.glob('candidate_outcomes.sqlite3*')) < 512*1024**2)

    def ingest(self, source, manifest):
        if source_manifest(source) != manifest:
            raise ValueError('Observer manifest changed')
        cursor = int(self.db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0])
        started = int(self.db.execute("SELECT value FROM meta WHERE key='started_ms'").fetchone()[0])
        src = readonly(source)
        try:
            # Short read transaction; bounded fetch. Source index makes min(id) cheap.
            rows = src.execute('''SELECT o.* FROM observations o WHERE o.id>? AND
                o.id=(SELECT min(i.id) FROM observations i WHERE i.source_id=o.source_id)
                ORDER BY o.id LIMIT 128''', (cursor,)).fetchall()
        finally:
            src.close()
        with self.db:
            for row in rows:
                plan = decode(row)
                era = 'BACKFILL' if plan['finished_ms'] < started else 'PROSPECTIVE'
                self.db.execute('INSERT OR IGNORE INTO labels VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (row['source_id'],row['id'],row['payload_sha256'],plan['entry_ms'],era,
                     canonical(plan),'PENDING',None,None,None))
                cursor = row['id']
            self.db.execute("UPDATE meta SET value=? WHERE key='cursor'", (str(cursor),))
        return len(rows)


class OutcomeLab:
    def __init__(self, store, client):
        self.store, self.client = store, client

    async def update(self, row, now):
        plan = json.loads(row['plan'])
        entry = plan['entry_ms']; end = min(now//MINUTE*MINUTE,entry+DAY)
        if end <= entry:
            return
        saved = self.store.db.execute('SELECT payload FROM candles WHERE symbol=? AND open_ms>=? AND open_ms<? ORDER BY open_ms',
                                      (plan['symbol'],entry,end)).fetchall()
        bars = []
        for r in saved:
            bar = json.loads(r[0])
            if int(bar[0]) != entry+len(bars)*MINUTE:
                break
            bars.append(bar)
        result = simulate(bars,plan['stop'],plan['direction'],[(2,1,False,False)],
                          complete=len(bars)==1440,funding_rate=plan['funding_rate'])
        cursor = entry+len(bars)*MINUTE
        while cursor < end and result['status'] not in ('CLOSED','INVALID_ENTRY'):
            if not self.store.storage_ok():
                raise RuntimeError('PAUSED_STORAGE_LIMIT')
            raw = await self.client.klines(plan['symbol'],'1m',limit=min(1000,(end-cursor)//MINUTE),
                                           start_time=cursor,end_time=end-1)
            validate_bars(raw,cursor,end)
            with self.store.db:
                for b in raw:
                    old=self.store.db.execute('SELECT payload FROM candles WHERE symbol=? AND open_ms=?',
                                              (plan['symbol'],int(b[0]))).fetchone()
                    if old and old[0]!=canonical(b):
                        raise ValueError('EXECUTION_CANDLE_REVISION')
                self.store.db.executemany('INSERT OR IGNORE INTO candles VALUES(?,?,?)',
                    [(plan['symbol'],int(b[0]),canonical(b)) for b in raw])
            bars.extend(raw); cursor=entry+len(bars)*MINUTE
            result=simulate(bars,plan['stop'],plan['direction'],[(2,1,False,False)],
                            complete=len(bars)==1440,funding_rate=plan['funding_rate'])
            self.client.evidence.clear()
        if 'R' in result:
            result['model_pnl_usdt']=2*result['R']
        with self.store.db:
            self.store.db.execute('UPDATE labels SET status=?,result=?,updated_ms=?,error=NULL WHERE source_id=?',
                (result['status'],canonical(result),now,row['source_id']))

    async def cycle(self, source, manifest):
        now = int(time.time()*1000)
        if not self.store.storage_ok():
            raise RuntimeError('PAUSED_STORAGE_LIMIT')
        imported = self.store.ingest(source, manifest)
        errors=[]
        rows=self.store.db.execute("SELECT * FROM labels WHERE status IN ('PENDING','OPEN') ORDER BY entry_ms LIMIT 128").fetchall()
        for row in rows:
            try:
                await self.update(row,now)
            except Exception as exc:
                error=str(exc)[:400]
                errors.append({'source':row['source_id'],'error':error})
                with self.store.db:
                    self.store.db.execute('UPDATE labels SET error=? WHERE source_id=?',(error,row['source_id']))
            finally:
                self.client.evidence.clear()
        status={'at_ms':now,'finished_ms':int(time.time()*1000),'status':'PARTIAL' if errors else 'OK',
                'imported':imported,'checked':len(rows),'errors':errors,'mode':PROTOCOL['mode']}
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO meta VALUES('cycle',?)",(canonical(status),))
        return status


def summarize(rows):
    """Descriptive comparisons only; refusals overlap, unknowns stay unknown."""
    outcomes=Counter(); filters={}; pnl=0.; marked=0.
    early_keys={'OF_AGAINST','OF_HOSTILE','OF_WEAK_MIXED_SCORE','RS_UNALIGNED','STRUCTURE_BREAK'}
    for row in rows:
        gates=json.loads(row['plan']).get('gates') or {}
        early_keys.update((gates.get('early') or {}).get('early_all_gate_failures',[]))
    for row in rows:
        plan=json.loads(row['plan']); result=json.loads(row['result']) if row['result'] else {}
        outcomes[row['status']]+=1
        outcomes['update_errors']+=int(bool(row['error']))
        if row['status']=='CLOSED':pnl+=result['model_pnl_usdt']
        if row['status']=='OPEN':marked+=result['model_pnl_usdt']
        liq=plan['liquidity_failures']; gates=plan['gates'] or {}; early=gates.get('early')
        blockers={k:(None if liq is None else k in liq) for k in ('VOLUME','SPREAD','DEPTH5')}
        blockers.update({k:(None if early is None else k in early['early_all_gate_failures'])
                         for k in sorted(early_keys)})
        blockers['LIQUIDITY_ALL']=None if liq is None else bool(liq)
        blockers['EARLY_RULE']=None if early is None else bool(early['early_all_gate_failures'])
        original=(gates.get('original') or {}).get('allowed',{})
        for name,arm in [('CURRENT_GATE','CURRENT_GATE_2R'),('P8_OBSERVE','P8_OBSERVE_PROFILE')]:
            blockers[name]=None if arm not in original else not original[arm]
        for key,blocked in blockers.items():
            group=filters.setdefault(key,{'blocked':0,'not_blocked':0,'unknown_or_not_applicable':0,
                'blocked_closed':0,'blocked_wins':0,'blocked_losses':0,'blocked_pnl_usdt':0.,
                'not_blocked_closed':0,'not_blocked_pnl_usdt':0.})
            if blocked is None:
                group['unknown_or_not_applicable']+=1; continue
            prefix='blocked' if blocked else 'not_blocked';group[prefix]+=1
            if row['status']=='CLOSED':
                value=result['model_pnl_usdt'];group[prefix+'_closed']+=1;group[prefix+'_pnl_usdt']+=value
                if blocked:
                    group['blocked_wins']+=int(value>0);group['blocked_losses']+=int(value<0)
    return {'labels':len(rows),'states':dict(outcomes),'closed_label_pnl_usdt':pnl,
            'open_mark_pnl_usdt':marked,'filters':filters}


def report(path):
    db=readonly(path)
    try:
        db.execute('BEGIN')
        meta={r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM meta')}
        rows=[dict(r) for r in db.execute('SELECT * FROM labels ORDER BY entry_ms,source_id')]
    finally:
        db.close()
    selected=[];anchors={}
    for row in rows:
        p=json.loads(row['plan']);key=(p['symbol'],p['direction'])
        if key not in anchors or row['entry_ms']>=anchors[key]+DAY:
            selected.append(row);anchors[key]=row['entry_ms']
    return {'scope':'Counterfactual labels, NOT portfolio return; filter groups overlap; no causal attribution',
            'meta':meta,'all':summarize(rows),'first_24h_per_symbol_direction':summarize(selected),
            'by_era':{era:summarize([r for r in rows if r['era']==era]) for era in ('BACKFILL','PROSPECTIVE')},
            'rows':[{**r,'plan':json.loads(r['plan']),'result':json.loads(r['result']) if r['result'] else None} for r in rows]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True);parser.add_argument('--data-dir',required=True)
    args=parser.parse_args()
    source=Path(args.source).resolve();directory=Path(args.data_dir).resolve()
    if directory==source.parent or directory in source.parents:
        raise ValueError('Outcome directory must be separate from observer data')
    observer=source_manifest(source)
    package=Path(__file__).resolve().parents[1]
    manifest={'protocol':PROTOCOL,'observer':observer,'source_path':str(source),
              'code_sha256':digest({str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in sorted(package.rglob('*.py'))})}
    directory.mkdir(parents=True,exist_ok=True)
    with (directory/'process.lock').open('a+b') as lock:
        import os
        if os.name=='posix':
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        else:
            import msvcrt
            lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        store=OutcomeStore(directory,manifest)
        async def run():
            client=PublicDataClient()
            try:return await OutcomeLab(store,client).cycle(source,observer)
            finally:await client.close()
        try:
            status=asyncio.run(run())
            result=report(directory/'candidate_outcomes.sqlite3')
            for name,payload in [('status',status),('report',result)]:
                tmp=directory/(name+'.tmp');tmp.write_text(json.dumps(payload,indent=2),encoding='utf-8')
                tmp.replace(directory/(name+'.json'))
            print(canonical({'cycle':status,'all':result['all']['states']}))
        finally:
            store.db.close()


if __name__=='__main__':main()
