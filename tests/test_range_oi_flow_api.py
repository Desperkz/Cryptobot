import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from paper_pilots_api import api_range_oi_flow_shadow

NOW = 2_000_000
ROOT = Path(__file__).resolve().parents[1]


def sample():
    return {'study': 'range-oi-flow-shadow-v1', 'mode': 'SHADOW', 'order_capability': False,
            'combined_with_main_balance': False, 'health': 'OK', 'registered_ms': 1_000_000,
            'generated_at_ms': NOW, 'source': {'health': 'OK', 'age_seconds': 4},
            'counts': {'observed': 0, 'accepted': 0, 'rejected': 0, 'unscorable': 0, 'pending': 0, 'closed_pairs': 0},
            'results': {'control_net_usdt': 0., 'accepted_control_net_usdt': 0.,
                        'prevented_losses_usdt': 0., 'missed_profits_usdt': 0., 'paired_delta_usdt': 0.,
                        'all_control_net_usdt': 0., 'unscorable_control_net_usdt': 0.,
                        'known_closed_pairs': 0, 'unscorable_closed_pairs': 0},
            'errors': [], 'limitations': ['Publication may lag the modeled source entry.']}


def mixed():
    value = sample()
    value['counts'] = {'observed': 5, 'accepted': 2, 'rejected': 2, 'unscorable': 1, 'pending': 1, 'closed_pairs': 4}
    value['results'] = {'control_net_usdt': 1., 'accepted_control_net_usdt': 3.,
                        'prevented_losses_usdt': 4., 'missed_profits_usdt': 2., 'paired_delta_usdt': 2.,
                        'all_control_net_usdt': -4., 'unscorable_control_net_usdt': -5.,
                        'known_closed_pairs': 3, 'unscorable_closed_pairs': 1}
    return value


def save(tmp_path, data):
    path = tmp_path / 'status.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_initial_read_is_passive_and_staleness_preserves_results(tmp_path):
    path = save(tmp_path, sample())
    before = path.read_bytes()
    assert api_range_oi_flow_shadow(path, now_ms=NOW)['health'] == 'OK'
    assert api_range_oi_flow_shadow(path, now_ms=NOW + 900000)['health'] == 'OK'
    old = api_range_oi_flow_shadow(path, now_ms=NOW + 900001)
    assert old['health'] == 'STALE' and old['results']['known_closed_pairs'] == 0
    assert path.read_bytes() == before


def test_unknown_control_is_separate_from_matched_effect(tmp_path):
    result = api_range_oi_flow_shadow(save(tmp_path, mixed()), now_ms=NOW)
    assert result['health'] == 'OK'
    r = result['results']
    assert r['control_net_usdt'] == 1 and r['all_control_net_usdt'] == -4
    assert r['paired_delta_usdt'] == r['prevented_losses_usdt'] - r['missed_profits_usdt'] == 2
    assert r['unscorable_control_net_usdt'] == -5


@pytest.mark.parametrize('mutation', [
    lambda d: d.update(order_capability=True),
    lambda d: d.update(mode='LIVE'),
    lambda d: d.update(combined_with_main_balance=True),
    lambda d: d.update(study='old'),
    lambda d: d.update(registered_ms=True),
    lambda d: d.update(generated_at_ms=NOW + 60001),
    lambda d: d.update(health='OK', source={'health': 'UNAVAILABLE', 'age_seconds': None}),
    lambda d: d['counts'].update(observed=True),
    lambda d: d['counts'].update(observed=6),
    lambda d: d['counts'].update(pending=0),
    lambda d: d['results'].update(known_closed_pairs=4),
    lambda d: d['results'].update(unscorable_closed_pairs=True),
    lambda d: d['results'].update(control_net_usdt=float('nan')),
    lambda d: d['results'].update(prevented_losses_usdt=-1),
    lambda d: d['results'].update(missed_profits_usdt=-1),
    lambda d: d['results'].update(paired_delta_usdt=5),
    lambda d: d['results'].update(all_control_net_usdt=1),
    lambda d: d['results'].pop('unscorable_control_net_usdt'),
    lambda d: d.update(limitations='not a list'),
    lambda d: d.update(errors=[1]),
])
def test_bad_protocol_counts_or_accounting_are_not_presented_as_profit(tmp_path, mutation):
    value = mixed()
    mutation(value)
    result = api_range_oi_flow_shadow(save(tmp_path, value), now_ms=NOW)
    assert result['health'] == 'UNAVAILABLE' and 'results' not in result


