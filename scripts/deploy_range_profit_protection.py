"""One-time isolated future-only RANGE exit rollout; no source study reset.

Run on Linux only, after placing deployment.zip under BASE. All package files
are SHA256 checked, a disposable systemd WAL probe precedes registration, and
only the dashboard controller may be restarted. Failures restore the UI and
retain any newly registered lab for inspection under BASE/failed_lab_*.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import time
import urllib.request
import zipfile


ROOT = Path('/root/bot_v2_1')
BASE = Path('/root/range_profit_protection_deploy_20261121')
LAB = Path('/root/bot_range_profit_protection')
SOURCE = Path('/root/bot_diversified_v3/data/shadow.sqlite3')
OI = Path('/root/bot_range_oi_flow/data/range_oi_flow.sqlite3')
PAGES = ('bot_control_v2.py', 'dashboard_v2.html', 'range_profit_protection_api.py')
UNITS = ('range-profit-protection-shadow.service', 'range-profit-protection-shadow.timer')
PROBE = 'check_range_profit_protection_feed_access.py'
SERVICES = ('trading-bot-v2-1', 'paper-monitor-v2-1', 'bot-control-v2-1',
            'mainnet-paper-lab', 'market-evidence-collector')
TIMERS = ('diversified-v3-shadow.timer', 'range-oi-flow-shadow.timer')
CODE_FILES = {
    'code/src/trading_bot/__init__.py',
    'code/src/trading_bot/research/__init__.py',
    *('code/src/trading_bot/research/' + name for name in (
        'range_profit_protection.py', 'range_profit_execution.py',
        'range_oi_flow_shadow.py', 'range_oi_flow_gate.py')),
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def service(name):
    raw = subprocess.check_output(['systemctl', 'show', name, '-p', 'MainPID',
                                  '-p', 'NRestarts', '-p', 'ActiveState'], text=True, timeout=10)
    return dict(line.split('=', 1) for line in raw.splitlines())


def readonly(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA cache_size=-512')
    db.execute('BEGIN')
    db.set_progress_handler(lambda end=time.monotonic()+8: int(time.monotonic() > end), 1000)
    return db


def resource_check(*, wait_seconds=30):
    deadline = time.monotonic()+wait_seconds
    while True:
        available = next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()
                         if line.startswith('MemAvailable:'))
        if available >= 80*1024**2 or time.monotonic() >= deadline:
            break
        time.sleep(1)
    free_disk = shutil.disk_usage('/root').free
    require(available >= 80*1024**2, 'Less than 80 MiB MemAvailable; rollout paused')
    require(free_disk >= 1024**3, 'Less than 1 GiB free disk; rollout paused')
    path = SOURCE.with_name('status.json')
    require(path.stat().st_size <= 128*1024, 'Source status too large')
    source = json.loads(path.read_text())
    age = int(time.time()*1000)-source['generated_at_ms']
    require(source.get('health') == 'OK' and 0 <= age <= 900000, 'Source not fresh and healthy')
    return {'available_memory_bytes': available, 'free_disk_bytes': free_disk, 'source_age_ms': age}


def oi_pairs():
    with closing(readonly(OI)) as db:
        result = {}
        for row in db.execute('SELECT id,opened_ms,plan,plan_sha256,snapshot,decision,saved_ms,control FROM pairs ORDER BY id'):
            immutable = {key: row[key] for key in ('id', 'opened_ms', 'plan', 'plan_sha256', 'snapshot', 'decision', 'saved_ms')}
            raw = json.dumps(immutable, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            result[str(row['id'])] = {'immutable_sha256': hashlib.sha256(raw).hexdigest(),
                'control_sha256': hashlib.sha256(row['control'].encode()).hexdigest() if row['control'] is not None else None}
        return result


def audit():
    files = {}
    for directory in (ROOT, *(path/'code' for path in sorted(Path('/root').glob('bot_*')) if path != LAB)):
        candidates = (list(directory.glob('*.py')) + list((directory/'src').rglob('*.py'))
            + list(directory.glob('*.yaml')) + list(directory.glob('*.yml'))
            + list(directory.glob('*settings.json')) + list(directory.glob('config*.json'))
            + list((directory/'scripts').rglob('*.py')) + list((directory/'scripts').rglob('*.conf')))
        for path in candidates:
            if path.is_file() and not (directory == ROOT and path.name in PAGES):
                files[str(path)] = sha(path)
    frozen = {}
    for path in sorted(Path('/root').glob('bot_*/data/*.sqlite3')):
        if path.parent.parent == LAB:
            continue
        with closing(readonly(path)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
                continue
            columns = {row[1] for row in db.execute('PRAGMA table_info(meta)')}
            if not {'key', 'value'} <= columns:
                continue
            values = {row['key']: hashlib.sha256(row['value'].encode()).hexdigest()
                      for row in db.execute("SELECT key,value FROM meta WHERE key='manifest' OR key LIKE 'registration%' ORDER BY key")}
            if values:
                frozen[str(path)] = values
    with closing(readonly(ROOT/'data/trading_bot_v2_1.sqlite3')) as db:
        populations = {table: db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                       for table in ('trades', 'shadow_trades')}
    return {'files': files, 'frozen': frozen, 'main_history_rows': populations,
            'oi_pairs': oi_pairs(), 'services': {name: service(name) for name in SERVICES},
            'timers': {name: service(name) for name in TIMERS}}


def check_preserved(before, after):
    require(after['files'] == before['files'], 'Existing trading/research code changed')
    require(after['frozen'] == before['frozen'], 'Existing manifest or registration changed')
    require(all(after['main_history_rows'][key] >= value for key, value in before['main_history_rows'].items()),
            'Main trade history decreased')
    for name in SERVICES:
        if name != 'bot-control-v2-1':
            require(after['services'][name] == before['services'][name], 'Existing process changed: '+name)
    require(after['services']['bot-control-v2-1']['ActiveState'] == 'active', 'Controller not active')
    require(after['timers'] == before['timers'], 'Existing source or OI timer changed')
    for identifier, previous in before['oi_pairs'].items():
        current = after['oi_pairs'].get(identifier)
        require(current is not None and current['immutable_sha256'] == previous['immutable_sha256'],
                'Previous OI admission changed: '+identifier)
        if previous['control_sha256'] is not None:
            require(current['control_sha256'] == previous['control_sha256'], 'Previous closed OI outcome changed: '+identifier)


def unpack(package, stage):
    require(not stage.exists(), 'Stage already exists; retain old attempt and use a new rollout directory')
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), 'Duplicate ZIP paths')
        hashes = json.loads(archive.read('hashes.json'))
        require(isinstance(hashes, dict) and set(names) == set(hashes) | {'hashes.json'}, 'Uncatalogued package member')
        required = CODE_FILES | set(PAGES) | {'ui_before.json', 'git_revision.txt', 'future_protocol.json'} | {'scripts/'+n for n in (*UNITS, PROBE)}
        require(required <= set(hashes), 'Required package members missing')
        require({name for name in hashes if name.startswith('code/')} == CODE_FILES, 'Unexpected runtime code dependency')
        require(sum(info.file_size for info in archive.infolist()) <= 64*1024**2, 'Package uncompressed size too large')
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            require(not path.is_absolute() and '..' not in path.parts and '\\' not in info.filename
                    and not info.is_dir() and path.as_posix() == info.filename,
                    'Unbounded ZIP path')
            require(not stat.S_ISLNK(info.external_attr >> 16), 'ZIP symlinks not allowed')
            require(info.file_size <= 16*1024**2, 'Package member too large')
            require((stage/info.filename).resolve().is_relative_to(stage.resolve()), 'ZIP destination escaped stage')
        for name, expected in hashes.items():
            require(isinstance(expected, str) and re.fullmatch('[0-9a-f]{64}', expected), 'Invalid SHA256 catalogue')
            require(hashlib.sha256(archive.read(name)).hexdigest() == expected, 'Package hash mismatch: '+name)
        for name in CODE_FILES:
            if name.endswith('/__init__.py'):
                require(archive.read(name) == b'', 'Runtime package initializer must be empty')
        stage.mkdir(mode=0o700)
        archive.extractall(stage)
    require(all(sha(stage/name) == value for name, value in hashes.items()), 'Extracted package hash mismatch')
    return hashes


def replace(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name('.'+destination.name+'.range-profit-stage')
    shutil.copy2(source, temporary)
    os.chmod(temporary, destination.stat().st_mode & 0o777 if destination.exists() else 0o644)
    os.replace(temporary, destination)


def controller_get(route):
    pid = service('bot-control-v2-1')['MainPID']
    environment = dict(item.split(b'=', 1) for item in
                       Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in item)
    port = environment.get(b'BOT_CONTROL_PORT', b'8890').decode()
    token = environment.get(b'BOT_CONTROL_TOKEN', b'').decode()
    request = urllib.request.Request('http://127.0.0.1:'+port+route)
    if token:
        request.add_header('X-Bot-Control-Token', token)
    with urllib.request.urlopen(request, timeout=6) as response:
        return response.read(256*1024+1)


def main():
    require(os.name == 'posix', 'Run this rollout only on the intended Linux server')
    require(not LAB.exists(), 'New study already exists; refusing overwrite or reset')
    require(all(not (Path('/etc/systemd/system')/name).exists() for name in UNITS), 'New study unit already exists')
    resources_before = resource_check()
    stage = BASE/'stage'
    hashes = unpack(BASE/'deployment.zip', stage)
    revision = (stage/'git_revision.txt').read_text().strip()
    require(re.fullmatch('[0-9a-f]{40}', revision), 'Invalid Git revision')
    expected = json.loads((stage/'ui_before.json').read_text())
    require(set(expected) == set(PAGES), 'Unexpected UI preflight files')
    for name in PAGES:
        current = ROOT/name
        require((not current.exists()) if expected[name] is None else (current.is_file() and sha(current) == expected[name]),
                'UI changed since preflight: '+name)
        require(name == 'range_profit_protection_api.py' or expected[name] is not None, 'Existing UI file missing')
    before = audit()
    require(len(before['frozen']) >= 17, 'Expected registered studies not all found')
    require(all(value['ActiveState'] == 'active' for value in before['services'].values()), 'An existing service is not active')
    require(all(value['ActiveState'] == 'active' for value in before['timers'].values()), 'An existing timer is not active')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = BASE/('backup_'+stamp)
    backup.mkdir(mode=0o700)
    (backup/'before.json').write_text(json.dumps(before, indent=2))
    for name in PAGES:
        if expected[name] is not None:
            shutil.copy2(ROOT/name, backup/name)
    replaced = []
    installed_units = []
    try:
        subprocess.run(['systemd-analyze', 'verify', *(str(stage/'scripts'/name) for name in UNITS)],
                       check=True, timeout=20)
        # Probe all read-only/WAL mounts before creating the future registration.
        probe = subprocess.run(['/usr/bin/python3', str(stage/'scripts'/PROBE), '--unit', str(stage/'scripts'/UNITS[0])],
                               text=True, capture_output=True, timeout=180, check=False)
        (backup/'wal_probe_stdout.json').write_text(probe.stdout)
        (backup/'wal_probe_stderr.txt').write_text(probe.stderr)
        require(probe.returncode == 0, 'Disposable WAL sandbox regression failed')
        resources_at_registration = resource_check()
        LAB.mkdir(mode=0o700)
        shutil.copytree(stage/'code', LAB/'code')
        (LAB/'data').mkdir(mode=0o700)
        clean_environment = {'PATH': '/usr/local/bin:/usr/bin:/bin',
                             'PYTHONPATH': str(LAB/'code/src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        subprocess.run(['/usr/bin/python3', '-m', 'trading_bot.research.range_profit_protection',
                        '--data-dir', str(LAB/'data'), '--source', str(SOURCE), '--init-only'],
                       env=clean_environment, check=True, timeout=30)
        initial = json.loads((LAB/'data/status.json').read_text())
        require(initial['study'] == 'range-profit-protection-shadow-v1' and initial['health'] == 'OK'
                and initial['order_capability'] is False and initial['combined_with_main_balance'] is False,
                'Initial experiment status failed')
        require(all(value == 0 for value in initial['counts'].values()), 'New experiment did not start at zero')
        with closing(readonly(LAB/'data/range_profit_protection.sqlite3')) as db:
            registration = json.loads(db.execute("SELECT value FROM meta WHERE key='registration'").fetchone()[0])
        require(registration['identity'] == json.loads((stage/'future_protocol.json').read_text()),
                'Actual registration differs from the predeclared frozen protocol')
        (backup/'initial_zero_status.json').write_text(json.dumps(initial, indent=2))
        for name in UNITS:
            replace(stage/'scripts'/name, Path('/etc/systemd/system')/name)
            installed_units.append(name)
        for name in PAGES:
            replace(stage/name, ROOT/name)
            replaced.append(name)
        subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=20)
        subprocess.run(['systemctl', 'restart', 'bot-control-v2-1'], check=True, timeout=40)
        last_error = None
        for _ in range(12):
            try:
                endpoint = json.loads(controller_get('/range-profit-protection-shadow'))
                require(endpoint['health'] == 'OK' and endpoint['order_capability'] is False
                        and endpoint['registered_ms'] == initial['registered_ms'], 'API did not expose new registration')
                require('range-profit-protection-shadow' in controller_get('/dashboard').decode(), 'New dashboard card missing')
                break
            except Exception as exc:
                last_error = type(exc).__name__
                time.sleep(.5)
        else:
            raise RuntimeError('Controller verification failed: '+str(last_error))
        (backup/'api_initial_status.json').write_text(json.dumps(endpoint, indent=2))
        subprocess.run(['systemctl', 'enable', '--now', UNITS[1]], check=True, timeout=20)
        require(service(UNITS[1])['ActiveState'] == 'active', 'New timer not active')
        after = audit()
        check_preserved(before, after)
        record = {'passed': True, 'at_ms': int(time.time()*1000), 'git_revision': revision,
                  'backup': str(backup), 'registered_ms': initial['registered_ms'],
                  'started_local': datetime.fromtimestamp(initial['registered_ms']/1000, timezone(timedelta(hours=5))).isoformat(),
                  'source_watermark': initial['source_watermark'], 'before': before, 'after': after,
                  'resources_before': resources_before, 'resources_at_registration': resources_at_registration,
                  'package_hashes': hashes, 'new_statistics_started_at_zero': True,
                  'existing_controls_unchanged': True, 'main_bot_monitor_producer_collector_not_restarted': True,
                  'only_dashboard_controller_restarted': True}
        (BASE/'deployment.json').write_text(json.dumps(record, indent=2))
        print(json.dumps({key: record[key] for key in ('passed', 'git_revision', 'started_local', 'source_watermark',
            'backup', 'new_statistics_started_at_zero', 'existing_controls_unchanged', 'only_dashboard_controller_restarted')}))
    except BaseException:
        rollback_errors = []
        def attempt(label, callback):
            try:
                callback()
            except BaseException as exc:
                rollback_errors.append(label+':'+type(exc).__name__+':'+str(exc)[:160])
        if UNITS[1] in installed_units:
            attempt('disable new timer', lambda: subprocess.run(['systemctl', 'disable', '--now', UNITS[1]], check=True, timeout=20))
        if UNITS[0] in installed_units:
            attempt('stop new observer', lambda: subprocess.run(['systemctl', 'stop', UNITS[0]], check=True, timeout=90))
        for name in reversed(replaced):
            if expected[name] is None:
                require(name == 'range_profit_protection_api.py', 'Refusing to remove preexisting UI file')
                attempt('remove new API', lambda name=name: (ROOT/name).unlink(missing_ok=True))
            else:
                attempt('restore '+name, lambda name=name: replace(backup/name, ROOT/name))
        for name in installed_units:
            attempt('remove new unit '+name, lambda name=name: (Path('/etc/systemd/system')/name).unlink(missing_ok=True))
        if installed_units:
            attempt('reload units', lambda: subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=20))
        if replaced:
            attempt('restart restored controller', lambda: subprocess.run(['systemctl', 'restart', 'bot-control-v2-1'], check=True, timeout=40))
        if LAB.exists():
            destination = BASE/('failed_lab_'+stamp)
            require(LAB == Path('/root/bot_range_profit_protection') and destination.parent == BASE
                    and not destination.exists(), 'Failed lab retention path invalid')
            attempt('retain failed lab', lambda: LAB.rename(destination))
        (backup/'rollback.json').write_text(json.dumps({'attempted': True, 'errors': rollback_errors}, indent=2))
        raise


if __name__ == '__main__':
    main()
