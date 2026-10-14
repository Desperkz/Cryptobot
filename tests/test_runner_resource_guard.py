import json
from pathlib import Path

import pytest
import runner_resource_guard as guard


def test_minute_retries_do_not_increase_worker_frequency(tmp_path):
    saved = tmp_path / 'status.json'
    saved.write_text(json.dumps({'generated_at_ms': 1_000_000}))
    assert guard.cycle_due(saved, 1_060_000)[0] is False
    assert guard.cycle_due(saved, 1_299_999)[0] is False
    assert guard.cycle_due(saved, 1_300_000)[0] is True
    assert guard.cycle_due(saved, 1_600_000)[0] is True


@pytest.mark.parametrize('content', ['{', '{}', '[]', '{"generated_at_ms": NaN}',
    '{"generated_at_ms": Infinity}', '{"generated_at_ms": true}',
    '{"generated_at_ms": "100"}', ' ' * (512 * 1024 + 1)],
    ids=['broken', 'missing', 'array', 'nan', 'infinity', 'bool', 'string', 'oversized'])
def test_invalid_saved_status_does_not_admit_worker(tmp_path, content):
    saved = tmp_path / 'status.json'
    saved.write_text(content)
    assert guard.cycle_due(saved, 2_000_000) == (False, 'INVALID_STATUS')


def test_future_timestamp_defers_but_missing_status_allows_initial_cycle(tmp_path):
    saved = tmp_path / 'status.json'
    assert guard.cycle_due(saved, 2_000_000)[0] is True
    saved.write_text(json.dumps({'generated_at_ms': 2_000_001}))
    assert guard.cycle_due(saved, 2_000_000)[0] is False


def test_busy_promotion_retries_without_changing_saved_clock(tmp_path, monkeypatch):
    saved = tmp_path / 'status.json'
    saved.write_text('{"generated_at_ms": 1000000}')
    original = saved.read_bytes()
    read_text = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *a, **k:
        'MemAvailable: 100000 kB\nSwapFree: 500000 kB\n'
        if path.as_posix() == '/proc/meminfo' else read_text(path, *a, **k))
    monkeypatch.setattr(guard.subprocess, 'check_output', lambda *a, **k: 'activating\n')
    assert guard.ready(saved, 1_300_000) is False
    monkeypatch.setattr(guard.subprocess, 'check_output', lambda *a, **k: 'inactive\n')
    assert guard.ready(saved, 1_360_000) is True
    assert saved.read_bytes() == original
