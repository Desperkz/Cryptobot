import importlib.util
import json
from pathlib import Path

import pytest

from diversified_v4_review_api import api_diversified_v4_review,EXPECTED_SHA256

ROOT=Path(__file__).resolve().parents[1]
DOCS=ROOT/'docs/research/diversified_v4_20261122'
spec=importlib.util.spec_from_file_location('v4review',ROOT/'scripts/diversified_v4_review.py')
review=importlib.util.module_from_spec(spec);spec.loader.exec_module(review)


def reproduce():
    return review.review(DOCS/'declaration.json',DOCS/'historical_features.json',DOCS/'source_outcomes.json')


def test_full_saved_comparison_reproduces_and_all_255_costs_and_inputs_match():
    assert reproduce()==json.loads((DOCS/'historical_review.json').read_text(encoding='utf-8'))


def test_lower_total_loss_is_not_a_success_when_average_trade_is_worse():
    data=reproduce()
    assert data['parity_validated']==255 and data['unknown']==0
    for v in data['variants']:
        assert not v['historical_launch_checks_passed'] and not v['paper_promotion']
        assert v['prevented_losses_usdt']-v['missed_profit_usdt']==pytest.approx(v['paired_delta_usdt'])
        assert v['accepted']['closed']+v['rejected']['closed']==v['control']['closed']
    vwap,sweep,momentum=data['variants']
    assert vwap['accepted']['net_usdt']>0 and vwap['stress10']['net_usdt']<0
    for v in (sweep,momentum):
        assert v['paired_delta_usdt']>0
        assert v['accepted']['average_r']<v['control']['average_r']


def test_mismatched_original_costs_cannot_be_published(tmp_path):
    rows=json.loads((DOCS/'source_outcomes.json').read_text(encoding='utf-8'))
    rows['v3_positions'][0]['net_usdt']+=1
    p=tmp_path/'changed.json';p.write_text(json.dumps(rows),encoding='utf-8')
    with pytest.raises(ValueError,match='CONTROL_NET_MISMATCH'):
        review.review(DOCS/'declaration.json',DOCS/'historical_features.json',p)


def test_saved_gate_decision_cannot_be_changed_after_outcome(tmp_path):
    data=json.loads((DOCS/'historical_features.json').read_text(encoding='utf-8'))
    data['rows'][0]['decision']['accepted']=True
    p=tmp_path/'changed.json';p.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(ValueError,match='FROZEN_FEATURE_EXPORT_CHANGED'):
        review.review(DOCS/'declaration.json',p,DOCS/'source_outcomes.json')


def test_passive_api_serves_dated_verified_failures_without_stale_live_label(tmp_path,monkeypatch):
    p=DOCS/'summary.json';before=p.read_bytes();result=api_diversified_v4_review(p)
    assert result['health']=='OK' and result['artifact_sha256']==EXPECTED_SHA256
    assert result['is_historical_snapshot'] and not result['live_variants']
    assert result['decision']=='NO_VARIANT_PASSED' and result['parity_validated']==255
    assert p.read_bytes()==before
    monkeypatch.setenv('BOT_ROOT',str(ROOT))
    assert api_diversified_v4_review()==result


@pytest.mark.parametrize('raw',[b'{}',b'{"health":"OK","orders_possible":true}',b'x'*65537],ids=['empty','changed','oversized'])
def test_modified_missing_or_oversized_review_never_claims_valid_results(tmp_path,raw):
    p=tmp_path/'summary.json';p.write_bytes(raw)
    result=api_diversified_v4_review(p)
    assert result['health']=='UNAVAILABLE' and 'variants' not in result
    assert api_diversified_v4_review(tmp_path/'missing')['health']=='UNAVAILABLE'
