"""One-time isolated V3 rollout. Run on VPS after reviewing packaged hashes.

Never clears a DB or overwrites a frozen study. Only the controller UI restarts.
Failure restores the previous UI and retains the failed new study for inspection.
"""
import hashlib,json,os,shutil,sqlite3,subprocess,time,zipfile
from contextlib import closing
from datetime import datetime,timezone,timedelta
from pathlib import Path

ROOT=Path('/root/bot_v2_1');BASE=Path('/root/diversified_v3_deploy_20261019')
SOURCE=Path('/root/bot_diversified_shadow');LAB=Path('/root/bot_diversified_v3')
PAGES=['paper_pilots_api.py','dashboard_v2.html']
UNITS=['diversified-v3-shadow.service','diversified-v3-shadow.timer']
SERVICES=['trading-bot-v2-1','paper-monitor-v2-1','bot-control-v2-1','mainnet-paper-lab','mainnet-early-paper',
          'sqz-candidate-observer','btc-rs-paper','sqz-direction-paper','sqz-confirmation-paper']
FROZEN=['bot_mainnet_paper','bot_mainnet_early','bot_btc_rs_paper','bot_sqz_direction_paper','bot_sqz_confirmation_paper',
        'bot_sqz_promotion_paper','bot_candidate_observer','bot_candidate_outcomes','bot_runner_comparison',
        'bot_diversified_shadow','bot_sqz_c2_high_paper']
def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def service(name):
    result=subprocess.check_output(['systemctl','show',name,'-p','MainPID','-p','NRestarts','-p','ActiveState'],text=True)
    return dict(line.split('=',1) for line in result.splitlines())


