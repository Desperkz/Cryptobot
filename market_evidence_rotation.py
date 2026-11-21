"""Lossless physical rotation of market evidence; frozen collector stays unchanged.

Call only with collector and RANGE observer stopped. Cold rows remain in a
verified full SQLite gzip archive; active one-hour overlap keeps future reads.
"""
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

TABLES=('meta','segments','snapshots','liquidations','issues','connections')
MANIFEST_SHA='2d4676395c80334c2c89e30585e16df1afdd9c380138227e9b754a244befa60b'
KEEP_MS=3600000
THRESHOLD=192*1024**2


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)


def file_sha(path, opener=open):
    value=hashlib.sha256()
    with opener(path,'rb') as source:
        while block:=source.read(1024*1024):value.update(block)
    return value.hexdigest()


def ro(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA cache_size=-512')
    return db


def rows_fingerprint(db):
    result={}
    for table in TABLES:
        info=list(db.execute('PRAGMA table_info('+table+')'))
        order=[x[1] for x in sorted(info,key=lambda r:r[5]) if x[5]]
        assert order, 'Expected stable primary key'
        h=hashlib.sha256();count=0
        for row in db.execute('SELECT * FROM '+table+' ORDER BY '+','.join(order)):
            h.update((canonical(list(row))+'\n').encode());count+=1
        result[table]={'rows':count,'sha256':h.hexdigest()}
    return result


def context(db,symbol,at):
    minute=(at//60000-1)*60000
    queries=(
        ("SELECT id,symbol,received_ms,payload FROM snapshots WHERE symbol=? AND kind='open_interest' AND received_ms BETWEEN ? AND ? ORDER BY received_ms,id",(symbol,at-420000,at)),
        ('SELECT session,symbol,minute_ms,payload FROM segments WHERE symbol=? AND minute_ms BETWEEN ? AND ? ORDER BY minute_ms,session',(symbol,minute-60000,minute+60000)),
        ('SELECT * FROM connections WHERE started_ms<=? AND (ended_ms IS NULL OR ended_ms>=?) ORDER BY started_ms',(at,at-420000)),
        ("SELECT at_ms,kind,symbol,detail FROM issues WHERE at_ms BETWEEN ? AND ? AND (symbol=? OR symbol='') ORDER BY at_ms",(at-420000,at,symbol)))
    return hashlib.sha256(canonical([[list(r) for r in db.execute(sql,args)] for sql,args in queries]).encode()).hexdigest()


def prepare(source,archives,now_ms,*,expected_manifest=MANIFEST_SHA):
    """Build and verify archive and candidate; never replace the active file."""
    source=Path(source).resolve();archives=Path(archives).resolve()
    assert source.is_file() and source.parent!=archives
    assert shutil.disk_usage(source.parent).free>max(1024**3,3*source.stat().st_size)
    for suffix in ('-wal','-shm'):
        assert not source.with_name(source.name+suffix).exists(),'Close every DB reader/writer before rotation'
    epoch=str(now_ms)+'-'+uuid.uuid4().hex[:8]
    directory=archives/epoch;directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    full=directory/'full.sqlite3'
    candidate=source.parent/('.rotation-'+epoch+'.sqlite3')
    assert not candidate.exists()
    cutoff=now_ms-KEEP_MS
    with closing(ro(source)) as src:
        manifest=src.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()[0]
        assert hashlib.sha256(manifest.encode()).hexdigest()==expected_manifest,'UNEXPECTED_COLLECTOR_MANIFEST'
        assert src.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        fingerprint=rows_fingerprint(src)
        with closing(sqlite3.connect(full)) as backup:src.backup(backup)
        with closing(ro(full)) as backup:assert rows_fingerprint(backup)==fingerprint
        with closing(sqlite3.connect(candidate)) as dst:
            dst.execute('PRAGMA cache_size=-512')
            for kind in ('table','index'):
                for row in src.execute("SELECT sql FROM sqlite_master WHERE type=? AND sql IS NOT NULL ORDER BY name",(kind,)):
                    dst.execute(row[0])
            selection={
                'meta':('',()),
                'segments':(' WHERE minute_ms>=?',(cutoff//60000*60000,)),
                'snapshots':(' WHERE received_ms>=? OR id=(SELECT max(id) FROM snapshots)',(cutoff,)),
                'liquidations':(' WHERE received_ms>=?',(cutoff,)),
                'issues':(' WHERE at_ms>=? OR id=(SELECT max(id) FROM issues)',(cutoff,)),
                'connections':(' WHERE ended_ms IS NULL OR ended_ms>=?',(cutoff,)),
            }
            for table,(where,args) in selection.items():
                cursor=src.execute('SELECT * FROM '+table+where,args)
                placeholders=','.join('?' for _ in cursor.description)
                while batch:=cursor.fetchmany(128):
                    dst.executemany('INSERT INTO '+table+' VALUES('+placeholders+')',batch)
            dst.commit()
            assert dst.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            assert dict(dst.execute('SELECT * FROM meta'))==dict(src.execute('SELECT * FROM meta'))
            for table in ('snapshots','issues'):
                assert dst.execute('SELECT max(id) FROM '+table).fetchone()[0]==src.execute('SELECT max(id) FROM '+table).fetchone()[0]
            samples={}
            for symbol in ('BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','LTCUSDT','ADAUSDT'):
                for age in (0,180000,1800000):
                    key=symbol+':'+str(now_ms-age)
                    samples[key]=context(src,symbol,now_ms-age)
                    assert context(dst,symbol,now_ms-age)==samples[key],'RECENT_CONTEXT_CHANGED'
            tail=rows_fingerprint(dst)
    archive=directory/'full.sqlite3.gz'
    full_sha=file_sha(full)
    with full.open('rb') as raw,archive.open('xb') as target:
        with gzip.GzipFile(fileobj=target,mode='wb',compresslevel=1,mtime=0) as compressed:
            shutil.copyfileobj(raw,compressed,1024*1024)
        target.flush()
        import os
        os.fsync(target.fileno())
    assert file_sha(archive,gzip.open)==full_sha,'ARCHIVE_CONTENT_MISMATCH'
    record={'version':1,'at_ms':now_ms,'source':str(source),'archive':str(archive),
            'candidate':str(candidate),'manifest_sha256':expected_manifest,
            'full_sha256':full_sha,'archive_sha256':file_sha(archive),
            'archive_bytes':archive.stat().st_size,'source_bytes':source.stat().st_size,
            'candidate_bytes':candidate.stat().st_size,'active_cutoff_ms':cutoff,
            'full_tables':fingerprint,'active_tables':tail,'recent_context_samples':samples,
            'archive_verified':True,'registration_and_state_unchanged':True,
            'activated':False}
    (directory/'receipt.json').write_text(json.dumps(record,indent=2))
    # The full snapshot is now preserved as a verified gzip. Retain the old
    # active DB through activation/recovery; only this temporary backup is removed.
    full.unlink()
    archive.chmod(0o400)
    return record


def activate(record):
    """Caller keeps writer/reader paused; old file remains available for recovery."""
    source=Path(record['source']);candidate=Path(record['candidate'])
    archive=Path(record['archive']);previous=source.with_name('.previous-'+candidate.name)
    assert record['archive_verified'] and file_sha(archive)==record['archive_sha256']
    assert file_sha(archive,gzip.open)==record['full_sha256']
    assert not previous.exists()
    for suffix in ('-wal','-shm'):
        assert not source.with_name(source.name+suffix).exists()
    with closing(ro(source)) as db:assert rows_fingerprint(db)==record['full_tables']
    with closing(ro(candidate)) as db:assert rows_fingerprint(db)==record['active_tables']
    source.rename(previous)
    try:candidate.rename(source)
    except BaseException:
        previous.rename(source);raise
    record.update(activated=True,previous=str(previous))
    (archive.parent/'receipt.json').write_text(json.dumps(record,indent=2))
    return record
