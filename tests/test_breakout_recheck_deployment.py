import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('wal_probe', Path(__file__).resolve().parents[1] /
                                             'scripts/check_breakout_recheck_feed_access.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize('error', [
    {'ok': False, 'sqlite_errorcode': 14},
    {'ok': False, 'sqlite_errorcode': 270},
    {'ok': False, 'sqlite_errorcode': None, 'sqlite_exception_type': 'OperationalError',
     'sqlite_message': 'unable to open database file'},
])
def test_negative_wal_control_supports_python_310_without_exception_codes(error):
    assert probe.expected_cantopen(error)


@pytest.mark.parametrize('error', [
    {'ok': True, 'sqlite_errorcode': 14},
    {'ok': False, 'sqlite_errorcode': 8},
    {'ok': False, 'sqlite_errorcode': None, 'sqlite_exception_type': 'OperationalError',
     'sqlite_message': 'disk I/O error'},
    {'ok': False, 'sqlite_errorcode': None, 'sqlite_exception_type': 'PermissionError',
     'sqlite_message': 'unable to open database file'},
])
def test_negative_wal_control_never_accepts_unknown_failures(error):
    assert not probe.expected_cantopen(error)
