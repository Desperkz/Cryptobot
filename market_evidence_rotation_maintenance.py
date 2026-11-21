"""Owner-only market archive rotation with persistent service recovery."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

import market_evidence_rotation as rotation

OPS=Path('/root/bot_market_evidence_rotation')
SOURCE=Path('/root/bot_market_evidence/data/market_evidence.sqlite3')
ARCHIVES=Path('/root/bot_market_evidence/archives')
COLLECTOR='market-evidence-collector.service'
OBSERVER='range-oi-flow-shadow'
MARKER=OPS/'data/recovery.json'


def command(*args):
    return subprocess.check_output(args,text=True,stderr=subprocess.STDOUT,timeout=45).strip()


def active(unit):
    return command('systemctl','show',unit,'-p','ActiveState','--value')=='active'


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(value,f,indent=2);f.flush();os.fsync(f.fileno())
    temp.replace(path)


def core():
    return {n:command('systemctl','show',n,'-p','MainPID','-p','NRestarts','-p','ActiveState')
            for n in ('trading-bot-v2-1','paper-monitor-v2-1','mainnet-paper-lab','bot-control-v2-1')}


def decisions():
    p=Path('/root/bot_range_oi_flow/data/range_oi_flow.sqlite3')
    with closing(rotation.ro(p)) as db:
        return {row[0]:rotation.canonical(list(row)) for row in db.execute(
            'SELECT id,opened_ms,plan,plan_sha256,snapshot,decision,saved_ms,control FROM pairs ORDER BY id')}


def recover():
    if not MARKER.exists():return
    v=json.loads(MARKER.read_text())
    assert v['source']==str(SOURCE)
    previous=Path(v['previous']) if v.get('previous') else None
    if not SOURCE.exists():
        assert previous and previous.parent==SOURCE.parent and previous.name.startswith('.previous-.rotation-')
        previous.rename(SOURCE)
    with closing(rotation.ro(SOURCE)) as db:
        manifest=db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()[0]
        import hashlib
        assert hashlib.sha256(manifest.encode()).hexdigest()==rotation.MANIFEST_SHA
        assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    if v['resume_collector'] and not active(COLLECTOR):
        command('systemctl','reset-failed',COLLECTOR)
        command('systemctl','start',COLLECTOR)
    if v['resume_observer'] and not active(OBSERVER+'.timer'):
        command('systemctl','start',OBSERVER+'.timer')
    MARKER.unlink()


def main(force=False):
    recover()
    data=OPS/'data';data.mkdir(parents=True,exist_ok=True)
    used=sum(p.stat().st_size for p in SOURCE.parent.glob('market_evidence.sqlite3*'))
    old_path=data/'status.json'
    old=json.loads(old_path.read_text()) if old_path.exists() else {}
    now=int(time.time()*1000)
    base={'study':'market-evidence-rotation-v1','order_capability':False,'generated_at_ms':now,
          'active_bytes':used,'trigger_bytes':rotation.THRESHOLD,'limit_bytes':256*1024**2,
          'free_disk_bytes':shutil.disk_usage(SOURCE.parent).free,'keep_overlap_ms':rotation.KEEP_MS,
          'last_success_ms':old.get('last_success_ms'),'archive_count':len(list(ARCHIVES.glob('*/full.sqlite3.gz')))}
    if not force and used<rotation.THRESHOLD:
        save(old_path,{**base,'health':'OK','action':'CHECK_ONLY','errors':[]});return
    assert base['free_disk_bytes']>2*1024**3,'Insufficient global headroom for verified lossless rotation'
    before_core=core();record=None
    marker={'source':str(SOURCE),'resume_collector':active(COLLECTOR),
            'resume_observer':active(OBSERVER+'.timer'),'previous':None}
    assert marker['resume_collector'],'Collector must be running before planned maintenance'
    save(MARKER,marker)
    try:
        if marker['resume_observer']:command('systemctl','stop',OBSERVER+'.timer')
        command('systemctl','stop',OBSERVER+'.service')
        command('systemctl','stop',COLLECTOR)
        old_decisions=decisions()
        with closing(sqlite3.connect(SOURCE,timeout=2)) as db:
            assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]==0,'WAL_BUSY'
            assert db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete'
        record=rotation.prepare(SOURCE,ARCHIVES,int(time.time()*1000))
        marker['previous']=str(SOURCE.with_name('.previous-'+Path(record['candidate']).name))
        save(MARKER,marker)
        rotation.activate(record)
        assert decisions()==old_decisions,'Existing observer decisions changed'
        catalog_path=data/'catalog.json'
        catalog=json.loads(catalog_path.read_text()) if catalog_path.exists() else []
        catalog.append(record);save(catalog_path,catalog)
        recover()
        assert core()==before_core,'Trading/UI processes changed during market maintenance'
        # Archive is verified before activation. The previous physical DB is
        # only a temporary recovery copy; all original rows remain in gzip.
        previous=Path(record['previous'])
        assert previous.parent==SOURCE.parent and previous.name.startswith('.previous-.rotation-')
        previous.unlink()
        result={**base,'health':'OK','action':'ROTATED','errors':[],
                'generated_at_ms':int(time.time()*1000),'last_success_ms':int(time.time()*1000),
                'active_bytes':sum(p.stat().st_size for p in SOURCE.parent.glob('market_evidence.sqlite3*')),
                'archive_count':len(catalog),'latest_archive':record['archive'],
                'archived_rows':{t:r['rows'] for t,r in record['full_tables'].items()},
                'recent_contexts_identical':True,'registration_state_and_decisions_preserved':True,
                'main_bot_monitor_producer_controller_not_restarted':True}
        save(old_path,result);print(json.dumps(result))
    except BaseException as exc:
        save(old_path,{**base,'health':'DEGRADED','errors':[type(exc).__name__+':'+str(exc)[:240]],
                       'generated_at_ms':int(time.time()*1000)})
        raise
    finally:
        recover()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--force',action='store_true');p.add_argument('--recover',action='store_true');args=p.parse_args()
    if args.recover:recover()
    else:main(args.force)
