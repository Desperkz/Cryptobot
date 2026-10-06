import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import runner_comparison as study

NOW=1791288000000


def source_row(direction='LONG'):
    prices=['110','116','122'] if direction=='LONG' else ['90','84','78']
    targets=[{'name':name,'price':price,'quantity':qty,'fraction':f,'reward_risk':r,
        'move_stop_to_breakeven':be,'activate_trailing':tr} for name,price,qty,f,r,be,tr in zip(
        ('TP1','TP2','RUNNER'),prices,('2.5','3.5','4'),('.25','.35','.4'),('1','1.6','2.2'),(True,False,False),(False,True,True))]
    return {'id':2,'created_at':'2026-10-06 12:00:00','mode':'PAPER_TRADING','symbol':'BTCUSDT',
        'direction':direction,'entry_price':'100','stop_loss':'90' if direction=='LONG' else '110',
        'take_profit':'124' if direction=='LONG' else '76','quantity':'10','risk_amount':'100',
        'metadata':json.dumps({'original_stop_loss':'90' if direction=='LONG' else '110',
            'partial_take_profits':targets,'signal_metadata':{'strategy':'SQUEEZE_BREAKOUT',
            'exit_profile_signature':study.PROFILE,'atr_pct':'10','funding_rate':'.0001'}})}


def plan(direction='LONG'):
    return study.make_plan(source_row(direction),NOW-100000000)


def bar(at,o,h,l,c):
    return [at,str(o),str(h),str(l),str(c),'1',at+study.MINUTE-1]


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_arm_identity_before_runner_and_treatment_has_no_other_fixed_cap(direction,monkeypatch):
    monkeypatch.setattr(study.monitor,'TRAILING_CALLBACK_MAX_PCT',study.number('30'))
    p=plan(direction);states={a:study.new_state(p) for a in study.ARMS}
    first=bar(p['start_ms'],100,117,99,116) if direction=='LONG' else bar(p['start_ms'],100,101,83,84)
    for arm in study.ARMS:study.step(p,states[arm],first,arm)
    assert states['CURRENT_PROFILE']==states['RUNNER_TRAILING']
    second=bar(p['start_ms']+study.MINUTE,124,135,124,130) if direction=='LONG' else bar(p['start_ms']+study.MINUTE,70,71,65,70)
    study.validate_bar(second,p['start_ms']+study.MINUTE)
    for arm in study.ARMS:study.step(p,states[arm],second,arm)
    assert states['CURRENT_PROFILE']['status']=='CLOSED'
    assert states['RUNNER_TRAILING']['status']=='OPEN' and states['RUNNER_TRAILING']['remaining']=='4.0'
    assert states['CURRENT_PROFILE']['filled']==['TP1','TP2','RUNNER']
    assert states['RUNNER_TRAILING']['filled']==['TP1','TP2']


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_initial_stop_gap_is_same_for_both_and_not_filled_at_fictitious_stop(direction):
    p=plan(direction);states={a:study.new_state(p) for a in study.ARMS}
    b=bar(p['start_ms'],85,89,80,86) if direction=='LONG' else bar(p['start_ms'],115,120,111,116)
    for arm in study.ARMS:study.step(p,states[arm],b,arm)
    assert states['CURRENT_PROFILE']==states['RUNNER_TRAILING']
    assert states['CURRENT_PROFILE']['events'][0]['trigger']==('85' if direction=='LONG' else '115')


def test_target_and_initial_stop_same_bar_uses_stop_first():
    p=plan();s=study.new_state(p)
    study.step(p,s,bar(p['start_ms'],100,130,89,120),'RUNNER_TRAILING')
    assert s['status']=='CLOSED' and s['filled']==[] and s['reason']=='STOP'


