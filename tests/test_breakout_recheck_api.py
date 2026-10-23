import copy
import io
import json

import pytest

import paper_pilots_api as api


NOW = 2_000_000_000


def saved_status():
    return {
        'study': 'sqz-entry-recheck-shadow-v1', 'mode': 'SHADOW',
        'order_capability': False, 'cohort': '2026-10-23-sqz-entry-recheck-shadow-v1',
        'registered_ms': NOW - 86_400_000, 'generated_at_ms': NOW,
        'health': 'OK', 'errors': [],
        'counts': {'sources': 12, 'waiting_entry': 1, 'decided': 10,
                   'kept': 7, 'rejected': 3, 'unscorable': 1,
                   'open_controls': 2, 'closed_pairs': 8},
        'pnl': {'control_net_usdt': -4.5, 'recheck_net_usdt': -2.5,
                'delta_usdt': 2., 'avoided_losses_usdt': 3., 'lost_profits_usdt': 1.},
        'transitions': {'INSIDE_TO_OUTSIDE': 2},
        'review': {'elapsed_days': 1., 'required_days': 14,
                   'required_closed_pairs': 50, 'ready': False},
    }


def write_status(tmp_path, data=None):
    path = tmp_path / 'status.json'
    path.write_text(json.dumps(saved_status() if data is None else data), encoding='utf-8')
    return path


def test_reader_never_opens_source_database_or_creates_missing_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(api.sqlite3, 'connect', lambda *a, **k: pytest.fail('Source DB access in UI'))
    missing = tmp_path / 'missing' / 'status.json'
    result = api.api_breakout_recheck_shadow(missing, now_ms=NOW)
    assert result['health'] == 'UNAVAILABLE'
    assert 'counts' not in result and 'pnl' not in result
    assert not missing.parent.exists()
    path = write_status(tmp_path)
    before, modified = path.read_bytes(), path.stat().st_mtime_ns
    result = api.api_breakout_recheck_shadow(path, now_ms=NOW + 1000)
    assert result['health'] == 'OK' and result['age_seconds'] == 1
    assert result['generated_at_ms'] == NOW
    assert result['pnl']['control_net_usdt'] == -4.5
    assert path.read_bytes() == before and path.stat().st_mtime_ns == modified


def test_stale_and_degraded_preserve_original_numbers_and_generated_clock(tmp_path):
    data = saved_status()
    data.update(health='DEGRADED', errors=['ENTRY_EVIDENCE_DELAYED'])
    path = write_status(tmp_path, data)
    assert api.api_breakout_recheck_shadow(path, now_ms=NOW)['health'] == 'DEGRADED'
    result = api.api_breakout_recheck_shadow(path, now_ms=NOW + 901_000)
    assert result['health'] == 'STALE' and result['generated_at_ms'] == NOW
    assert result['pnl'] == data['pnl'] and result['errors'] == data['errors']


@pytest.mark.parametrize('patch', [
    {'study': 'wrong-study'}, {'mode': 'LIVE'}, {'order_capability': True},
    {'order_capability': 0}, {'cohort': 'older-cohort'}, {'health': 'OKAY'},
    {'registered_ms': 0}, {'registered_ms': NOW + 1},
    {'generated_at_ms': NOW + 60_001}, {'generated_at_ms': True},
    {'generated_at_ms': float('nan')}, {'generated_at_ms': '2000000000'},
    {'counts': []}, {'pnl': None}, {'review': None}, {'transitions': []}, {'errors': 'bad'},
])
def test_invalid_identity_or_status_is_unavailable(tmp_path, patch):
    data = saved_status()
    data.update(patch)
    result = api.api_breakout_recheck_shadow(write_status(tmp_path, data), now_ms=NOW)
    assert result['health'] == 'UNAVAILABLE' and 'pnl' not in result


@pytest.mark.parametrize('section,key,value', [
    ('counts', 'rejected', -1), ('counts', 'kept', True), ('counts', 'sources', 1.5),
    ('pnl', 'delta_usdt', float('inf')), ('pnl', 'lost_profits_usdt', float('nan')),
    ('pnl', 'control_net_usdt', '1.5'), ('pnl', 'avoided_losses_usdt', False),
    ('review', 'elapsed_days', -1), ('review', 'elapsed_days', float('nan')),
    ('review', 'ready', 1), ('review', 'ready', True),
    ('review', 'required_days', 1), ('review', 'required_closed_pairs', 5),
])
def test_malformed_numbers_or_premature_review_are_refused(tmp_path, section, key, value):
    data = saved_status()
    data[section][key] = value
    assert api.api_breakout_recheck_shadow(write_status(tmp_path, data), now_ms=NOW)['health'] == 'UNAVAILABLE'


def test_empty_valid_series_and_completed_review_are_distinct(tmp_path):
    data = saved_status()
    empty = copy.deepcopy(data)
    empty['counts'] = dict.fromkeys(data['counts'], 0)
    empty['pnl'] = dict.fromkeys(data['pnl'], 0.)
    result = api.api_breakout_recheck_shadow(write_status(tmp_path, empty), now_ms=NOW)
    assert result['health'] == 'OK' and result['counts']['closed_pairs'] == 0
    data['counts']['closed_pairs'] = 50
    data['review'].update(elapsed_days=14., ready=True)
    assert api.api_breakout_recheck_shadow(write_status(tmp_path, data), now_ms=NOW)['review']['ready'] is True


@pytest.mark.parametrize('raw', [b'{', b'[]', b'\xff', b' ' * (512 * 1024 + 1),
                               b'[' * 2000 + b']' * 2000],
                         ids=['broken', 'array', 'nonutf8', 'oversized', 'too_deep'])
def test_broken_or_oversized_saved_status_is_unavailable(tmp_path, raw):
    path = tmp_path / 'status.json'
    path.write_bytes(raw)
    assert api.api_breakout_recheck_shadow(path, now_ms=NOW)['health'] == 'UNAVAILABLE'


def test_read_is_bounded_even_if_source_file_grows():
    reads = []

    class RecordedFile(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    class Source:
        def open(self, mode):
            assert mode == 'rb'
            return RecordedFile(b'x' * (2 * 1024 * 1024))

    assert api.api_breakout_recheck_shadow(Source(), now_ms=NOW)['health'] == 'UNAVAILABLE'
    assert reads == [512 * 1024 + 1]
