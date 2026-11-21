import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from range_profit_protection_api import RULE, STUDY, api_range_profit_protection_shadow

NOW = 2_000_000
ROOT = Path(__file__).resolve().parents[1]


def sample():
    return {'study': STUDY, 'mode': 'SHADOW', 'order_capability': False,
            'combined_with_main_balance': False, 'health': 'OK',
            'registered_ms': 1_000_000, 'generated_at_ms': NOW,
            'source': {'health': 'OK', 'age_seconds': 3}, 'protocol': dict(RULE),
            'counts': {'observed': 0, 'eligible': 0, 'skipped': 0, 'unscorable': 0,
                       'open_pairs': 0, 'closed_pairs': 0},
            'results': {'paired_control_net_usdt': 0., 'protected_net_usdt': 0.,
                        'paired_delta_usdt': 0., 'improved_pairs': 0, 'worsened_pairs': 0,
                        'unchanged_pairs': 0, 'unscorable_control_net_usdt': 0.,
                        'unscorable_controls_closed': 0},
            'review': {'required_days': 14, 'required_closed_pairs': 50,
                       'elapsed_days': 0., 'ready': False, 'automatic_promotion': False},
            'recent_pairs': [], 'errors': [],
            'limitations': ['Closed-minute model; execution at the original entry is not proven.']}


def mixed():
    data = sample()
    data['counts'].update(observed=5, eligible=3, skipped=1, unscorable=1, open_pairs=1, closed_pairs=2)
    data['results'].update(paired_control_net_usdt=3., protected_net_usdt=4., paired_delta_usdt=1.,
                           improved_pairs=1, worsened_pairs=1, unscorable_control_net_usdt=-2.,
                           unscorable_controls_closed=1)
    pair = {'id': 7, 'symbol': 'LTCUSDT', 'direction': 'LONG', 'opened_ms': 1_100_000,
            'admission': 'ELIGIBLE', 'reason': 'NEW_SOURCE', 'state': 'CLOSED',
            'armed_ms': 1_200_000, 'control_net_usdt': 2., 'protected_net_usdt': 3.,
            'delta_usdt': 1., 'control_reason': 'TARGET', 'protected_reason': 'PROTECTED_STOP', 'error': None}
    unknown = {**pair, 'id': 8, 'admission': 'UNKNOWN', 'reason': 'MISSING_MINUTE',
               'state': 'UNKNOWN', 'armed_ms': None, 'control_net_usdt': -2.,
               'protected_net_usdt': None, 'delta_usdt': None, 'protected_reason': None}
    incomplete = {**pair, 'id': 9, 'state': 'OPEN', 'control_net_usdt': None,
                  'protected_net_usdt': 1., 'delta_usdt': None, 'control_reason': None}
    data['recent_pairs'] = [pair, unknown, incomplete]
    return data


def save(tmp_path, data):
    path = tmp_path / 'status.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_reader_is_passive_and_preserves_saved_results_when_stale(tmp_path):
    path = save(tmp_path, mixed())
    raw = path.read_bytes()
    good = api_range_profit_protection_shadow(path, now_ms=NOW)
    assert good['health'] == 'OK' and good['results']['paired_delta_usdt'] == 1
    assert api_range_profit_protection_shadow(path, now_ms=NOW + 900_000)['health'] == 'OK'
    stale = api_range_profit_protection_shadow(path, now_ms=NOW + 900_001)
    assert stale['health'] == 'STALE' and stale['results']['protected_net_usdt'] == 4
    assert path.read_bytes() == raw


