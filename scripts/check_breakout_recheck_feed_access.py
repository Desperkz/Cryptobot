#!/usr/bin/env python3
"""Disposable Linux/systemd regression for the entry recheck read-only WAL feed.

Uses only the standard library and a synthetic database below /var/lib. It never
opens a bot database or runs any bot module. Only filesystem/resource sandbox
properties are copied from --unit; all paths are remapped to the fixture.

Example (root, Linux):
  python3 check_breakout_recheck_feed_access.py --unit /etc/systemd/system/breakout-recheck-shadow.service

The fixture is retained for inspection. The JSON report contains its path and
unit checksum. Every transient service is collected automatically after exit.
"""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import time
import uuid


SOURCE_DIRECTORY = '/root/bot_mainnet_paper/data'
OWN_DIRECTORY = '/root/bot_breakout_recheck_shadow/data'
COPIED_PROPERTIES = {
    'TimeoutStartSec', 'Nice', 'CPUQuota', 'CPUQuotaPeriodSec', 'MemoryHigh',
    'MemoryMax', 'MemorySwapMax', 'OOMScoreAdjust', 'NoNewPrivileges',
    'PrivateTmp', 'PrivateNetwork', 'TasksMax', 'ProtectSystem', 'ProtectHome', 'ReadOnlyPaths',
    'ReadWritePaths', 'InaccessiblePaths', 'UMask', 'PrivateDevices',
    'ProtectKernelTunables', 'ProtectKernelModules', 'ProtectControlGroups',
    'RestrictSUIDSGID', 'LockPersonality', 'RestrictRealtime',
    'CapabilityBoundingSet', 'AmbientCapabilities', 'RestrictAddressFamilies',
    'SystemCallFilter', 'SystemCallArchitectures', 'ProtectProc', 'ProcSubset',
}
LIST_PROPERTIES = {
    'ReadOnlyPaths', 'ReadWritePaths', 'InaccessiblePaths',
    'CapabilityBoundingSet', 'AmbientCapabilities', 'RestrictAddressFamilies',
    'SystemCallFilter', 'SystemCallArchitectures',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def service_properties(path, fixture):
    """Copy only sandbox settings, never real Exec*/environment settings."""
    section = None
    properties = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('['):
            section = line
            continue
        if section != '[Service]' or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key not in COPIED_PROPERTIES:
            continue
        value = value.replace(SOURCE_DIRECTORY, str(fixture / 'source'))
        value = value.replace(OWN_DIRECTORY, str(fixture / 'output'))
        value = value.replace('/root/bot_breakout_recheck_shadow/code', str(fixture / 'code'))
        if '/root/' in value:
            raise ValueError('Unmapped production path in sandbox property: ' + key)
        if key in LIST_PROPERTIES:
            properties[key] = (properties.get(key, '') + ' ' + value).strip() if value else ''
        else:
            properties[key] = value
    for key in ('ProtectSystem', 'ProtectHome', 'ReadOnlyPaths', 'ReadWritePaths'):
        if key not in properties:
            raise ValueError('Missing required sandbox property: ' + key)
    return properties


def source_reader(source, output, expected):
    """Same SQLite URI/transaction semantics as entry recheck read-only open, no bot imports."""
    database = source / 'mainnet_paper_lab.sqlite3'
    result = {'ok': False, 'expected_ids': expected}
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            rows = [row[0] for row in db.execute('SELECT id FROM snapshots ORDER BY id')]
            assert rows == expected, {'rows': rows, 'expected': expected}
            assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            for query_only in (True, False):
                if not query_only:
                    db.execute('PRAGMA query_only=OFF')
                try:
                    db.execute("UPDATE snapshots SET payload='forbidden' WHERE id=1")
                except sqlite3.OperationalError as exc:
                    assert 'readonly' in str(exc).lower(), str(exc)
                else:
                    db.rollback()
                    raise AssertionError('Read-only source SQL UPDATE unexpectedly succeeded')
            db.rollback()
        # A read-only bind mount must protect both the actual DB and status file,
        # even though SQLite is allowed to create its own coordination sidecars.
        for path in (database, source / 'status.json'):
            try:
                fd = os.open(path, os.O_WRONLY)
            except OSError as exc:
                assert exc.errno in (13, 30), repr(exc)  # EACCES or EROFS
            else:
                os.close(fd)
                raise AssertionError('Filesystem source file is not read-only: ' + str(path))
        marker = output / ('reader_' + uuid.uuid4().hex + '.json')
        result = {'ok': True, 'ids': rows, 'source_sql_read_only': True,
                  'source_filesystem_read_only': True, 'journal_mode': 'wal'}
        marker.write_text(json.dumps(result))
    except Exception as exc:
        result.update(ok=False, error=repr(exc),
                      sqlite_exception_type=type(exc).__name__, sqlite_message=str(exc),
                      sqlite_errorname=getattr(exc, 'sqlite_errorname', None),
                      sqlite_errorcode=getattr(exc, 'sqlite_errorcode', None))
    print(json.dumps(result), flush=True)
    return 0 if result['ok'] else 1


def invoke_reader(script, interpreter, properties, fixture, expected, label):
    unit = 'codex-breakout-wal-probe-' + uuid.uuid4().hex[:16]
    command = ['systemd-run', '--quiet', '--wait', '--pipe', '--collect',
               '--unit=' + unit, '--service-type=oneshot']
    for key, value in properties.items():
        command += ['--property=' + key + '=' + value]
    command += [interpreter, str(script), '--reader', str(fixture / 'source'),
                '--reader-output', str(fixture / 'output'),
                '--expected-ids', ','.join(map(str, expected))]
    completed = subprocess.run(command, text=True, capture_output=True, timeout=150)
    result = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and 'ok' in value:
            result = value
            break
    if result is None:
        raise RuntimeError('Probe did not report JSON: ' + completed.stderr[-2000:])
    result.update(label=label, unit=unit, returncode=completed.returncode)
    if completed.stderr:
        result['service_stderr'] = completed.stderr[-2000:]
    return result


def require_success(value):
    if not value['ok'] or value['returncode'] != 0:
        raise AssertionError(value)


def expected_cantopen(value):
    """Python 3.10 does not expose sqlite_errorcode on exceptions yet."""
    if value.get('ok'):
        return False
    code = value.get('sqlite_errorcode')
    if isinstance(code, int):
        return code & 255 == 14  # SQLite's primary SQLITE_CANTOPEN code.
    return (value.get('sqlite_exception_type') == 'OperationalError'
            and value.get('sqlite_message') == 'unable to open database file')


def run_probe(args):
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise RuntimeError('Run as root on Linux with a running systemd manager')
    unit_path = Path(args.unit).resolve(strict=True)
    identifier = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '_' + uuid.uuid4().hex[:8]
    fixture = Path(args.fixture_parent).resolve() / ('codex-breakout-wal-probe-' + identifier)
    # /tmp is deliberately not used: PrivateTmp hides host temporary paths.
    if str(fixture).startswith(('/tmp/', '/var/tmp/')):
        raise ValueError('Fixture must be outside /tmp and /var/tmp (PrivateTmp)')
    fixture.mkdir(mode=0o755, parents=False)
    source = fixture / 'source'
    source.mkdir(mode=0o755)
    (fixture / 'output').mkdir(mode=0o700)
    (fixture / 'code').mkdir(mode=0o755)
    # Run the exact same probe code inside the mount namespace. It is outside
    # every writable path and contains no production data.
    script = fixture / 'probe_wal_sandbox.py'
    script.write_bytes(Path(__file__).resolve().read_bytes())
    script.chmod(0o644)
    database = source / 'mainnet_paper_lab.sqlite3'
    state = source / 'status.json'
    state.write_text('{"fixture":true}')
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        db.execute('CREATE TABLE snapshots(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        db.execute("INSERT INTO snapshots VALUES(1,'initial')")
        db.commit()
    assert not database.with_name(database.name + '-wal').exists()
    assert not database.with_name(database.name + '-shm').exists()
    properties = service_properties(unit_path, fixture)
    report = {'fixture': str(fixture), 'unit_path': str(unit_path),
              'unit_sha256': digest(unit_path), 'sandbox_properties': properties,
              'sqlite_version': sqlite3.sqlite_version, 'checks': [], 'ok': False}
    try:
        # Negative control: reproduce the original all-read-only directory mount
        # after a clean writer close removes SQLite's -wal/-shm sidecars.
        old_properties = dict(properties)
        old_properties['ReadOnlyPaths'] = str(source)
        old_properties['ReadWritePaths'] = str(fixture / 'output')
        broken = invoke_reader(script, args.python, old_properties, fixture, [1], 'negative_all_source_readonly')
        report['checks'].append(broken)
        assert expected_cantopen(broken), broken

        before = {'database': digest(database), 'status': digest(state)}
        first = invoke_reader(script, args.python, properties, fixture, [1], 'clean_close_without_sidecars')
        report['checks'].append(first)
        require_success(first)
        assert {'database': digest(database), 'status': digest(state)} == before

        # A complete subsequent source cycle must be visible on a new open.
        with closing(sqlite3.connect(database)) as db:
            db.execute("INSERT INTO snapshots VALUES(2,'next-cycle')")
            db.commit()
        second = invoke_reader(script, args.python, properties, fixture, [1, 2], 'source_update_visible_next_open')
        report['checks'].append(second)
        require_success(second)

        # Keep the writer open and retain new committed rows in WAL only. This
        # catches immutable=1 / copying only the main DB as an unsafe workaround.
        with closing(sqlite3.connect(database)) as writer:
            writer.execute('PRAGMA wal_autocheckpoint=0')
            main_hash = digest(database)
            writer.execute("INSERT INTO snapshots VALUES(3,'committed-in-wal')")
            writer.commit()
            assert digest(database) == main_hash, 'Fixture row unexpectedly checkpointed out of WAL'
            assert database.with_name(database.name + '-wal').stat().st_size > 0
            writer.execute("INSERT INTO snapshots VALUES(4,'not-yet-committed')")
            third = invoke_reader(script, args.python, properties, fixture, [1, 2, 3], 'live_wal_committed_not_uncommitted')
            report['checks'].append(third)
            require_success(third)
            writer.commit()
            fourth = invoke_reader(script, args.python, properties, fixture, [1, 2, 3, 4], 'live_wal_next_commit_visible')
            report['checks'].append(fourth)
            require_success(fourth)
        assert digest(state) == before['status']
        report['ok'] = True
    except Exception as exc:
        report['failure'] = repr(exc)
    finally:
        (fixture / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report['ok'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit', default='/etc/systemd/system/breakout-recheck-shadow.service')
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--fixture-parent', default='/var/lib')
    parser.add_argument('--reader', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--reader-output', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--expected-ids', default='', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.reader:
        expected = [int(value) for value in args.expected_ids.split(',') if value]
        return source_reader(args.reader, args.reader_output, expected)
    return run_probe(args)


if __name__ == '__main__':
    raise SystemExit(main())
