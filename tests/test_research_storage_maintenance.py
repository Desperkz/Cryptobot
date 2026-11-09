import json
from collections import namedtuple

import pytest

import research_storage_maintenance as maintenance


def test_recovery_starts_only_originally_active_research_timers(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, 'OPS', tmp_path)
    (tmp_path/'data').mkdir()
    marker = tmp_path/'data/resume_timers.json'
    marker.write_text(json.dumps(['diversified-shadow.timer']))
    calls = []
    monkeypatch.setattr(maintenance, 'systemctl', lambda *a: calls.append(a))
    monkeypatch.setenv('SERVICE_RESULT', 'success')
    maintenance.recover_timers()
    assert calls == [('start', 'diversified-shadow.timer')] and not marker.exists()


def test_recovery_rejects_trading_service_in_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, 'OPS', tmp_path)
    (tmp_path/'data').mkdir()
    (tmp_path/'data/resume_timers.json').write_text(json.dumps(['trading-bot-v2-1.service']))
    calls = []
    monkeypatch.setattr(maintenance, 'systemctl', lambda *a: calls.append(a))
    with pytest.raises(RuntimeError, match='INVALID_TIMER'):
        maintenance.recover_timers()
    assert not calls


def test_timeout_recovery_is_visible_as_degraded(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, 'OPS', tmp_path)
    (tmp_path/'data').mkdir()
    status = tmp_path/'data/status.json'
    status.write_text(json.dumps({'health': 'OK', 'errors': [], 'last_success_ms': 123}))
    monkeypatch.setenv('SERVICE_RESULT', 'timeout')
    maintenance.recover_timers()
    value = json.loads(status.read_text())
    assert value['health'] == 'DEGRADED' and value['last_success_ms'] == 123
    assert 'MAINTENANCE_SERVICE_TIMEOUT' in value['errors']


def test_low_disk_stops_before_stopping_a_single_service(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, 'OPS', tmp_path)
    monkeypatch.setattr(maintenance, 'summary', lambda: [])
    monkeypatch.setattr(maintenance, 'file_bytes', lambda name: 512*1024**2)
    usage = namedtuple('usage', 'total used free')(2*1024**3, 1024**3, 1024**3)
    monkeypatch.setattr(maintenance.shutil, 'disk_usage', lambda path: usage)
    calls = []
    monkeypatch.setattr(maintenance, 'systemctl', lambda *a: calls.append(a))
    with pytest.raises(RuntimeError, match='HEADROOM'):
        maintenance.maintain()
    assert not calls
    assert json.loads((tmp_path/'data/status.json').read_text())['health'] == 'DEGRADED'
