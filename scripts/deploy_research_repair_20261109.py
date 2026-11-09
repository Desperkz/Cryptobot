"""Verified cold-storage repair and isolated prospective SQZ quality rollout."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

BASE = Path('/root/research_repair_deploy_20261109')
ROOT = Path('/root/bot_v2_1')
OPS = Path('/root/bot_research_storage')
LAB = Path('/root/bot_sqz_entry_quality_shadow')
PAGES = ('bot_control_v2.py', 'paper_pilots_api.py', 'dashboard_v2.html')
UNITS = ('research-storage-maintenance.service', 'research-storage-maintenance.timer',
         'sqz-entry-quality-shadow.service', 'sqz-entry-quality-shadow.timer')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args, timeout=30):
    return subprocess.run(list(args), check=True, timeout=timeout)


def main():
    stage = BASE/'stage'
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BASE/'deployment.zip') as z:
        hashes = json.loads(z.read('hashes.json'))
        assert set(z.namelist()) == set(hashes)|{'hashes.json'}
        for name in z.namelist():
            p = Path(name)
            assert not p.is_absolute() and '..' not in p.parts
            assert (stage/p).resolve().is_relative_to(stage.resolve())
        z.extractall(stage)
    assert all(sha(stage/n) == digest for n, digest in hashes.items())
    spec = importlib.util.spec_from_file_location('verified_helpers', stage/'scripts/deploy_breakout_recheck_shadow.py')
    util = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(util)
    expected = json.loads((stage/'ui_before.json').read_text())
    assert all(sha(ROOT/n) == expected[n] for n in PAGES), 'Live UI changed since preflight'
    resuming = '--resume' in sys.argv
    if resuming:
        assert (BASE/'ui_backup/before.json').exists()
        assert OPS.exists() and LAB.exists()
        assert not list((LAB/'data').glob('*.sqlite3')), 'Never reset a registered quality series'
        assert sha(OPS/'code/research_storage.py') == hashes['operations/research_storage.py']
    else:
        assert not LAB.exists() and not OPS.exists(), 'Never overwrite an existing study/archive'
        assert all(not (Path('/etc/systemd/system')/n).exists() for n in UNITS)
    assert shutil.disk_usage('/root').free > 1536*1024**2
    before = util.audit()
    assert all(s['ActiveState'] == 'active' for s in before['services'].values())
    backup = BASE/'ui_backup'
    backup.mkdir(mode=0o700, exist_ok=resuming)
    if not resuming:
        (backup/'before.json').write_text(json.dumps(before, indent=2))
    for n in PAGES:
        shutil.copy2(ROOT/n, backup/n)
    OPS.mkdir(mode=0o700, exist_ok=resuming)
    shutil.copytree(stage/'operations', OPS/'code', dirs_exist_ok=resuming)
    for name in ('data', 'archives', 'backups', 'temporary', 'reports'):
        (OPS/name).mkdir(mode=0o700, exist_ok=resuming)
    LAB.mkdir(mode=0o700, exist_ok=resuming)
    shutil.copytree(stage/'quality', LAB/'code', dirs_exist_ok=resuming)
    (LAB/'data').mkdir(mode=0o700, exist_ok=resuming)
    run('systemd-analyze', 'verify', *(str(stage/'scripts'/n) for n in UNITS))
    for n in UNITS:
        util.replace(stage/'scripts'/n, Path('/etc/systemd/system')/n)
    run('systemctl', 'daemon-reload')
    run('/usr/bin/python3', str(stage/'scripts/check_research_storage_mounts.py'),
        '/etc/systemd/system/research-storage-maintenance.service', timeout=100)
    storage_ok = False
    ui_changed = False
    try:
        # Execute initial repair inside the permanent unit's exact sandbox.
        # Only this invocation requests full restore proofs and both cohorts.
        override = Path('/run/systemd/system/research-storage-maintenance.service.d/initial.conf')
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text('[Service]\nCPUQuota=25%\nTimeoutStartSec=1800\nExecStart=\nExecStart=/usr/bin/flock -n /run/research-storage-maintenance/lock /usr/bin/python3 /root/bot_research_storage/code/research_storage_maintenance.py --force --verify-restore\n')
        try:
            run('systemctl', 'daemon-reload')
            print(json.dumps({'stage': 'storage_repair_started'}), flush=True)
            run('systemctl', 'start', 'research-storage-maintenance.service', timeout=1000)
        finally:
            override.unlink()
            run('systemctl', 'set-property', '--runtime', 'research-storage-maintenance.service', 'CPUQuota=10%')
            run('systemctl', 'daemon-reload')
        storage_status = json.loads((OPS/'data/status.json').read_text())
        assert storage_status['health'] == 'OK'
        storage_report = json.loads(Path(storage_status['report']).read_text())
        assert storage_report['ok'] and len(storage_report['operations']) == 2
        assert all(op['restore_proof']['ok'] for op in storage_report['operations'])
        storage_ok = True
        (backup/'storage_status.json').write_text(json.dumps(storage_status, indent=2))
        (backup/'storage_proof.json').write_text(json.dumps(storage_report, indent=2))
        print(json.dumps({'stage': 'storage_repair_verified', 'labs': storage_status['labs']}), flush=True)
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONPATH': str(LAB/'code/src')}
        subprocess.run(['/usr/bin/python3', '-m', 'trading_bot.research.sqz_entry_quality_shadow',
            '--source-db', '/root/bot_mainnet_paper/data/mainnet_paper_lab.sqlite3',
            '--code-root', str(LAB/'code'), '--data-dir', str(LAB/'data'), '--init-only'],
            env=env, check=True, timeout=30)
        initial = json.loads((LAB/'data/status.json').read_text())
        assert initial['health'] == 'OK' and initial['counts']['sources'] == 0
        assert initial['counts']['closed_pairs'] == 0 and initial['order_capability'] is False
        (backup/'initial_quality_status.json').write_text(json.dumps(initial, indent=2))
        checked = subprocess.run(['/usr/bin/python3', str(stage/'scripts/check_sqz_entry_quality_feed_access.py'),
            '--unit', '/etc/systemd/system/sqz-entry-quality-shadow.service'],
            text=True, capture_output=True, timeout=150)
        (backup/'quality_wal_probe.json').write_text(checked.stdout)
        (backup/'quality_wal_probe_stderr.txt').write_text(checked.stderr)
        assert checked.returncode == 0, 'Isolated quality WAL sandbox probe failed'
        for n in PAGES:
            util.replace(stage/n, ROOT/n)
        ui_changed = True
        run('systemctl', 'restart', 'bot-control-v2-1', timeout=45)
        for attempt in range(20):
            try:
                api = json.loads(util.controller_get('/sqz-entry-quality-shadow'))
                archive_api = json.loads(util.controller_get('/research-storage'))
                assert api['health'] == 'OK' and api['registered_ms'] == initial['registered_ms']
                assert archive_api['health'] == 'OK'
                page = util.controller_get('/dashboard').decode()
                assert 'quality-card' in page
                break
            except Exception:
                if attempt == 19:
                    raise
                time.sleep(.5)
        run('systemctl', 'enable', '--now', 'research-storage-maintenance.timer', 'sqz-entry-quality-shadow.timer')
        after = util.audit()
        assert before['files'] == after['files'] and before['manifests'] == after['manifests']
        assert all(after['services'][n] == before['services'][n] for n in before['services']
                   if n != 'bot-control-v2-1')
        assert all(after['main_history_rows'][k] >= count for k, count in before['main_history_rows'].items())
        result = {'at_ms': int(time.time()*1000), 'git_revision': (stage/'git_revision.txt').read_text().strip(),
                  'before': before, 'after': after, 'package_hashes': hashes,
                  'storage_status': storage_status, 'quality_initial_status': initial,
                  'all_archive_restore_proofs_passed': True,
                  'main_and_monitor_not_restarted': True, 'old_manifests_unchanged': True}
        (BASE/'deployment.json').write_text(json.dumps(result, indent=2))
        print(json.dumps({'stage': 'deployment_verified', 'git_revision': result['git_revision'],
              'quality_registered_ms': initial['registered_ms'],
              'all_archive_restore_proofs_passed': True, 'main_and_monitor_not_restarted': True}), flush=True)
    except BaseException:
        subprocess.run(['systemctl', 'disable', '--now', 'sqz-entry-quality-shadow.timer'], check=False)
        subprocess.run(['systemctl', 'stop', 'sqz-entry-quality-shadow.service'], check=False)
        if ui_changed:
            for n in PAGES:
                util.replace(backup/n, ROOT/n)
            run('systemctl', 'restart', 'bot-control-v2-1', timeout=45)
        # Never replace restored research DBs after their timers have resumed:
        # they may contain new entries. Keep the verified archives/ops root.
        if storage_ok:
            run('systemctl', 'enable', '--now', 'research-storage-maintenance.timer')
        raise


if __name__ == '__main__':
    main()
