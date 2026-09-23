import hashlib
import json
import sqlite3
import zlib
from types import SimpleNamespace

import pytest

from trading_bot.research.candidate_outcomes import (
    OutcomeStore,OutcomeLab,PROTOCOL,decode,validate_bars,report,readonly)
from trading_bot.research.mainnet_paper import canonical


MANIFEST={'settings':{'experiment':'sqz-candidate-observer-v1'}}


def payload(hour=3599999, observed=3601000, finished=3659999):
    source=f'SQZ:BTCUSDT:LONG:{hour}'
    return {'source_id':source,'observed_ms':observed,'finished_ms':finished,
            'signal':{'symbol':'BTCUSDT','direction':'LONG','entry_price':'100','stop_loss':'90',
                      'metadata':{'source_hour_close_time':hour,'squeeze_entry_timing':'early_breakout'}},
            'metrics':{'funding_rate':'0'},'liquidity_failures':['DEPTH5'],
            'gates':{'early':{'early_all_gate_failures':['RS_UNALIGNED']}},'impact':None,'errors':[]}


def row(p, i=1):
    raw=canonical(p).encode()
    return {'id':i,'source_id':p['source_id'],'observed_ms':p['observed_ms'],
            'payload_sha256':hashlib.sha256(raw).hexdigest(),'payload_zlib':zlib.compress(raw)}


def source(tmp_path, payloads):
    p=tmp_path/'source.sqlite3';db=sqlite3.connect(p)
    db.executescript('CREATE TABLE meta(key TEXT,value TEXT); CREATE TABLE observations(id INTEGER PRIMARY KEY,source_id TEXT,observed_ms INTEGER,payload_sha256 TEXT,payload_zlib BLOB);')
    db.execute('INSERT INTO meta VALUES(?,?)',('manifest',canonical(MANIFEST)))
    for i,payload_ in enumerate(payloads,1):
        r=row(payload_,i);db.execute('INSERT INTO observations VALUES(?,?,?,?,?)',tuple(r.values()))
    db.commit();db.close();return p


def store(tmp_path):
    return OutcomeStore(tmp_path/'out',{'protocol':PROTOCOL},started_ms=4000000)


def bar(t,o='100',h='101',l='99',c='100'):
    return [t,o,h,l,c,'1',t+59999,'100']


def test_entry_after_all_evidence_and_corrupt_identity():
    p=payload(finished=3661000)
    assert decode(row(p))['entry_ms']==3720000
    r=row(p);r['payload_sha256']='bad'
    with pytest.raises(ValueError,match='checksum'):decode(r)
    r=row(p);r['source_id']='other'
    with pytest.raises(ValueError,match='identity'):decode(r)


def test_first_only_incremental_restart_era_and_readonly(tmp_path):
    a=payload();b=payload(observed=3662000,finished=3663000);b['liquidity_failures']=[]
    c=payload(7199999,7201000,7202000)
    src=source(tmp_path,[a,b,c]);before=src.read_bytes();s=store(tmp_path)
    assert s.ingest(src,MANIFEST)==2
    rows=list(s.db.execute('SELECT * FROM labels ORDER BY observation_id'))
    assert len(rows)==2 and rows[0]['era']=='BACKFILL' and rows[1]['era']=='PROSPECTIVE'
    assert json.loads(rows[0]['plan'])['liquidity_failures']==['DEPTH5']
    s.db.close();s=store(tmp_path)
    assert s.ingest(src,MANIFEST)==0
    assert src.read_bytes()==before
    db=readonly(src)
    with pytest.raises(sqlite3.OperationalError):db.execute('DELETE FROM observations')
    db.close();s.db.close()
    with pytest.raises(ValueError,match='Frozen'):OutcomeStore(tmp_path/'out',{'changed':True})


def test_corrupt_import_rolls_back_checkpoint(tmp_path):
    src=source(tmp_path,[payload()]);db=sqlite3.connect(src)
    db.execute("UPDATE observations SET payload_sha256='bad'");db.commit();db.close()
    s=store(tmp_path)
    with pytest.raises(ValueError):s.ingest(src,MANIFEST)
    assert s.db.execute('SELECT count(*) FROM labels').fetchone()[0]==0
    assert s.db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0]=='0'
    s.db.close()


@pytest.mark.parametrize('raw', [[],[bar(60000)],[bar(0),bar(120000)],
    [bar(0,h='NaN')],[bar(0,h='90')]])
def test_missing_bad_and_noncontiguous_bars_rejected(raw):
    with pytest.raises(ValueError):validate_bars(raw,0,180000)