def test_degraded_source_may_have_unknown_age(tmp_path):
    value = mixed()
    value.update(health='DEGRADED', source={'health': 'UNAVAILABLE', 'age_seconds': None}, errors=['SOURCE_UNAVAILABLE'])
    result = api_range_oi_flow_shadow(save(tmp_path, value), now_ms=NOW)
    assert result['health'] == 'DEGRADED' and result['results']['all_control_net_usdt'] == -4


def test_missing_oversized_and_duplicate_json_are_unavailable(tmp_path):
    path = tmp_path / 'absent.json'
    assert api_range_oi_flow_shadow(path, now_ms=NOW)['health'] == 'UNAVAILABLE'
    path.write_bytes(b' ' * (128 * 1024 + 1))
    assert api_range_oi_flow_shadow(path, now_ms=NOW)['health'] == 'UNAVAILABLE'
    path.write_text(json.dumps(sample())[:-1] + ',"study":"range-oi-flow-shadow-v1"}')
    assert api_range_oi_flow_shadow(path, now_ms=NOW)['health'] == 'UNAVAILABLE'


def test_api_route_is_registered():
    import bot_control_v2
    assert bot_control_v2.ROUTES['/range-oi-flow-shadow'] is api_range_oi_flow_shadow


def test_dashboard_renders_unknown_outcomes_without_inventing_filter_effect():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is not installed')
    html = (ROOT / 'dashboard_v2.html').read_text(encoding='utf-8')
    match = re.search(r'function renderRangeOiFlow\(\)\{[\s\S]*?\n\}\n(?=function renderFundingCarry)', html)
    assert match
    unknown = sample()
    unknown['counts'].update(observed=1, unscorable=1, closed_pairs=1)
    unknown['results'].update(all_control_net_usdt=-5, unscorable_control_net_usdt=-5, unscorable_closed_pairs=1)
    cases = [sample(), unknown, mixed(), {'health': 'UNAVAILABLE'}]
    script = '''
const assert=require('assert');
let cache={},errors=new Map(),nodes={};
const $=id=>nodes[id]??(nodes[id]={textContent:'',className:'',hidden:true,innerHTML:''});
const setHTML=(id,s)=>$(id).innerHTML=s;
const esc=x=>String(x),fmt=x=>Number(x).toFixed(2),signedFmt=x=>(x>=0?'+':'')+Number(x).toFixed(2),msTime=x=>String(x),pnlCls=x=>x<0?'negative':'positive';
''' + match.group() + '\nconst cases=' + json.dumps(cases) + ''';
for(let i=0;i<cases.length;i++){
 nodes={};cache={'/range-oi-flow-shadow':cases[i]};renderRangeOiFlow();
 if(i<2)assert.strictEqual($('range-flow-delta').textContent,'—');
 if(i===1){assert($('range-flow-unknown').textContent.includes('-5.00'));assert($('range-flow-counts').textContent.includes('Не удалось оценить: 1'));}
 if(i===2){assert.strictEqual($('range-flow-control').textContent,'+1.00');assert.strictEqual($('range-flow-delta').textContent,'+2.00');assert($('range-flow-effects').textContent.includes('Потерянная прибыль 2.00'));}
 if(i===3){assert.strictEqual($('range-flow-control').textContent,'—');assert(!$('range-flow-alert').hidden);}
}
cache={'/range-oi-flow-shadow':{...cases[2],health:'STALE'}};renderRangeOiFlow();
assert($('range-flow-status').textContent.includes('Данные задержаны'));
assert(!$('range-flow-alert').hidden);
'''
    completed = subprocess.run([node, '-'], input=script, text=True, capture_output=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
