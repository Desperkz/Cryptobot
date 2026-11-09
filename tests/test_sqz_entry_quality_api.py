import copy
import json

import pytest

from paper_pilots_api import api_sqz_entry_quality_shadow, api_research_storage

NOW = 2_000_000


def quality():
    return {'study': 'sqz-entry-quality-shadow-v1', 'cohort': '2026-11-09-sqz-entry-quality-v1',
            'mode': 'SHADOW', 'order_capability': False, 'health': 'OK', 'errors': [],
            'registered_ms': 1_000_000, 'generated_at_ms': NOW, 'transitions': {},
            'counts': {k: 0 for k in ('sources', 'waiting_entry', 'decided', 'kept', 'rejected',
                       'unscorable', 'open_controls', 'closed_pairs')},
            'pnl': {k: 0. for k in ('control_net_usdt', 'recheck_net_usdt', 'delta_usdt',
                                  'avoided_losses_usdt', 'lost_profits_usdt')},
            'review': {'elapsed_days': .01, 'required_days': 14, 'required_closed_pairs': 50,
                       'accepted_closed': 0, 'ready': False},
            'quality_rule': {'min_volume_ratio': 2.5, 'min_breakout_atr': .3,
                             'comparison': 'AND', 'inclusive': True},
            'quality_buckets': [{'bucket': k, 'n_sources': 0, 'n_closed': 0,
                'control_net_usdt': 0., 'recheck_net_usdt': 0.,
                'avoided_losses_usdt': 0., 'lost_profits_usdt': 0.}
                for k in ('BOTH_PASS', 'VOLUME_ONLY_FAIL', 'BREAKOUT_ONLY_FAIL', 'BOTH_FAIL')]}


def saved(tmp_path, value):
    p = tmp_path/'status.json'
    p.write_text(json.dumps(value))
    return p


def test_zero_and_stale_do_not_invent_a_profit(tmp_path):
    p = saved(tmp_path, quality())
    assert api_sqz_entry_quality_shadow(p, now_ms=NOW)['health'] == 'OK'
    before = p.read_bytes()
    result = api_sqz_entry_quality_shadow(p, now_ms=NOW+901_000)
    assert result['health'] == 'STALE' and result['pnl']['delta_usdt'] == 0
    assert result['generated_at_ms'] == NOW and p.read_bytes() == before


@pytest.mark.parametrize('mutation', [
    lambda d: d.update(study='sqz-entry-recheck-shadow-v1'),
    lambda d: d.update(order_capability=True),
    lambda d: d['quality_rule'].update(min_volume_ratio=1.5),
    lambda d: d['review'].update(ready=True),
    lambda d: d['review'].update(accepted_closed=1),
    lambda d: d['pnl'].update(delta_usdt=float('inf')),
    lambda d: d['counts'].update(kept=True),
    lambda d: d['quality_buckets'].pop(),
    lambda d: d['quality_buckets'][1].update(bucket='BOTH_PASS'),
    lambda d: d['quality_buckets'][0].update(n_closed=1),
    lambda d: d.update(generated_at_ms=NOW+61_000),
])
def test_wrong_or_corrupt_study_is_unavailable(tmp_path, mutation):
    d = quality();mutation(d)
    result = api_sqz_entry_quality_shadow(saved(tmp_path, d), now_ms=NOW)
    assert result['health'] == 'UNAVAILABLE' and 'pnl' not in result


def test_missing_and_oversized_status_is_not_zero(tmp_path):
    p = tmp_path/'missing.json'
    assert api_sqz_entry_quality_shadow(p)['health'] == 'UNAVAILABLE'
    p.write_bytes(b' '*(512*1024+1))
    assert api_sqz_entry_quality_shadow(p)['health'] == 'UNAVAILABLE'


def test_quality_decision_may_precede_entry_bar(tmp_path):
    d = quality();d['counts'].update(sources=1, decided=1, kept=1, waiting_entry=1, open_controls=1)
    d['quality_buckets'][0]['n_sources'] = 1
    result = api_sqz_entry_quality_shadow(saved(tmp_path, d), now_ms=NOW)
    assert result['health'] == 'OK' and result['counts']['closed_pairs'] == 0


def test_storage_status_stale_and_errors_preserve_numbers(tmp_path):
    d = {'study': 'research-storage-maintenance-v1', 'order_capability': False,
         'health': 'OK', 'errors': [], 'generated_at_ms': NOW, 'last_success_ms': NOW,
         'free_disk_bytes': 2*1024**3}
    p = saved(tmp_path, d)
    assert api_research_storage(p, now_ms=NOW)['health'] == 'OK'
    assert api_research_storage(p, now_ms=NOW+49*3600_000)['health'] == 'STALE'
    d['health'] = 'DEGRADED';d['errors'] = ['DISK_HEADROOM_BELOW_1GIB']
    assert api_research_storage(saved(tmp_path, d), now_ms=NOW)['health'] == 'DEGRADED'
    d['last_success_ms'] = NOW+1
    assert api_research_storage(saved(tmp_path, d), now_ms=NOW)['health'] == 'UNAVAILABLE'