class Feed:
    def __init__(self, rows):self.rows=rows;self.evidence={};self.starts=[]
    async def klines(self,symbol,tf,limit,start_time,end_time):
        self.starts.append(start_time)
        return [b for b in self.rows if start_time<=b[0]<=end_time][:limit]


@pytest.mark.asyncio
async def test_gap_retry_does_not_shift_entry_or_use_partial_pnl(tmp_path):
    src=source(tmp_path,[payload()]);s=store(tmp_path);s.ingest(src,MANIFEST)
    r=s.db.execute('SELECT * FROM labels').fetchone();entry=r['entry_ms']
    feed=Feed([bar(entry+60000,h='140')]);lab=OutcomeLab(s,feed)
    with pytest.raises(ValueError,match='GAP'):await lab.update(r,entry+120000)
    assert s.db.execute('SELECT result FROM labels').fetchone()[0] is None
    feed.rows=[bar(entry),bar(entry+60000,h='125',c='120')]
    await lab.update(r,entry+120000)
    v=s.db.execute('SELECT * FROM labels').fetchone();result=json.loads(v['result'])
    assert v['status']=='CLOSED' and result['reason']=='targets' and result['model_pnl_usdt']>3
    assert result['entry_ms']==entry and feed.starts==[entry,entry]
    s.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('direction,stop', [('LONG','90'),('SHORT','110')])
async def test_ambiguous_bar_is_loss_both_directions(tmp_path,direction,stop):
    p=payload();p['signal']['direction']=direction;p['signal']['stop_loss']=stop
    p['source_id']=p['source_id'].replace(':LONG:',':'+direction+':')
    src=source(tmp_path,[p]);s=store(tmp_path);s.ingest(src,MANIFEST)
    r=s.db.execute('SELECT * FROM labels').fetchone();entry=r['entry_ms']
    await OutcomeLab(s,Feed([bar(entry,h='140',l='60')])).update(r,entry+60000)
    v=s.db.execute('SELECT * FROM labels').fetchone();result=json.loads(v['result'])
    assert result['reason']=='stop' and result['ambiguous_bars']==1
    assert result['model_pnl_usdt'] < -2
    s.db.close()


@pytest.mark.asyncio
async def test_open_not_realized_and_24h_timeout_restart(tmp_path):
    src=source(tmp_path,[payload()]);s=store(tmp_path);s.ingest(src,MANIFEST)
    r=s.db.execute('SELECT * FROM labels').fetchone();entry=r['entry_ms']
    feed=Feed([bar(entry+i*60000) for i in range(1440)])
    await OutcomeLab(s,feed).update(r,entry+2*60000)
    a=report(s.directory/'candidate_outcomes.sqlite3')
    assert a['all']['states']['OPEN']==1 and a['all']['closed_label_pnl_usdt']==0
    s.db.close();s=store(tmp_path);r=s.db.execute('SELECT * FROM labels').fetchone()
    await OutcomeLab(s,feed).update(r,entry+1440*60000)
    result=json.loads(s.db.execute('SELECT result FROM labels').fetchone()[0])
    assert result['status']=='CLOSED' and result['reason']=='timeout'
    assert feed.starts[1]==entry+2*60000
    assert result['last_ms']==entry+1440*60000-1
    s.db.close()


@pytest.mark.asyncio
async def test_wrong_side_next_minute_is_invalid_not_loss(tmp_path):
    src=source(tmp_path,[payload()]);s=store(tmp_path);s.ingest(src,MANIFEST)
    r=s.db.execute('SELECT * FROM labels').fetchone();entry=r['entry_ms']
    await OutcomeLab(s,Feed([bar(entry,o='80',h='85',l='75',c='80')])).update(r,entry+60000)
    assert s.db.execute('SELECT status FROM labels').fetchone()[0]=='INVALID_ENTRY'
    s.db.close()


def test_report_clusters_and_overlapping_filter_counts(tmp_path):
    src=source(tmp_path,[payload(),payload(7199999,7201000,7202000)])
    s=store(tmp_path);s.ingest(src,MANIFEST)
    with s.db:s.db.execute('UPDATE labels SET status=?,result=?',('CLOSED',canonical({'model_pnl_usdt':3})))
    r=report(s.directory/'candidate_outcomes.sqlite3')
    assert r['all']['labels']==2 and r['first_24h_per_symbol_direction']['labels']==1
    assert r['all']['filters']['DEPTH5']['blocked_wins']==2
    assert r['all']['filters']['RS_UNALIGNED']['blocked_wins']==2
    assert r['by_era']['BACKFILL']['labels']==1 and r['by_era']['PROSPECTIVE']['labels']==1
    s.db.close()
