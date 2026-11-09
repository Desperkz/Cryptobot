"""Quiesced, lossless research-only maintenance; no trading service restart."""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

import research_storage as storage

OPS = Path('/root/bot_research_storage')
LABS = (('bot_diversified_shadow', 'diversified-shadow'),
        ('bot_diversified_v3', 'diversified-v3-shadow'))
MIB = 1024**2
TRIGGER = 384*MIB


def systemctl(*args):
    return subprocess.check_output(['systemctl', *args], text=True, timeout=110).strip()


def active(name):
    return systemctl('show', name, '-p', 'ActiveState', '--value') == 'active'


def core():
    return {name: systemctl('show', name, '-p', 'MainPID', '-p', 'NRestarts', '-p', 'ActiveState')
            for name in ('trading-bot-v2-1', 'paper-monitor-v2-1', 'mainnet-paper-lab')}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False))
    os.replace(temporary, path)


def source_path(name):
    return Path('/root')/name/'data/shadow.sqlite3'


def file_bytes(name):
    return sum(p.stat().st_size for p in source_path(name).parent.glob('shadow.sqlite3*'))


def checkpoint(path):
    with closing(sqlite3.connect(path, timeout=4)) as c:
        if c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
            raise RuntimeError('SOURCE_WAL_BUSY')
        if c.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
            raise RuntimeError('SOURCE_WAL_NOT_CLOSED')


def restore_backup(source, backup):
    checkpoint(source)
    # Separate ReadWritePaths are distinct bind mounts even when st_dev agrees.
    # Keep replacement files inside the lab's same writable mount, outside data.
    temporary = source.parent.parent/'.storage-temporary'/'rollback.sqlite3'
    temporary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(backup, temporary)
    os.chmod(temporary, 0o600)
    storage._fsync_file(temporary)
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(source)+suffix).exists():
            raise RuntimeError('ROLLBACK_SIDECAR_PRESENT')
    os.replace(temporary, source)
    storage._fsync_directory(source.parent)


def summary():
    result = []
    for name, _ in LABS:
        archive = OPS/'archives'/(name+'.sqlite3')
        result.append({'lab': name, 'active_bytes': file_bytes(name),
                       'archive_bytes': archive.stat().st_size if archive.exists() else 0})
    return result


def recover_timers():
    """ExecStopPost safety net also runs after a timeout/OOM/SIGKILL."""
    marker = OPS/'data/resume_timers.json'
    allowed = {unit+'.timer' for _, unit in LABS}
    if marker.exists():
        names = json.loads(marker.read_text())
        if not isinstance(names, list) or any(n not in allowed for n in names):
            raise RuntimeError('INVALID_TIMER_RECOVERY_MARKER')
        for timer in names:
            systemctl('start', timer)
        marker.unlink()
    reason = os.environ.get('SERVICE_RESULT', 'success')
    status_path = OPS/'data/status.json'
    if reason != 'success' and status_path.exists():
        value = json.loads(status_path.read_text())
        value.update(health='DEGRADED', generated_at_ms=int(time.time()*1000))
        value.setdefault('errors', []).append('MAINTENANCE_SERVICE_'+reason.upper())
        write_json(status_path, value)