def test_default_directory_uses_optional_environment_override(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    path = tmp_path / 'data/status.json'
    path.write_text(json.dumps(sample()))
    monkeypatch.setenv('RANGE_PROFIT_PROTECTION_DIR', str(tmp_path))
    assert api_range_profit_protection_shadow(now_ms=NOW)['health'] == 'OK'


@pytest.mark.parametrize('mutation', [
    lambda d: d.update(order_capability=True),
    lambda d: d.update(combined_with_main_balance=True),
    lambda d: d.update(mode='LIVE'),
    lambda d: d.update(study='old'),
    lambda d: d.update(registered_ms=True),
    lambda d: d.update(generated_at_ms=NOW + 60_001),
    lambda d: d['protocol'].update(arm_at_r=.5),
    lambda d: d['protocol'].update(arm_on='INTRAMINUTE_HIGH'),
    lambda d: d['protocol'].update(effective='SAME_MINUTE'),
    lambda d: d['protocol'].update(fees_bps_per_side=0),
    lambda d: d['counts'].update(observed=True),
    lambda d: d['counts'].update(observed=6),
    lambda d: d['counts'].update(open_pairs=0),
    lambda d: d['results'].update(improved_pairs=0),
    lambda d: d['results'].update(paired_delta_usdt=99),
    lambda d: d['results'].update(protected_net_usdt=float('nan')),
    lambda d: d['results'].update(unscorable_controls_closed=2),
    lambda d: d['review'].update(ready=True),
    lambda d: d['review'].update(automatic_promotion=True),
    lambda d: d.update(health='OK', source={'health': 'UNAVAILABLE', 'age_seconds': None}),
    lambda d: d['recent_pairs'][0].update(delta_usdt=4),
    lambda d: d['recent_pairs'][1].update(protected_net_usdt=2),
    lambda d: d['recent_pairs'][2].update(delta_usdt=1),
    lambda d: d['recent_pairs'][2].update(state='UNKNOWN'),
    lambda d: d.update(errors=[False]),
    lambda d: d.update(limitations='missing'),
])
def test_bad_protocol_or_incomplete_pairs_cannot_be_presented_as_profit(tmp_path, mutation):
    data = mixed()
    mutation(data)
    result = api_range_profit_protection_shadow(save(tmp_path, data), now_ms=NOW)
    assert result['health'] == 'UNAVAILABLE' and 'results' not in result


def test_zero_pairs_cannot_have_pnl_and_unknowns_stay_separate(tmp_path):
    data = sample()
    data['counts'].update(observed=1, unscorable=1)
    data['results'].update(unscorable_controls_closed=1, unscorable_control_net_usdt=-2.)
    good = api_range_profit_protection_shadow(save(tmp_path, data), now_ms=NOW)
    assert good['health'] == 'OK' and good['results']['paired_delta_usdt'] == 0
    data['results'].update(protected_net_usdt=5., paired_delta_usdt=5.)
    assert api_range_profit_protection_shadow(save(tmp_path, data), now_ms=NOW)['health'] == 'UNAVAILABLE'


def test_missing_duplicate_and_oversized_status_are_unavailable(tmp_path):
    path = tmp_path / 'absent.json'
    assert api_range_profit_protection_shadow(path, now_ms=NOW)['health'] == 'UNAVAILABLE'
    path.write_bytes(b' ' * (128 * 1024 + 1))
    assert api_range_profit_protection_shadow(path, now_ms=NOW)['health'] == 'UNAVAILABLE'
    path.write_text(json.dumps(sample())[:-1] + ',"health":"OK"}')
    assert api_range_profit_protection_shadow(path, now_ms=NOW)['health'] == 'UNAVAILABLE'


def test_route_is_registered():
    import bot_control_v2
    assert bot_control_v2.ROUTES['/range-profit-protection-shadow'] is api_range_profit_protection_shadow


def test_dashboard_keeps_unknowns_and_half_closed_pairs_out_of_effect():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is not installed')
    html = (ROOT / 'dashboard_v2.html').read_text(encoding='utf-8')
    match = re.search(r'function renderRangeProfitProtection\(\)\{[\s\S]*?\n\}\n(?=function renderRangeOiFlow)', html)
    assert match
    unknown = sample()
    unknown['counts'].update(observed=1, unscorable=1)
    unknown['results'].update(unscorable_controls_closed=1, unscorable_control_net_usdt=-2.)
    cases = [sample(), unknown, mixed(), {'health': 'UNAVAILABLE'}]
    script = '''
const assert=require('assert');let cache={},errors=new Map(),nodes={};
const $=id=>nodes[id]??(nodes[id]={textContent:'',className:'',hidden:true,innerHTML:''});
const setHTML=(id,s)=>$(id).innerHTML=s;
const esc=x=>String(x).replaceAll('<','&lt;'),signedFmt=x=>(x>=0?'+':'')+Number(x).toFixed(2),msTime=x=>String(x),pnlCls=x=>x<0?'negative':'positive';
''' + match.group() + '\nconst cases=' + json.dumps(cases) + ''';
for(let i=0;i<cases.length;i++){
 nodes={};cache={'/range-profit-protection-shadow':cases[i]};renderRangeProfitProtection();
 if(i<2)assert.strictEqual($('range-protection-delta').textContent,'—');
 if(i===1){assert($('range-protection-unknown').textContent.includes('-2.00'));assert($('range-protection-counts').innerHTML.includes('Не удалось оценить'));}
 if(i===2){assert.strictEqual($('range-protection-control').textContent,'+3.00');assert.strictEqual($('range-protection-variant').textContent,'+4.00');assert.strictEqual($('range-protection-delta').textContent,'+1.00');assert($('range-protection-pairs').innerHTML.includes('Пара ещё не завершена'));}
 if(i===3){assert.strictEqual($('range-protection-control').textContent,'—');assert(!$('range-protection-alert').hidden);}
}
cache={'/range-profit-protection-shadow':{...cases[2],health:'STALE'}};renderRangeProfitProtection();
assert($('range-protection-status').textContent.includes('Данные задержаны'));
assert(!$('range-protection-alert').hidden);
const malicious=structuredClone(cases[2]);malicious.recent_pairs[0].symbol='<script>';cache={'/range-profit-protection-shadow':malicious};renderRangeProfitProtection();
assert(!$('range-protection-pairs').innerHTML.includes('<script>'));
'''
    result = subprocess.run([node, '-'], input=script, text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