def test_funding_and_fees_match_frozen_monitor_not_double_counted():
    p=plan();s=study.new_state(p);when=p['opened_ms']+8*3600000
    study.close_piece(p,s,'10','110',when,'STOP','FINAL')
    ex=study.monitor._execution_pnl('LONG',study.number(100),study.number(110),study.number(10),
        opened_at=study.monitor._parse_timestamp('2026-10-06 12:00:00'),
        closed_at=study.monitor.datetime.fromtimestamp(when/1000,study.monitor.timezone.utc),funding_rate_per_8h=study.number('.0001'))
    assert study.number(s['costs']['net_pnl'])==ex.net_pnl
    assert study.number(s['costs']['funding_cost'])==study.number('.1')


def test_source_rejects_wrong_profile_and_prior_timestamp_without_mutating_input():
    row=source_row();before=copy.deepcopy(row)
    p=study.make_plan(row,NOW-100000000)
    assert row==before and p['start_ms']>p['opened_ms']
    with pytest.raises(ValueError,match='PRE_REGISTRATION'):study.make_plan(row,p['opened_ms']+1)
    meta=json.loads(row['metadata']);meta['signal_metadata']['exit_profile_signature']='other';row['metadata']=json.dumps(meta)
    with pytest.raises(ValueError,match='DIFFERENT_EXIT_PROFILE'):study.make_plan(row,NOW-100000000)


def test_missing_original_stop_after_modification_is_rejected():
    row=source_row();m=json.loads(row['metadata']);m.pop('original_stop_loss');m['filled_partial_targets']=['TP1'];row['metadata']=json.dumps(m)
    with pytest.raises(ValueError,match='ORIGINAL_STOP_MISSING'):study.make_plan(row,NOW-100000000)


def test_closed_bar_validation_rejects_gaps_and_bad_ohlc():
    for b in (bar(NOW+60000,100,101,99,100),bar(NOW,100,99,101,100),bar(NOW,100,'NaN',99,100)):
        with pytest.raises((ValueError,ArithmeticError)):study.validate_bar(b,NOW)


def test_common_horizon_forces_finite_exit_without_runner_target():
    p=plan();s=study.new_state(p)
    study.step(p,s,bar(p['end_ms']-study.MINUTE,100,100.5,99.5,100),'RUNNER_TRAILING')
    assert s['status']=='CLOSED' and s['reason']=='HORIZON_72H'