def maintain(*, force=False, keep_backups=False, verify_restore=False):
    for folder in ('data', 'archives', 'backups', 'temporary', 'reports'):
        (OPS/folder).mkdir(parents=True, exist_ok=True, mode=0o700)
    now = int(time.time()*1000)
    old_status = OPS/'data/status.json'
    previous = json.loads(old_status.read_text()) if old_status.exists() else {}
    value = {'study': 'research-storage-maintenance-v1', 'order_capability': False,
             'generated_at_ms': now, 'last_success_ms': previous.get('last_success_ms'),
             'health': 'IDLE', 'errors': [], 'labs': summary(),
             'free_disk_bytes': shutil.disk_usage('/root').free}
    chosen = [name for name, _ in LABS if force or file_bytes(name) >= TRIGGER]
    if not chosen:
        if value['free_disk_bytes'] < 1024**3:
            value.update(health='DEGRADED', errors=['DISK_HEADROOM_BELOW_1GIB'])
        write_json(old_status, value)
        return value
    if value['free_disk_bytes'] < 1536*MIB:
        value.update(health='DEGRADED', errors=['INSUFFICIENT_MAINTENANCE_HEADROOM'])
        write_json(old_status, value)
        raise RuntimeError(value['errors'][0])
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    run_path = OPS/'reports'/(stamp+'.json')
    report = {'at_ms': now, 'chosen': chosen, 'before_core': core(), 'operations': []}
    timers = {unit+'.timer': active(unit+'.timer') for _, unit in LABS}
    backups = []
    with ExitStack() as held:
        try:
            write_json(OPS/'data/resume_timers.json', [n for n, enabled in timers.items() if enabled])
            # Stop producers before holding locks, so a waiting unit cannot deadlock us.
            for timer in timers:
                systemctl('stop', timer)
            for _, unit in reversed(LABS):
                systemctl('stop', unit+'.service')
            import fcntl
            for name, _ in LABS:
                lock = held.enter_context((source_path(name).parent/'.cycle.lock').open('a+'))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with closing(sqlite3.connect(source_path('bot_diversified_v3'))) as c:
                cursor = int(c.execute("SELECT value FROM meta WHERE key='feed_cursor'").fetchone()[0])
            report['consumer_cursor'] = cursor
            for name in chosen:
                source = source_path(name)
                backup = OPS/'backups'/(stamp+'_'+name+'.sqlite3')
                operation = {'lab': name}
                operation['backup'] = storage.backup_database(source, backup)
                backups.append((source, backup))
                report['operations'].append(operation)
                write_json(run_path, report)
                archive = OPS/'archives'/(name+'.sqlite3')
                operation['archive'] = storage.archive_raw(source, archive,
                    cutoff_ms=now-storage.RETENTION_MS,
                    consumer_cursor=cursor if name == 'bot_diversified_shadow' else None,
                    now_ms=now)
                compact = source.parent.parent/'.storage-temporary'/(stamp+'_compact.sqlite3')
                operation['compact'] = storage.compact_database(source, compact,
                    expected_nonraw_digest=operation['archive']['nonraw_digest'])
                operation['coverage'] = storage.verify_archive(source, archive)
                if verify_restore:
                    # Initial rollout has no prior NULLs: actual full restoration
                    # must reproduce the exact pre-maintenance logical history.
                    restored = OPS/'temporary'/'restore_tests'/(stamp+'_'+name+'.sqlite3')
                    proof = storage.restore_nulls(source, archive, copy_to=restored)
                    if proof['logical_digest'] != operation['backup']['logical_digest']:
                        raise RuntimeError('FULL_RESTORE_PROOF_MISMATCH')
                    operation['restore_proof'] = {'ok': True,
                        'restored_raw_records': proof['restored_raw_records'],
                        'logical_digest': proof['logical_digest']}
                    restored.unlink()  # Disposable copy only; source/archive remain.
                write_json(run_path, report)
            report['after_core'] = core()
            if report['after_core'] != report['before_core']:
                raise RuntimeError('PROTECTED_SERVICES_CHANGED')
            report['ok'] = True
            report['backups_retained'] = keep_backups
            write_json(run_path, report)
            value.update(health='OK', last_success_ms=int(time.time()*1000),
                         labs=summary(), report=str(run_path))
        except BaseException as exc:
            report['error'] = repr(exc)
            report['ok'] = False
            for source, backup in reversed(backups):
                restore_backup(source, backup)
            write_json(run_path, report)
            value.update(health='DEGRADED', errors=[type(exc).__name__+': '+str(exc)],
                         report=str(run_path), labs=summary())
            raise
        finally:
            # ExitStack closes flocks after timers are queued; units may briefly
            # skip a busy lock and retry normally. No trading unit is touched.
            for timer, was_active in timers.items():
                if was_active:
                    systemctl('start', timer)
            marker = OPS/'data/resume_timers.json'
            if marker.exists():
                marker.unlink()
            value['generated_at_ms'] = int(time.time()*1000)
            value['free_disk_bytes'] = shutil.disk_usage('/root').free
            write_json(old_status, value)
    if not keep_backups:
        for _, backup in backups:
            assert backup.resolve().is_relative_to((OPS/'backups').resolve())
            try:
                backup.unlink()  # Temporary copies; verified hot+cold history remains.
            except OSError as exc:
                value['errors'].append('TEMP_BACKUP_CLEANUP: '+str(exc))
                value['health'] = 'DEGRADED'
        write_json(old_status, value)
    return value


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--keep-backups', action='store_true')
    parser.add_argument('--verify-restore', action='store_true')
    parser.add_argument('--recover-timers', action='store_true')
    args = parser.parse_args()
    if args.recover_timers:
        recover_timers()
    else:
        print(json.dumps(maintain(force=args.force, keep_backups=args.keep_backups,
                                 verify_restore=args.verify_restore)))
