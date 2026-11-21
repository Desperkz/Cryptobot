"""Boundary and source-parity checks for the predeclared protection rule."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import random

import pytest

from trading_bot.research.range_profit_execution import (
    MINUTE, advance_control, advance_protected, settle, validate_bars,
)


def original_model():
    """Extract only pure original functions, without importing trading modules."""
    path = Path(__file__).parents[1] / 'src/trading_bot/research/diversified_shadow.py'
    source = ast.parse(path.read_text(encoding='utf-8'))
    selected = [node for node in source.body if isinstance(node, ast.FunctionDef)
                and node.name in {'canonical', 'settle', 'advance'}]
    assert len(selected) == 3
    namespace = {'json': json, 'MINUTE': MINUTE}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def plan(direction='LONG', *, entry=100., stop=90., target=125., expires=None):
    first = 180000
    return {'direction': direction, 'entry': entry, 'entry_mid': entry / (1 + (1 if direction == 'LONG' else -1) * .0005),
            'stop': stop, 'target': target, 'quantity': .17, 'risk_usdt': 2.,
            'opened_ms': first - 20000, 'expires_ms': expires or first + 8 * 3600000,
            'next_ms': first}


def bar(at, o, h, l, c):
    return [at, str(o), str(h), str(l), str(c), '1', at + MINUTE - 1, '1', 1, '1', '1', '0']


def state(p):
    return {'next_ms': p['next_ms'], 'armed_ms': None}


def mirror(raw):
    at, o, h, l, c = int(raw[0]), *map(float, raw[1:5])
    return bar(at, 200-o, 200-l, 200-h, 200-c)


def directional(direction, rows, **kwargs):
    p = plan(**kwargs)
    if direction == 'SHORT':
        p = plan('SHORT', stop=110., target=75., expires=p['expires_ms'])
        rows = [mirror(b) for b in rows]
    return p, rows


def test_control_is_exact_source_model_over_many_deterministic_cases():
    source = original_model()
    rng = random.Random(20261117)
    for direction in ('LONG', 'SHORT'):
        for _ in range(150):
            entry = rng.uniform(10, 3000)
            distance = entry * rng.uniform(.002, .08)
            sign = 1 if direction == 'LONG' else -1
            p = plan(direction, entry=entry, stop=entry-sign*distance,
                     target=entry+sign*distance*rng.uniform(1.2, 3),
                     expires=180000+rng.randrange(1, 12)*MINUTE)
            p['quantity'] = rng.uniform(.001, 100)
            rows = []
            previous = entry
            for i in range(15):
                opened = previous + rng.uniform(-1.5, 1.5)*distance
                close = opened + rng.uniform(-1.2, 1.2)*distance
                high = max(opened, close) + rng.random()*distance
                low = min(opened, close) - rng.random()*distance
                rows.append(bar(p['next_ms']+i*MINUTE, opened, high, low, close))
                previous = close
            assert advance_control(p, rows) == source['advance'](p, rows)
            price = rng.uniform(entry*.8, entry*1.2)
            when = p['opened_ms'] + rng.randrange(0, 90000000)
            assert settle(p, price, when, 'TEST') == source['settle'](p, price, when, 'TEST')


@pytest.mark.parametrize('interval', [MINUTE, 900000])
def test_control_retains_source_supported_interval_and_empty_input(interval):
    p = plan()
    rows = [bar(p['next_ms'], 100, 101, 99, 100)]
    rows[0][6] = rows[0][0] + interval - 1
    assert advance_control(p, rows, interval_ms=interval) == original_model()['advance'](p, rows, interval_ms=interval)
    assert advance_control(p, []) == (None, p['next_ms'])
    with pytest.raises(ValueError, match='Unsupported'):
        advance_control(p, [], interval_ms=300000)


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_wick_does_not_arm_but_close_at_exact_one_r_does(direction):
    p, rows = directional(direction, [bar(180000, 101, 112, 99, 109), bar(240000, 105, 112, 104, 110)])
    initial = state(p)
    outcome, after_wick = advance_protected(p, initial, rows[:1])
    assert outcome is None and after_wick['armed_ms'] is None
    outcome, armed = advance_protected(p, after_wick, rows[1:])
    assert outcome is None and armed == {'next_ms': 300000, 'armed_ms': 299999}
    assert initial == state(p)


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_new_stop_cannot_retroactively_close_arming_minute(direction):
    p, rows = directional(direction, [bar(180000, 104, 112, 99, 110), bar(240000, 109, 111, 99, 102)])
    original = deepcopy(p)
    outcome, armed = advance_protected(p, state(p), rows[:1])
    assert outcome is None
    outcome, done = advance_protected(p, armed, rows[1:])
    assert outcome == settle(p, p['entry'], 299999, 'PROTECTED_STOP')
    assert done['armed_ms'] == 239999 and done['next_ms'] == 300000
    assert p == original
    assert outcome['net_usdt'] < 0  # Price-entry protection still pays all source costs.


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
@pytest.mark.parametrize('kind', ['STOP', 'STOP_FIRST_AMBIGUOUS', 'TARGET', 'TIMEOUT'])
def test_original_exit_wins_before_any_arm(direction, kind):
    specs = {'STOP': (100, 112, 89, 110),
             'STOP_FIRST_AMBIGUOUS': (100, 126, 89, 110),
             'TARGET': (100, 126, 99, 110),
             'TIMEOUT': (100, 112, 99, 110)}
    expires = 239999 if kind == 'TIMEOUT' else None
    p, rows = directional(direction, [bar(180000, *specs[kind])], expires=expires)
    outcome, final_state = advance_protected(p, state(p), rows)
    control, cursor = advance_control(p, rows)
    assert outcome == control
    assert outcome['reason'] == kind
    assert final_state == {'next_ms': cursor, 'armed_ms': None}


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_gap_through_protected_stop_uses_adverse_open_and_original_costs(direction):
    p, rows = directional(direction, [bar(180000, 104, 112, 102, 110), bar(240000, 95, 101, 94, 99)])
    outcome, final_state = advance_protected(p, state(p), rows)
    expected_raw = 95. if direction == 'LONG' else 105.
    assert outcome == settle(p, expected_raw, 299999, 'PROTECTED_STOP')
    costs = json.loads(outcome['costs'])
    assert costs['funding_source'] == 'adverse_1bp_per8h_buffer'
    assert costs['fees'] > 0 and costs['exit_slippage'] > 0 and costs['funding'] > 0
    assert final_state['armed_ms'] == 239999


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_protected_stop_first_when_target_also_touched(direction):
    p, rows = directional(direction, [bar(180000, 104, 112, 102, 110), bar(240000, 110, 126, 99, 120)])
    outcome, _ = advance_protected(p, state(p), rows)
    assert outcome == settle(p, p['entry'], 299999, 'PROTECTED_STOP_FIRST_AMBIGUOUS')


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_split_processing_and_serialized_restart_match_one_pass(direction):
    p, rows = directional(direction, [bar(180000, 102, 109, 99, 107), bar(240000, 108, 113, 106, 110),
                                     bar(300000, 111, 118, 108, 115), bar(360000, 108, 109, 98, 99)])
    all_at_once = advance_protected(p, state(p), rows)
    _, first_state = advance_protected(p, state(p), rows[:2])
    reloaded_state = json.loads(json.dumps(first_state))
    after_restart = advance_protected(p, reloaded_state, rows[2:])
    assert after_restart == all_at_once
    assert reloaded_state == first_state
    assert rows == directional(direction, [bar(180000, 102, 109, 99, 107), bar(240000, 108, 113, 106, 110),
                                        bar(300000, 111, 118, 108, 115), bar(360000, 108, 109, 98, 99)])[1]


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_no_arm_matches_original_control_including_timeout_costs(direction):
    p, rows = directional(direction, [bar(180000, 101, 109, 99, 105), bar(240000, 104, 109, 101, 108)], expires=299999)
    outcome, final_state = advance_protected(p, state(p), rows)
    control, cursor = original_model()['advance'](p, rows)
    assert outcome == control and outcome['reason'] == 'TIMEOUT'
    assert final_state == {'next_ms': cursor, 'armed_ms': None}


def test_validator_accepts_seven_fields_and_preserves_extra_fields_without_mutation():
    rows = [bar(180000, 100, 110, 90, 105), bar(240000, 105, 111, 101, 109)[:7]]
    before = deepcopy(rows)
    result = validate_bars(rows, 180000, 299999)
    assert result[0][1:5] == [100., 110., 90., 105.]
    assert result[0][7:] == rows[0][7:] and len(result[1]) == 7
    assert rows == before and result is not rows and result[0] is not rows[0]
    assert validate_bars([], 180000, 179999) == []


@pytest.mark.parametrize('mutation,reason', [
    (lambda b: b.__setitem__(0, 180001), 'MISSING_MINUTE'),
    (lambda b: b.__setitem__(0, 180000.), 'INVALID_BAR_OPEN_TIME'),
    (lambda b: b.__setitem__(6, 240000), 'INVALID_BAR_CLOSE_TIME'),
    (lambda b: b.__setitem__(6, True), 'INVALID_BAR_CLOSE_TIME'),
    (lambda b: b.__setitem__(1, float('nan')), 'INVALID_OHLC_PRICE'),
    (lambda b: b.__setitem__(2, float('inf')), 'INVALID_OHLC_PRICE'),
    (lambda b: b.__setitem__(3, -1), 'INVALID_OHLC_PRICE'),
    (lambda b: b.__setitem__(4, True), 'INVALID_OHLC_PRICE'),
    (lambda b: b.__setitem__(1, 'bad'), 'INVALID_OHLC_PRICE'),
    (lambda b: b.__setitem__(2, 99), 'INVALID_OHLC_RANGE'),
    (lambda b: b.__setitem__(3, 106), 'INVALID_OHLC_RANGE'),
])
def test_validator_refuses_corrupt_or_ambiguous_bars(mutation, reason):
    row = bar(180000, 100, 110, 90, 105)
    mutation(row)
    with pytest.raises(ValueError, match=reason):
        validate_bars([row], 180000, 299999)


def test_validator_refuses_gaps_duplicates_future_and_partial_minutes():
    first = bar(180000, 100, 110, 90, 105)
    with pytest.raises(ValueError, match='MISSING_MINUTE'):
        validate_bars([first, first], 180000, 299999)
    with pytest.raises(ValueError, match='MISSING_MINUTE'):
        validate_bars([first, bar(300000, 100, 110, 90, 105)], 180000, 359999)
    with pytest.raises(ValueError, match='UNFINISHED_OR_FUTURE_MINUTE'):
        validate_bars([first], 180000, 239998)
    with pytest.raises(ValueError, match='START_NOT_MINUTE_ALIGNED'):
        validate_bars([first], 180001, 239999)
    with pytest.raises(ValueError, match='INVALID_BAR_ROW'):
        validate_bars([first[:6]], 180000, 239999)


def test_bad_state_cannot_retroactively_arm_or_process_partial_entry_minute():
    p = plan()
    with pytest.raises(ValueError, match='INVALID_NEXT_FULL_MINUTE'):
        advance_protected(p, {'next_ms': 120000, 'armed_ms': None}, [])
    for armed in (179999, 240000, 239999, True):
        with pytest.raises(ValueError, match='INVALID_ARM_TIME'):
            advance_protected(p, {'next_ms': 180000, 'armed_ms': armed}, [])
    p['stop'] = p['entry']
    with pytest.raises(ValueError, match='INVALID_INITIAL_STOP_OR_TARGET'):
        advance_protected(p, state(p), [])


def test_validation_failure_does_not_mutate_plan_or_state_or_rows():
    p = plan()
    current = state(p)
    rows = [bar(180000, 104, 112, 99, 110), bar(300000, 110, 120, 109, 115)]
    originals = deepcopy((p, current, rows))
    with pytest.raises(ValueError, match='MISSING_MINUTE'):
        advance_protected(p, current, rows)
    assert (p, current, rows) == originals
