import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import time
import zlib
from contextlib import closing

BASE=Path('/root/diversified_v4_research_20261122')
spec=importlib.util.spec_from_file_location('gates',BASE/'diversified_v4_gates.py')
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
SOURCE=Path('/root/bot_diversified_v3/data/shadow.sqlite3')
COLD=Path('/root/bot_research_storage/archives/bot_diversified_v3.sqlite3')
FIELDS=('id','strategy','symbol','direction','opened_ms','entry_mid','entry','stop','target','quantity','risk_usdt','expires_ms','snapshot_id')


def ro(path):
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('PRAGMA cache_size=-512');db.execute('BEGIN')
    return db


def authorizer(action,table,column,*_):
    if action==sqlite3.SQLITE_READ and table=='positions' and column not in (*FIELDS,''):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


rows=[];errors=[];locations={'HOT':0,'COLD':0}
with closing(ro(SOURCE)) as src,closing(ro(COLD)) as cold:
    src.set_authorizer(authorizer)
    manifest=src.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()[0]
    assert hashlib.sha256(manifest.encode()).hexdigest()=='7254d8a4904cb905ae8c6f0b8dc7f87f2ab286c8352d6e6afb04c78d43e1b7a0'
    plans=[dict(r) for r in src.execute('SELECT '+','.join(FIELDS)+' FROM positions WHERE strategy IN (?,?,?) ORDER BY id',tuple(g.VARIANTS))]
    source_watermark=src.execute('SELECT coalesce(max(id),0) FROM positions').fetchone()[0]
    for plan in plans:
        try:
            row=src.execute('SELECT * FROM snapshots WHERE id=?',(plan['snapshot_id'],)).fetchone()
            if row is None:raise ValueError('SOURCE_SNAPSHOT_MISSING')
            snapshot={k:row[k] for k in ('id','symbol','bar_ms','observed_ms')}
            blob=row['raw'];where='HOT'
            archived=cold.execute('SELECT * FROM archived_snapshots WHERE id=?',(row['id'],)).fetchone()
            if archived is not None:
                if any(archived[k]!=row[k] for k in ('id','symbol','bar_ms','observed_ms')):raise ValueError('ARCHIVE_METADATA_MISMATCH')
                if hashlib.sha256(archived['raw']).hexdigest()!=archived['raw_sha256'] or len(archived['raw'])!=archived['raw_bytes']:
                    raise ValueError('ARCHIVE_BLOB_HASH_MISMATCH')
                if blob is not None and blob!=archived['raw']:raise ValueError('ARCHIVE_NON_NULL_CONFLICT')
                if blob is None:blob=archived['raw'];where='COLD'
            if blob is None:raise ValueError('ARCHIVED_RAW_UNAVAILABLE')
            if len(blob)>2*1024**2:raise ValueError('OVERSIZED_SOURCE')
            decoder=zlib.decompressobj();unpacked=decoder.decompress(blob,8*1024**2+1)
            if len(unpacked)>8*1024**2 or not decoder.eof or decoder.unused_data:raise ValueError('INVALID_SOURCE_PAYLOAD')
            raw=json.loads(unpacked)
            features=g.extract(plan,snapshot,raw);decision=g.evaluate(features)
            rows.append({'id':plan['id'],'plan':plan,'snapshot':snapshot,'features':features,
                         'decision':decision,'raw_blob_sha256':hashlib.sha256(blob).hexdigest(),'storage':where})
            locations[where]+=1
        except Exception as exc:
            errors.append({'id':plan['id'],'strategy':plan['strategy'],'reason':type(exc).__name__+':'+str(exc)})
result={'at_ms':int(time.time()*1000),'gate_code_sha256':hashlib.sha256((BASE/'diversified_v4_gates.py').read_bytes()).hexdigest(),
        'source_manifest_sha256':hashlib.sha256(manifest.encode()).hexdigest(),
        'source_watermark':source_watermark,
        'plans':len(plans),'rows':rows,'unknown':errors,'storage_counts':locations,
        'source_outcome_reads_denied_by_sqlite_authorizer':True,
        'mode':'EXPLORATORY_HISTORICAL_INPUTS_ONLY','orders_possible':False}
payload=(json.dumps(result,sort_keys=True,separators=(',',':'))+'\n').encode()
(BASE/'historical_features.json').write_bytes(payload)
print(payload.decode(),end='')
