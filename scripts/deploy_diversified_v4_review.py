"""Server-side reversible publication of a rejected historical V4 review only."""
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import zipfile

BASE=Path('/root/diversified_v4_review_deploy_20261122')
MAIN=Path('/root/bot_v2_1')
DOC='docs/research/diversified_v4_20261122/'
FILES=('bot_control_v2.py','dashboard_v2.html','diversified_v4_review_api.py',DOC+'summary.json',DOC+'report.html')
SERVICES=('trading-bot-v2-1','paper-monitor-v2-1','mainnet-paper-lab','market-evidence-collector.service','bot-control-v2-1')


def cmd(*args):return subprocess.check_output(args,text=True,stderr=subprocess.STDOUT,timeout=30).strip()
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def ro(path):
    c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)
    c.execute('PRAGMA query_only=ON');c.execute('PRAGMA cache_size=-512');c.execute('BEGIN')
    c.set_progress_handler(lambda until=time.monotonic()+3:int(time.monotonic()>until),1000)
    return c


def snapshot():
    frozen={}
    for p in Path('/root').glob('bot_*/data/*.sqlite3'):
        with closing(ro(p)) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='meta' AND type='table'").fetchone():continue
            rows=list(c.execute("SELECT key,value FROM meta WHERE key='manifest' OR key LIKE 'registration%' ORDER BY key"))
            if rows:frozen[str(p)]=digest(rows)
    admissions={}
    for name,fields in (
        ('range_oi_flow','id,opened_ms,plan,plan_sha256,snapshot,decision,saved_ms'),
        ('range_profit_protection','id,opened_ms,plan,plan_sha256,snapshot,admission,reason,saved_ms')):
        with closing(ro(Path('/root/bot_'+name+'/data/'+name+'.sqlite3'))) as c:
            admissions[name]={str(r[0]):digest(list(r)) for r in c.execute('SELECT '+fields+' FROM pairs ORDER BY id')}
    with closing(ro(Path('/root/bot_diversified_v3/data/shadow.sqlite3'))) as c:
        original=list(c.execute('SELECT * FROM positions WHERE id<=300 ORDER BY id'))
        closed=c.execute("SELECT count(*) FROM positions WHERE id<=300 AND status='CLOSED'").fetchone()[0]
        assert len(original)==closed==300
    services={n:cmd('systemctl','show',n,'-p','MainPID','-p','NRestarts','-p','ActiveState') for n in SERVICES}
    return {'frozen':frozen,'admissions':admissions,'original_300_closed_sha256':digest(original),'services':services}


def public_json(route):
    with urllib.request.urlopen('http://78.140.246.186:8888'+route,timeout=15) as r:
        return json.load(r)


def main():
    assert BASE.is_dir() and not (BASE/'stage').exists()
    stage=BASE/'stage';stage.mkdir(mode=0o700)
    with zipfile.ZipFile(BASE/'deployment.zip') as z:
        hashes=json.loads(z.read('hashes.json'));baseline=json.loads(z.read('baseline.json'))
        assert set(hashes)==set(FILES)
        assert set(z.namelist())==set(FILES)|{'hashes.json','baseline.json'}
        for name,value in hashes.items():
            assert not Path(name).is_absolute() and '..' not in Path(name).parts
            assert hashlib.sha256(z.read(name)).hexdigest()==value
        z.extractall(stage)
    assert set(baseline)=={'bot_control_v2.py','dashboard_v2.html'}
    for n,value in baseline.items():assert sha(MAIN/n)==value, 'SERVER_CODE_DRIFT: '+n
    for n in FILES:
        if n not in baseline:assert not (MAIN/n).exists(), 'EXISTING_ARTIFACT: '+n
    spec=importlib.util.spec_from_file_location('review_api',stage/'diversified_v4_review_api.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    expected=module.api_diversified_v4_review(stage/DOC/'summary.json')
    assert expected['health']=='OK' and expected['decision']=='NO_VARIANT_PASSED'
    assert len(expected['variants'])==3 and not expected['live_variants']
    for n in ('bot_control_v2.py','diversified_v4_review_api.py'):
        compile((stage/n).read_text(encoding='utf-8'),n,'exec')
    before=snapshot();(BASE/'before.json').write_text(json.dumps(before,indent=2))
    backup=BASE/'backup';backup.mkdir(mode=0o700)
    for n in baseline:shutil.copy2(MAIN/n,backup/n)
    installed=[]
    try:
        for n in FILES:
            target=MAIN/n;assert target.resolve().is_relative_to(MAIN)
            target.parent.mkdir(parents=True,exist_ok=True)
            temp=target.with_name('.'+target.name+'.v4-stage')
            shutil.copy2(stage/n,temp);os.replace(temp,target);installed.append(n)
        cmd('systemctl','restart','bot-control-v2-1')
        actual=None
        for attempt in range(12):
            try:
                actual=public_json('/diversified-v4-review')
                if actual==expected:break
            except (OSError,ValueError):pass
            time.sleep(.5)
        assert actual==expected,'PUBLIC_REVIEW_MISMATCH'
        health={r:public_json(r) for r in ('/status','/research-storage','/diversified-shadow','/range-oi-flow-shadow','/range-profit-protection-shadow')}
        assert health['/status']['bot']=='active'
        assert health['/range-profit-protection-shadow']['health']=='OK'
        assert health['/range-oi-flow-shadow']['health']=='OK'
        assert health['/diversified-shadow']['health']=='OK'
        after=snapshot()
        assert after['frozen']==before['frozen']
        assert after['original_300_closed_sha256']==before['original_300_closed_sha256']
        for name,old in before['admissions'].items():
            assert all(after['admissions'][name].get(k)==v for k,v in old.items())
        for n,value in before['services'].items():
            if n!='bot-control-v2-1':assert after['services'][n]==value,'TRADING_PROCESS_CHANGED: '+n
        assert cmd('systemctl','is-active','bot-control-v2-1')=='active'
        record={'passed':True,'at_ms':int(time.time()*1000),'hashes':hashes,'before':before,'after':after,
                'public_artifact_sha256':actual['artifact_sha256'],'old_frozen_studies_preserved':len(before['frozen']),
                'old_admissions_preserved':{k:len(v) for k,v in before['admissions'].items()},
                'old_closed_v3_positions_preserved':300,'new_trading_versions_started':0,
                'trading_bot_monitor_market_collector_not_restarted':True}
        (BASE/'deployment.json').write_text(json.dumps(record,indent=2))
        print(json.dumps({k:v for k,v in record.items() if k not in ('before','after','hashes')}))
    except BaseException:
        for n in installed:
            target=MAIN/n
            if n in baseline:shutil.copy2(backup/n,target)
            else:target.unlink()
        cmd('systemctl','restart','bot-control-v2-1')
        raise


if __name__=='__main__':main()
