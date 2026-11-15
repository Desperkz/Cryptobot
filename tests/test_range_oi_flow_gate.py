from __future__ import annotations

import copy
import json

import pytest

from trading_bot.research.range_oi_flow_gate import (
    MAX_OI_ROWS, MAX_ISSUE_ROWS, MINUTE, RULE, evaluate_gate,
)


T = 1_800_000_000_000 + 20_000
M = T//MINUTE*MINUTE-MINUTE


def oi(when, value, symbol='BTCUSDT'):
    return {'symbol': symbol, 'received_ms': when,
            'payload': {'request_started_ms': when-250, 'received_ms': when,
                        'data': {'symbol': symbol, 'openInterest': str(value), 'time': when-100}}}


def segment(minute, first_id, last_id, *, buys=500, sells=100, session='one'):
    return {'session': session, 'symbol': 'BTCUSDT', 'minute_ms': minute,
            'payload': {'first_id': first_id, 'last_id': last_id,
                        'messages': last_id-first_id+1,
                        'first_event_ms': minute+1000, 'last_event_ms': minute+59_000,
                        'first_received_ms': minute+1100, 'last_received_ms': minute+59_100,
                        'buy_quote': buys, 'sell_quote': sells,
                        'coverage': 'OBSERVED_SEGMENT_NOT_CERTIFIED_FULL_MINUTE'}}


def context():
    return {'oi_samples': [oi(T-310_000, 1000), oi(T-10_000, 990)],
            'segments': [segment(M-MINUTE, 100, 109), segment(M, 110, 119),
                         segment(M+MINUTE, 120, 125)],
            'connections': [{'session': 'one', 'started_ms': M-1_000_000,
                             'last_received_ms': T, 'ended_ms': None, 'reason': None}],
            'issues': [], 'collector_registered_ms': T-10_000_000}


def evaluate(c=None, direction='LONG'):
    return evaluate_gate('BTCUSDT', direction, T, **(context() if c is None else c))


def test_accepts_only_both_confirmations_and_describes_observed_interval():
    result = evaluate()
    assert result['decision'] == 'ACCEPT'
    assert result['accepted'] is True and result['data_quality'] == 'PASS'
    assert result['evidence']['oi']['observed_interval_ms'] == 300_000
    assert result['evidence']['flow']['minute_ms'] == M
    assert result['evidence']['flow']['coverage'].startswith('CONTIGUOUS_SAME_SESSION')
    assert 'not all exchange' in RULE['market_scope']
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(('old', 'new', 'buys', 'sells', 'direction', 'reason'), [
    (1000, 990, 100, 500, 'SHORT', 'CONFIRMED'),
    (1000, 1001, 500, 100, 'LONG', 'OI_NOT_DECLINING'),
    (1000, 1000, 500, 100, 'LONG', 'OI_NOT_DECLINING'),
    (1000, 990, 100, 500, 'LONG', 'FLOW_NOT_CONFIRMING'),
    (1000, 990, 500, 500, 'LONG', 'FLOW_NOT_CONFIRMING'),
    (1000, 1001, 100, 500, 'LONG', 'OI_AND_FLOW_NOT_CONFIRMING'),
])
def test_fixed_decline_and_side_rules(old, new, buys, sells, direction, reason):
    c = context(); c['oi_samples'] = [oi(T-310_000, old), oi(T-10_000, new)]
    c['segments'][1]['payload'].update(buy_quote=buys, sell_quote=sells)
    result = evaluate(c, direction)
    assert result['reason'] == reason
    assert result['decision'] == ('ACCEPT' if reason == 'CONFIRMED' else 'REJECT')


def test_adding_unavailable_future_oi_cannot_change_decision_or_evidence():
    c = context(); before = evaluate(c)
    future = oi(T+1, 1)
    future['payload']['data']['openInterest'] = 'NaN'  # Entire response unavailable at T.
    c['oi_samples'].append(future)
    assert evaluate(c) == before


