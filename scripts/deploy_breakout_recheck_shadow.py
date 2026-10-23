"""One-time isolated shadow rollout; never resets or restarts source studies.

Expects deployment.zip under BASE with SHA256 hashes.json, ui_before.json, and
git_revision.txt. Failure restores the three controller files and retains the
new study for inspection. Only the dashboard controller is restarted.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import zipfile
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path('/root/bot_v2_1')
BASE = Path('/root/breakout_recheck_deploy_20261023')
LAB = Path('/root/bot_breakout_recheck_shadow')
SOURCE = Path('/root/bot_mainnet_paper/data/mainnet_paper_lab.sqlite3')
PAGES = ('paper_pilots_api.py', 'bot_control_v2.py', 'dashboard_v2.html')
UNITS = ('breakout-recheck-shadow.service', 'breakout-recheck-shadow.timer')
SERVICES = ('trading-bot-v2-1', 'paper-monitor-v2-1', 'mainnet-paper-lab', 'bot-control-v2-1')
FROZEN = ('bot_mainnet_paper', 'bot_mainnet_early', 'bot_btc_rs_paper',
          'bot_sqz_direction_paper', 'bot_sqz_confirmation_paper', 'bot_sqz_promotion_paper',
          'bot_candidate_observer', 'bot_candidate_outcomes', 'bot_runner_comparison',
          'bot_diversified_shadow', 'bot_diversified_v3', 'bot_sqz_c2_high_paper')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def service(name):
    raw = subprocess.check_output(['systemctl', 'show', name, '-p', 'MainPID',
                                   '-p', 'NRestarts', '-p', 'ActiveState'], text=True)
    return dict(line.split('=', 1) for line in raw.splitlines())


def readonly(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=4)
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA cache_size=-1024')
    return db


def audit():
    files = {}; manifests = {}; populations = {}
    for root in (ROOT, *(Path('/root') / n / 'code' for n in FROZEN)):
        paths = (list(root.glob('*.py')) + list((root / 'src').rglob('*.py')) +
                 list(root.glob('*.yaml')) + list(root.glob('*settings.json')))
        for p in paths:
            if root == ROOT and p.name in PAGES:
                continue
            files[str(p)] = sha(p)
    for name in FROZEN:
        for p in (Path('/root') / name / 'data').glob('*.sqlite3'):
            with closing(readonly(p)) as db:
                columns = {r[1] for r in db.execute('PRAGMA table_info(meta)')}
                if {'key', 'value'} <= columns:
                    row = db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
                    if row:
                        manifests[str(p)] = hashlib.sha256(row[0].encode()).hexdigest()
    with closing(readonly(ROOT / 'data/trading_bot_v2_1.sqlite3')) as db:
        populations = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0]
                       for t in ('trades', 'shadow_trades')}
    return {'files': files, 'manifests': manifests, 'main_history_rows': populations,
            'services': {n: service(n) for n in SERVICES}}


def replace(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    temporary = dst.with_name('.' + dst.name + '.breakout-stage')
    shutil.copy2(src, temporary)
    os.chmod(temporary, dst.stat().st_mode & 0o777 if dst.exists() else 0o644)
    os.replace(temporary, dst)


def controller_get(route):
    pid = service('bot-control-v2-1')['MainPID']
    environment = dict(item.split(b'=', 1) for item in
                       Path('/proc/' + pid + '/environ').read_bytes().split(b'\0') if b'=' in item)
    port = environment.get(b'BOT_CONTROL_PORT', b'8890').decode()
    token = environment.get(b'BOT_CONTROL_TOKEN', b'').decode()
    request = urllib.request.Request('http://127.0.0.1:' + port + route)
    if token:
        request.add_header('X-Bot-Control-Token', token)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


def main():
    stage = BASE / 'stage'
    assert not LAB.exists(), 'A study already exists: refusing to overwrite or reset it'
    assert all(not (Path('/etc/systemd/system') / n).exists() for n in UNITS)
    assert shutil.disk_usage('/root').free > 1024 ** 3
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BASE / 'deployment.zip') as archive:
        hashes = json.loads(archive.read('hashes.json'))
        assert set(archive.namelist()) == set(hashes) | {'hashes.json'}
        for name in archive.namelist():
            p = Path(name)
            assert not p.is_absolute() and '..' not in p.parts
            assert (stage / name).resolve().is_relative_to(stage.resolve())
        archive.extractall(stage)
    assert all(sha(stage / name) == value for name, value in hashes.items())
    expected = json.loads((stage / 'ui_before.json').read_text())
    assert all(sha(ROOT / name) == expected[name] for name in PAGES), 'UI changed after preflight'
    before = audit()
    assert all(s['ActiveState'] == 'active' for s in before['services'].values())
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = BASE / ('backup_' + stamp)
    backup.mkdir(mode=0o700)
    (backup / 'before.json').write_text(json.dumps(before, indent=2))
    for name in PAGES:
        shutil.copy2(ROOT / name, backup / name)
    ui_changed = False
    try:
        LAB.mkdir(mode=0o700)
        shutil.copytree(stage / 'code', LAB / 'code')
        (LAB / 'data').mkdir(mode=0o700)
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin',
               'PYTHONPATH': str(LAB / 'code/src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        subprocess.run(['/usr/bin/python3', '-m', 'trading_bot.research.breakout_recheck_shadow',
                        '--code-root', str(LAB / 'code'), '--source-db', str(SOURCE),
                        '--data-dir', str(LAB / 'data'), '--init-only'], env=env,
                       check=True, timeout=30)
        initial = json.loads((LAB / 'data/status.json').read_text())
        assert initial['health'] == 'OK' and initial['order_capability'] is False
        assert initial['counts']['sources'] == initial['counts']['closed_pairs'] == 0
        (backup / 'initial_zero_status.json').write_text(json.dumps(initial, indent=2))
        subprocess.run(['systemd-analyze', 'verify',
                        *(str(stage / 'scripts' / n) for n in UNITS)], check=True, timeout=20)
        # Exercise the exact unit's WAL/file protections using only synthetic data.
        checked = subprocess.run(['/usr/bin/python3', str(stage / 'scripts/check_breakout_recheck_feed_access.py'),
                                  '--unit', str(stage / 'scripts/breakout-recheck-shadow.service')],
                                 text=True, capture_output=True, timeout=120)
        (backup / 'wal_probe_stdout.json').write_text(checked.stdout)
        (backup / 'wal_probe_stderr.txt').write_text(checked.stderr)
        assert checked.returncode == 0, 'Disposable WAL sandbox regression failed'
        for name in UNITS:
            replace(stage / 'scripts' / name, Path('/etc/systemd/system') / name)
        for name in PAGES:
            replace(stage / name, ROOT / name)
        ui_changed = True
        subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=20)
        subprocess.run(['systemctl', 'restart', 'bot-control-v2-1'], check=True, timeout=40)
        last_error = None
        for _ in range(10):
            try:
                endpoint = json.loads(controller_get('/breakout-recheck-shadow'))
                assert endpoint['health'] == 'OK' and endpoint['order_capability'] is False
                assert endpoint['registered_ms'] == initial['registered_ms']
                page = controller_get('/dashboard').decode()
                assert 'breakout-recheck-shadow' in page
                break
            except Exception as exc:
                last_error = exc
                time.sleep(.5)
        else:
            raise RuntimeError('Controller verification failed: ' + type(last_error).__name__)
        (backup / 'api_initial_status.json').write_text(json.dumps(endpoint, indent=2))
        subprocess.run(['systemctl', 'enable', '--now', 'breakout-recheck-shadow.timer'],
                       check=True, timeout=20)
        after = audit()
        assert after['files'] == before['files'] and after['manifests'] == before['manifests']
        assert all(after['main_history_rows'][k] >= v for k, v in before['main_history_rows'].items())
        assert all(after['services'][n] == before['services'][n] for n in SERVICES
                   if n != 'bot-control-v2-1')
        assert after['services']['bot-control-v2-1']['ActiveState'] == 'active'
        record = {'at_ms': int(time.time()*1000), 'backup': str(backup),
                  'git_revision': (stage / 'git_revision.txt').read_text().strip(),
                  'registered_ms': initial['registered_ms'],
                  'started_local': datetime.fromtimestamp(initial['registered_ms']/1000,
                      timezone(timedelta(hours=5))).isoformat(),
                  'before': before, 'after': after, 'package_hashes': hashes,
                  'new_statistics_started_at_zero': True, 'existing_controls_unchanged': True,
                  'main_bot_and_monitor_not_restarted': True}
        (BASE / 'deployment.json').write_text(json.dumps(record, indent=2))
        print(json.dumps({k: record[k] for k in ('started_local', 'backup', 'git_revision',
                                                'new_statistics_started_at_zero',
                                                'main_bot_and_monitor_not_restarted')}))
    except BaseException:
        subprocess.run(['systemctl', 'disable', '--now', 'breakout-recheck-shadow.timer'],
                       check=False, timeout=20)
        subprocess.run(['systemctl', 'stop', 'breakout-recheck-shadow.service'],
                       check=False, timeout=110)
        for name in PAGES:
            replace(backup / name, ROOT / name)
        for name in UNITS:
            path = Path('/etc/systemd/system') / name
            if path.exists():
                path.unlink()
        subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=20)
        if ui_changed:
            subprocess.run(['systemctl', 'restart', 'bot-control-v2-1'], check=True, timeout=40)
        if LAB.exists():
            LAB.rename(BASE / ('failed_lab_' + stamp))
        raise


if __name__ == '__main__':
    main()