def audit():
    files={};manifests={}
    for root in [ROOT]+[Path('/root')/n/'code' for n in FROZEN]:
        for path in list(root.glob('*.py'))+list((root/'src').rglob('*.py'))+list(root.glob('*.yaml'))+list(root.glob('*settings.json')):
            if root==ROOT and path.name in PAGES:continue
            files[str(path)]=sha(path)
    for name in FROZEN:
        for path in (Path('/root')/name/'data').glob('*.sqlite3'):
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                columns={r[1] for r in db.execute('PRAGMA table_info(meta)')}
                if {'key','value'}<=columns:
                    row=db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
                    if row:manifests[str(path)]=hashlib.sha256(row[0].encode()).hexdigest()
    with closing(sqlite3.connect((ROOT/'data/trading_bot_v2_1.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        history={name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ('trades','shadow_trades')}
    return {'files':files,'manifests':manifests,'services':{n:service(n) for n in SERVICES},'main_history_rows':history}


def replace(src,dst):
    dst.parent.mkdir(parents=True,exist_ok=True);temp=dst.with_name('.'+dst.name+'.v3-stage')
    shutil.copy2(src,temp);os.chmod(temp,dst.stat().st_mode&0o777 if dst.exists() else 0o644);os.replace(temp,dst)


def main():
    stage=BASE/'stage';stage.mkdir(parents=True,exist_ok=True)
    assert not LAB.exists() and all(not (Path('/etc/systemd/system')/n).exists() for n in UNITS)
    assert shutil.disk_usage('/root').free>1024**3
    with zipfile.ZipFile(BASE/'deployment.zip') as z:
        hashes=json.loads(z.read('hashes.json'))
        assert set(z.namelist())==set(hashes)|{'hashes.json'}
        for name in z.namelist():
            assert not Path(name).is_absolute() and '..' not in Path(name).parts
            assert (stage/name).resolve().is_relative_to(stage.resolve())
        z.extractall(stage)
    assert all(sha(stage/name)==digest for name,digest in hashes.items())
    expected=json.loads((stage/'ui_before.json').read_text())
    assert all(sha(ROOT/n)==expected[n]['server_sha256'] for n in PAGES),'Live UI changed since preflight'
    before=audit();assert all(s['ActiveState']=='active' for s in before['services'].values())
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');backup=BASE/('backup_'+stamp);backup.mkdir(mode=0o700)
    (backup/'before.json').write_text(json.dumps(before,indent=2))
    for name in PAGES:shutil.copy2(ROOT/name,backup/name)
    # SQLite backup captures one consistent old series while its control continues.
    with closing(sqlite3.connect((SOURCE/'data/shadow.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=5)) as src:
        with closing(sqlite3.connect(backup/'previous_v2.sqlite3')) as dst:src.backup(dst,pages=128,sleep=.01)
    shutil.copytree(SOURCE/'code',backup/'previous_v2_code')
    shutil.copy2(SOURCE/'data/status.json',backup/'previous_v2_status.json')
    old_db=backup/'previous_v2.sqlite3'
    with closing(sqlite3.connect(old_db)) as c:
        old_meta=json.loads(c.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()[0])
        old_count=c.execute('SELECT count(*) FROM positions').fetchone()[0]
        assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    archive={'sha256':sha(old_db),'positions':old_count,'cohort':old_meta['cohort'],'path':str(old_db)}
    (backup/'archive.json').write_text(json.dumps(archive,indent=2))
    active=False
    try:
        LAB.mkdir(mode=0o700);shutil.copytree(stage/'code',LAB/'code');(LAB/'data').mkdir(mode=0o700)
        env={'PATH':'/usr/local/bin:/usr/bin:/bin','PYTHONPATH':str(LAB/'code/src'),'PYTHONDONTWRITEBYTECODE':'1'}
        subprocess.run([str(ROOT/'.venv/bin/python'),'-m','trading_bot.research.diversified_v3_shadow',
            '--code-root',str(LAB/'code'),'--data-dir',str(LAB/'data'),'--init-only'],env=env,check=True,timeout=45)
        initial=json.loads((LAB/'data/status.json').read_text())
        assert initial['health']=='OK' and len(initial['groups'])==6
        assert all(g['entries']==g['closed']==g['open']==0 for g in initial['groups'])
        assert initial['order_capability'] is False
        (backup/'initial_zero_status.json').write_text(json.dumps(initial,indent=2))
        subprocess.run(['systemd-analyze','verify',*[str(stage/'scripts'/n) for n in UNITS]],check=True,timeout=20)
        for name in UNITS:replace(stage/'scripts'/name,Path('/etc/systemd/system')/name)
        for name in PAGES:replace(stage/name,ROOT/name)
        active=True
        subprocess.run(['systemctl','daemon-reload'],check=True,timeout=20)
        subprocess.run(['systemctl','restart','bot-control-v2-1'],check=True,timeout=45)
        subprocess.run(['systemctl','enable','--now','diversified-v3-shadow.timer'],check=True,timeout=20)
        after=audit()
        assert after['files']==before['files'] and after['manifests']==before['manifests']
        assert all(after['main_history_rows'][k]>=v for k,v in before['main_history_rows'].items())
        assert all(after['services'][n]==before['services'][n] for n in SERVICES if n!='bot-control-v2-1')
        assert after['services']['bot-control-v2-1']['ActiveState']=='active'
        record={'backup':str(backup),'at_ms':int(time.time()*1000),'registered_ms':initial['registered_ms'],
            'started_local':datetime.fromtimestamp(initial['registered_ms']/1000,timezone(timedelta(hours=5))).isoformat(),
            'archive':archive,'before':before,'after':after,'package_hashes':hashes,'new_statistics_started_at_zero':True,
            'old_control_continues':True,'main_bot_not_restarted':True}
        (BASE/'deployment.json').write_text(json.dumps(record,indent=2))
        print(json.dumps({k:record[k] for k in ['backup','started_local','archive','new_statistics_started_at_zero','main_bot_not_restarted']}))
    except BaseException:
        subprocess.run(['systemctl','disable','--now','diversified-v3-shadow.timer'],check=False,timeout=20)
        subprocess.run(['systemctl','stop','diversified-v3-shadow.service'],check=False,timeout=110)
        for name in PAGES:replace(backup/name,ROOT/name)
        for name in UNITS:
            path=Path('/etc/systemd/system')/name
            if path.exists():path.unlink()
        subprocess.run(['systemctl','daemon-reload'],check=True,timeout=20)
        if active:subprocess.run(['systemctl','restart','bot-control-v2-1'],check=True,timeout=45)
        if LAB.exists():LAB.rename(BASE/('failed_lab_'+stamp))
        raise


if __name__=='__main__':main()