def test_samples_after_baseline_anchor_cannot_rewrite_previous_endpoint():
    c = context(); c['oi_samples'].append(oi(T-299_999, 1))
    result = evaluate(c)
    assert result['decision'] == 'ACCEPT'
    assert result['evidence']['oi']['previous'] == 1000


def test_target_aggregate_with_later_receipts_is_unknown_not_future_profit_filter():
    c = context(); c['segments'][1]['payload']['last_received_ms'] = T+1
    result = evaluate(c)
    assert result['decision'] == 'UNKNOWN'
    assert result['reason'] == 'FLOW_LATE_MUTATED_AGGREGATE'
    assert result['accepted'] is None


def test_successor_can_have_future_totals_but_uses_only_known_first_boundary():
    c = context(); original = evaluate(c)
    c['segments'][2]['payload'].update(last_received_ms=T+500_000,
                                     last_id=9000, messages=8881, buy_quote=1e30,
                                     sell_quote=0, last_event_ms=T+499_000)
    assert evaluate(c) == original


def test_successor_not_yet_observed_makes_last_minute_uncertified():
    c = context(); c['segments'][2]['payload']['first_received_ms'] = T+1
    assert evaluate(c)['decision'] == 'UNKNOWN'


@pytest.mark.parametrize('which', [0, 2])
def test_missing_adjacent_minute_cannot_be_treated_as_full_tape(which):
    c = context(); c['segments'].pop(which)
    assert evaluate(c)['reason'] == 'FLOW_SEGMENT_MISSING_OR_SPLIT'


@pytest.mark.parametrize(('field', 'value'), [
    ('messages', 9), ('messages', 0), ('first_id', 111), ('last_id', 118),
    ('first_event_ms', M-1), ('last_event_ms', M+MINUTE),
    ('buy_quote', 'NaN'), ('sell_quote', -1), ('buy_quote', '1e5000'),
    ('first_received_ms', M-100_000),
])
def test_invalid_or_noncontiguous_segment_is_unknown(field, value):
    c = context(); c['segments'][1]['payload'][field] = value
    result = evaluate(c)
    assert result['decision'] == 'UNKNOWN'
    json.dumps(result, allow_nan=False)


def test_boundary_id_gap_is_detected_without_a_recorded_issue():
    c = context(); c['segments'][0]['payload']['last_id'] = 108
    assert evaluate(c)['reason'] == 'FLOW_UNCERTIFIED_BOUNDARY_IDS'


def test_reconnection_splits_target_minute_even_if_each_half_looks_valid():
    c = context(); duplicate = copy.deepcopy(c['segments'][1]); duplicate['session'] = 'two'
    c['segments'].append(duplicate)
    assert evaluate(c)['reason'] == 'FLOW_SEGMENT_MISSING_OR_SPLIT'


@pytest.mark.parametrize('kind', [
    'AGGREGATE_ID_GAP', 'INVALID_STREAM_EVENT', 'WEBSOCKET_DISCONNECTED', 'PROCESS_RESTART_GAP',
])
def test_bad_context_stays_unknown_instead_of_rejection(kind):
    c = context(); c['issues'] = [{'kind': kind, 'symbol': '', 'at_ms': T-5000, 'detail': '{}'}]
    result = evaluate(c)
    assert result['decision'] == 'UNKNOWN' and result['data_quality'] == 'UNKNOWN'
    assert result['reason'] == 'CONTEXT_'+kind


def test_future_or_other_symbol_issues_do_not_change_a_decision():
    c = context(); before = evaluate(c)
    c['issues'] = [{'kind': 'AGGREGATE_ID_GAP', 'symbol': 'BTCUSDT', 'at_ms': T+1},
                   {'kind': 'AGGREGATE_ID_GAP', 'symbol': 'ETHUSDT', 'at_ms': T-1}]
    assert evaluate(c) == before


