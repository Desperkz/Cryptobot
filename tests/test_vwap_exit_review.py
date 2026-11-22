import importlib.util
import json
from pathlib import Path
import pytest

from vwap_exit_review_api import api_vwap_exit_review,EXPECTED_SHA256

ROOT=Path(__file__).resolve().parents[1]
DOCS=ROOT/'docs/research/vwap_exits_20261122'
SOURCE=ROOT/'docs/research/diversified_v4_20261122'
spec=importlib.util.spec_from_file_location('vwap_review',ROOT/'scripts/vwap_exit_review.py')
review=importlib.util.module_from_spec(spec);spec.loader.exec_module(review)


def test_all_26_control_exits_and_costs_reproduce_and_both_exit_changes_fail():
    d=review.review(DOCS,SOURCE)
    assert d==json.loads((DOCS/'review.json').read_text(encoding='utf-8'))
    assert d['parity_validated']==26 and not d['unknown']
    assert d['diagnosis']=={'source_losers':15,'losers_closed_at_1r_before_exit':1,'losers_closed_at_half_r_before_exit':2}
    for v in d['variants']:
        assert not v['historical_launch_checks_passed'] and v['paired_delta_usdt']<0 and v['stress10']['net_usdt']<0
        assert v['improved_pairs']+v['worsened_pairs']+v['unchanged_pairs']==26
        assert v['gained_usdt']-v['given_up_usdt']==pytest.approx(v['paired_delta_usdt'])


def test_control_sequence_or_cost_mismatch_cannot_be_counted_as_improvement():
    row={'closed_ms':100,'reason':'STOP','exit_price':90,'net_usdt':-2,'costs':{'fees':.1}}
    replay={**row,'costs':json.dumps(row['costs'])}
    review.parity(replay,row)
    with pytest.raises(ValueError,match='SEQUENCE_MISMATCH'):review.parity({**replay,'closed_ms':101},row)
    with pytest.raises(ValueError,match='PNL_MISMATCH'):review.parity({**replay,'net_usdt':0},row)
    with pytest.raises(ValueError,match='COST_MISMATCH'):review.parity({**replay,'costs':'{"fees":0}'},row)


def test_changed_frozen_minutes_are_rejected_before_replay(tmp_path):
    (tmp_path/'declaration.json').write_bytes((DOCS/'declaration.json').read_bytes())
    (tmp_path/'minutes.json.gz').write_bytes(b'changed')
    with pytest.raises(ValueError,match='FROZEN_MINUTES_CHANGED'):review.review(tmp_path,SOURCE)


def test_passive_api_preserves_dated_snapshot_and_source_bytes(monkeypatch):
    p=DOCS/'summary.json';raw=p.read_bytes();d=api_vwap_exit_review(p)
    assert d['health']=='OK' and d['is_historical_snapshot'] and d['artifact_sha256']==EXPECTED_SHA256
    assert not d['live_variants'] and not d['orders_possible'] and d['decision']=='BOTH_EXIT_RULES_REJECTED'
    assert p.read_bytes()==raw
    monkeypatch.setenv('BOT_ROOT',str(ROOT));assert api_vwap_exit_review()==d


@pytest.mark.parametrize('raw',[b'{}',b'x'*65537],ids=['changed','oversized'])
def test_missing_or_unverified_report_has_no_result_rows(tmp_path,raw):
    p=tmp_path/'status.json';p.write_bytes(raw)
    assert api_vwap_exit_review(p)['health']=='UNAVAILABLE'
    assert 'variants' not in api_vwap_exit_review(p)
    assert api_vwap_exit_review(tmp_path/'absent')['health']=='UNAVAILABLE'
