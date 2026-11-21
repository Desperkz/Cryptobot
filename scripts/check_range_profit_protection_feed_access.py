"""Disposable Linux/systemd WAL sandbox probe; never opens production inputs.

The source directory is writable only to permit normal SQLite WAL coordination.
The DB and status file are separately mounted read-only. This checks the actual
unit's paths and inherited protections with a live disposable writer, including
the clean-close case in which WAL sidecars may have been removed by SQLite.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import uuid


COPIED = {
    'ProtectSystem', 'ProtectHome', 'PrivateTmp', 'NoNewPrivileges',
    'ReadOnlyPaths', 'ReadWritePaths', 'MemoryHigh', 'MemoryMax', 'MemorySwapMax',
    'CPUQuota', 'TasksMax', 'Nice', 'RestrictAddressFamilies',
    'ProtectKernelTunables', 'ProtectKernelModules', 'ProtectControlGroups',
    'RestrictSUIDSGID', 'UMask',
}
LAB = '/root/bot_range_profit_protection'
SOURCE = '/root/bot_diversified_v3/data'


def mapped_properties(unit: Path, root: Path) -> dict[str, str]:
    """Copy sandbox properties without retaining any production path."""
    root = PurePosixPath(root.as_posix())
    properties = {}
    in_service = False
    for line in unit.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line.startswith('['):
            in_service = line == '[Service]'
            continue
        if not in_service or not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key not in COPIED:
            continue
        if key in properties:
            raise ValueError('Repeated sandbox property: ' + key)
        for old, new in ((LAB, str(root / 'own')), (SOURCE, str(root / 'source'))):
            value = value.replace(old, new)
        if '/root/' in value:
            raise ValueError('Unmapped production path in sandbox property')
        properties[key] = value
    required = {'ReadOnlyPaths', 'ReadWritePaths', 'ProtectSystem', 'ProtectHome',
                'RestrictAddressFamilies', 'NoNewPrivileges'}
    if not required <= properties.keys():
        raise ValueError('Required sandbox protections missing')
    if properties['ProtectSystem'] != 'strict' or properties['ProtectHome'] != 'read-only':
        raise ValueError('Filesystem protections changed')
    if properties['NoNewPrivileges'] != 'true':
        raise ValueError('Privilege protection changed')
    if set(properties['ReadWritePaths'].split()) != {str(root / 'own/data'), str(root / 'source')}:
        raise ValueError('Unexpected writable path')
    if set(properties['ReadOnlyPaths'].split()) != {
            str(root / 'own/code'), str(root / 'source/shadow.sqlite3'), str(root / 'source/status.json')}:
        raise ValueError('Input DB, input status and code must be read-only')
    if set(properties['RestrictAddressFamilies'].split()) != {'AF_UNIX', 'AF_INET', 'AF_INET6'}:
        raise ValueError('Unexpected address families for public HTTPS')
    return properties


def reader(root: Path, expected: list[int]) -> None:
    database = root / 'source/shadow.sqlite3'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        db.execute('PRAGMA query_only=ON')
        if [r[0] for r in db.execute('SELECT id FROM items ORDER BY id')] != expected:
            raise AssertionError('Committed source rows did not match')
        # Even an accidental change to query_only cannot defeat the read-only URI.
        db.execute('PRAGMA query_only=OFF')
        try:
            db.execute('INSERT INTO items VALUES(999)')
        except sqlite3.OperationalError as exc:
            if 'readonly' not in str(exc).lower():
                raise
        else:
            raise AssertionError('Read-only URI did not protect source')
    for protected in (database, database.with_name('status.json'), root / 'own/code/sentinel.txt'):
        try:
            with protected.open('r+b'):
                pass
        except OSError:
            pass
        else:
            raise AssertionError('Sandbox file mount not read-only: ' + str(protected))
    result = {'passed': True, 'expected': expected, 'inputs': 1}
    (root / 'own/data/result.json').write_text(json.dumps(result), encoding='utf-8')
    print(json.dumps(result))


def main(unit: Path) -> None:
    root = Path('/var/lib') / ('range-profit-protection-access-' + uuid.uuid4().hex)
    properties = mapped_properties(unit, root)
    for relative in ('source', 'own/data', 'own/code'):
        (root / relative).mkdir(parents=True, exist_ok=False)
    (root / 'own/code/sentinel.txt').write_text('unchanged', encoding='utf-8')
    database = root / 'source/shadow.sqlite3'
    database.with_name('status.json').write_text('{}', encoding='utf-8')
    helper = root / 'reader.py'
    helper.write_bytes(Path(__file__).read_bytes())
    writer = None
    phases = []

    def phase(expected: list[int]) -> None:
        args = ['systemd-run', '--quiet', '--wait', '--pipe', '--collect',
                '--unit=range-profit-protection-access-' + uuid.uuid4().hex]
        for key, value in properties.items():
            args += ['-p', key + '=' + value]
        args += ['/usr/bin/python3', str(helper), '--reader', str(root),
                 '--expected', ','.join(map(str, expected))]
        completed = subprocess.run(args, text=True, capture_output=True, timeout=55, check=False)
        if completed.returncode:
            raise RuntimeError('Disposable sandbox reader failed: ' + completed.stdout + completed.stderr)
        result = json.loads((root / 'own/data/result.json').read_text(encoding='utf-8'))
        if not result['passed'] or result['expected'] != expected:
            raise AssertionError('Unexpected disposable reader result')
        phases.append(result)

    try:
        writer = sqlite3.connect(database)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE items(id INTEGER PRIMARY KEY)')
        writer.execute('INSERT INTO items VALUES(1)')
        writer.commit()
        writer.execute('INSERT INTO items VALUES(2)')
        phase([1])  # Uncommitted row 2 must not leak through the live WAL.
        writer.rollback()
        writer.close()
        writer = None
        phase([1])  # A source clean-close may remove WAL/SHM; reader must reopen.
        with closing(sqlite3.connect(database)) as producer:
            producer.execute('INSERT INTO items VALUES(2)')
            producer.commit()
        phase([1, 2])  # Reader protections must not block the original owner.
    finally:
        if writer is not None:
            writer.close()
    print(json.dumps({'passed': True, 'fixture': str(root), 'phases': phases,
                      'production_databases_opened': False, 'network_requests_made': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit', type=Path)
    parser.add_argument('--reader', type=Path)
    parser.add_argument('--expected')
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    if args.reader:
        if not args.expected:
            parser.error('--reader requires --expected')
        reader(args.reader, [int(value) for value in args.expected.split(',')])
    elif args.unit:
        if args.validate_only:
            properties = mapped_properties(args.unit, Path('/var/lib/disposable-range-profit-protection'))
            print(json.dumps({'passed': True, 'sandbox_properties': len(properties),
                              'production_databases_opened': False, 'systemd_executed': False}))
        else:
            main(args.unit)
    else:
        parser.error('Specify --unit or --reader')