@pytest.mark.parametrize(('field', 'value'), [
    ('started_ms', M+1), ('last_received_ms', M+MINUTE-1), ('ended_ms', M+MINUTE-1),
])
def test_connection_must_cover_whole_target_and_observed_successor(field, value):
    c = context(); c['connections'][0][field] = value
    assert evaluate(c)['decision'] == 'UNKNOWN'


def test_later_connection_lifecycle_metadata_does_not_change_past_feature_values():
    c = context(); before = evaluate(c)
    c['connections'][0].update(last_received_ms=T+100000, ended_ms=T+100000, reason='CLEAN_STOP')
    assert evaluate(c) == before


@pytest.mark.parametrize('which', [0, 1])
def test_each_oi_endpoint_must_be_fresh(which):
    c = context()
    anchor = T-300_000 if which == 0 else T
    c['oi_samples'][which] = oi(anchor-90_001, 1000 if which == 0 else 990)
    assert evaluate(c)['decision'] == 'UNKNOWN'


def test_oi_endpoint_equality_at_same_receive_time_is_ambiguous():
    c = context(); c['oi_samples'].append(oi(T-10_000, 980))
    assert evaluate(c)['reason'] == 'OI_ENDPOINT_AMBIGUOUS'


def test_oi_timestamps_and_symbols_are_validated():
    c = context(); c['oi_samples'][1]['payload']['received_ms'] += 1
    assert evaluate(c)['reason'] == 'OI_RECEIPT_TIME_MISMATCH'
    c = context(); c['oi_samples'][1]['payload']['data']['symbol'] = 'ETHUSDT'
    assert evaluate(c)['reason'] == 'OI_SYMBOL_MISMATCH'


def test_recent_registration_and_truncated_context_remain_unknown():
    c = context(); c['collector_registered_ms'] = T-300_001
    assert evaluate(c)['reason'] == 'COLLECTOR_REGISTRATION_AFTER_REQUIRED_CONTEXT'
    c = context(); c['oi_samples'] *= MAX_OI_ROWS
    assert evaluate(c)['reason'] == 'MISSING_OR_OVERSIZED_CONTEXT'
    c = context(); c['issues'] = [{}] * (MAX_ISSUE_ROWS+1)
    assert evaluate(c)['reason'] == 'MISSING_OR_OVERSIZED_CONTEXT'


def test_only_oi_rest_failure_is_relevant_to_oi_flow_gate():
    c = context(); c['issues'] = [{'kind': 'REST_ERROR', 'symbol': 'BTCUSDT', 'at_ms': T-1,
                                 'detail': '{"kind":"book5"}'}]
    assert evaluate(c)['decision'] == 'ACCEPT'
    c['issues'][0]['detail'] = '{"kind":"open_interest"}'
    assert evaluate(c)['reason'] == 'CONTEXT_OI_COLLECTION_ERROR'


def test_missing_data_is_unknown_even_if_known_oi_already_fails():
    c = context(); c['oi_samples'][1] = oi(T-10_000, 1001); c['segments'] = []
    assert evaluate(c)['decision'] == 'UNKNOWN'


def test_individually_finite_extreme_oi_cannot_produce_nonfinite_json_evidence():
    c = context(); c['oi_samples'] = [oi(T-310_000, '1e-308'), oi(T-10_000, '1e308')]
    result = evaluate(c)
    assert result['decision'] == 'UNKNOWN'
    assert result['reason'] == 'OI_DERIVED_VALUE_NONFINITE'
    json.dumps(result, allow_nan=False)


def test_input_dictionary_and_embedded_json_forms_agree_and_are_not_mutated():
    c = context(); snapshot = copy.deepcopy(c); expected = evaluate(c)
    assert c == snapshot
    for row in c['oi_samples']+c['segments']:
        row['payload'] = json.dumps(row['payload'])
    assert evaluate(c) == expected


@pytest.mark.parametrize(('direction', 'decision'), [('SIDEWAYS', T), ('LONG', True)])
def test_invalid_source_identity_is_unknown(direction, decision):
    assert evaluate_gate('BTCUSDT', direction, decision, **context())['decision'] == 'UNKNOWN'