def make_source(path,rows):
    conn=sqlite3.connect(path);conn.execute('CREATE TABLE trades(id INTEGER PRIMARY KEY,created_at TEXT,mode TEXT,symbol TEXT,direction TEXT,entry_price TEXT,stop_loss TEXT,take_profit TEXT,quantity TEXT,risk_amount TEXT,metadata TEXT,closed_at TEXT,status TEXT,realized_pnl TEXT)')
    for row in rows:
        row={**row,'closed_at':None,'status':'ACCEPTED','realized_pnl':'0'}
        conn.execute('INSERT INTO trades VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
    conn.commit();conn.close()


def test_prospective_watermark_and_read_only_source_and_completed_pair_accounting(tmp_path):
    source=tmp_path/'source.sqlite3';make_source(source,[{**source_row(),'id':1}])
    settings={'monitor_sha256':hashlib.sha256(Path(study.monitor.__file__).read_bytes()).hexdigest()}
    db,protocol=study.connect_store(tmp_path/'study',settings,source,1)
    assert protocol['source_max_id']==1
    before=hashlib.sha256(source.read_bytes()).hexdigest();study.ingest(db,source,protocol)
    assert not db.execute('SELECT * FROM sources').fetchall()
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    with sqlite3.connect(source) as conn:
        row=source_row();conn.execute('INSERT INTO trades VALUES('+','.join('?' for _ in range(14))+')',tuple(row.values())+(None,'ACCEPTED','0'))
    before=hashlib.sha256(source.read_bytes()).hexdigest();study.ingest(db,source,protocol)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    r=db.execute('SELECT * FROM sources').fetchone();p,s=json.loads(r['plan']),json.loads(r['state'])
    for a in study.ARMS:study.step(p,s[a],bar(p['start_ms'],85,89,80,86),a)
    with db:db.execute('UPDATE sources SET state=?',(study.canonical(s),))
    result=study.report(db,protocol,1000)
    assert result['complete_pairs']==1 and result['paired_delta_usdt']==0 and not result['review_ready']
    assert all(g['closed']==1 and g['open']==0 for g in result['groups'])
    assert result['paired_groups']['CURRENT_PROFILE']==result['paired_groups']['RUNNER_TRAILING']
    db.close()


def test_source_readonly_connection_cannot_write(tmp_path):
    source=tmp_path/'source.sqlite3';make_source(source,[])
    conn=study.source_connection(source)
    with pytest.raises(sqlite3.OperationalError):conn.execute('DELETE FROM trades')
    conn.close()


def test_saved_status_api_freshness_and_fail_closed(tmp_path):
    from paper_pilots_api import api_runner_comparison
    path=tmp_path/'status.json'
    assert api_runner_comparison(path,now_ms=NOW)['health']=='UNAVAILABLE'
    data={'mode':'LOCAL_PAPER_ONLY','order_capability':False,'health':'OK','generated_at_ms':NOW,
        'protocol':{'name':study.RULE['name']}}
    path.write_text(json.dumps(data))
    before=path.read_bytes()
    assert api_runner_comparison(path,now_ms=NOW)['health']=='OK'
    assert api_runner_comparison(path,now_ms=NOW+901000)['health']=='STALE'
    assert path.read_bytes()==before
    path.write_text('{"mode":NaN}')
    assert api_runner_comparison(path,now_ms=NOW)['health']=='UNAVAILABLE'
    data['mode']='LIVE';path.write_text(json.dumps(data))
    assert api_runner_comparison(path,now_ms=NOW)['health']=='UNAVAILABLE'


def test_controller_has_read_only_comparison_route():
    import bot_control_v2
    assert '/runner-comparison' in bot_control_v2.ROUTES
    assert not any('runner' in route for route in bot_control_v2.POST_ROUTES)


def test_cycle_does_not_skip_missing_minute_or_touch_source_or_order_functions(tmp_path,monkeypatch):
    import httpx
    source=tmp_path/'source.sqlite3';make_source(source,[{**source_row(),'id':1}])
    opened=study.stamp(source_row()['created_at']);clock=[(opened-1000)/1000]
    monkeypatch.setattr(study.time,'time',lambda:clock[0])
    config=tmp_path/'config.yaml';config.write_text('frozen')
    settings={'monitor_sha256':hashlib.sha256(Path(study.monitor.__file__).read_bytes()).hexdigest(),
        'source_monitor':study.monitor.__file__,'source_config':str(config),
        'config_sha256':hashlib.sha256(config.read_bytes()).hexdigest(),'venue':'https://demo-fapi.binance.com','environment':{}}
    study.run(source,tmp_path/'data',settings,init_only=True)
    with sqlite3.connect(source) as conn:
        conn.execute('INSERT INTO trades VALUES('+','.join('?' for _ in range(14))+')',tuple(source_row().values())+(None,'ACCEPTED','0'))
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    for name in ('_connect_db','get_open_positions','close_position','close_partial_target'):
        monkeypatch.setattr(study.monitor,name,lambda *args,**kwargs:pytest.fail('Original monitor mutation invoked'))
    client_type=httpx.Client;calls=[];start=(opened//study.MINUTE+1)*study.MINUTE
    def handler(request):
        calls.append(request)
        assert request.method=='GET' and request.url.host=='demo-fapi.binance.com'
        assert not request.headers.get('X-MBX-APIKEY')
        return httpx.Response(200,json=[bar(start,100,101,99,100),bar(start+2*study.MINUTE,100,101,99,100)])
    monkeypatch.setattr(study.httpx,'Client',lambda **kwargs:client_type(**kwargs,transport=httpx.MockTransport(handler)))
    clock[0]=(start+3*study.MINUTE)/1000
    result=study.run(source,tmp_path/'data',settings)
    assert result['health']=='DEGRADED' and result['complete_pairs']==0
    assert result['recent_sources'][0]['processed_through_ms']==start+study.MINUTE
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    assert len(calls)==1
