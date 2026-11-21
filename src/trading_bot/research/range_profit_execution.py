"""Fixed RANGE price-entry protection; minute OHLC, no orders or networking.

Control settlement and sequencing preserve diversified_shadow's frozen model.
The one experimental change arms on a completed minute CLOSE at +1 initial
price-stop R and uses the original effective entry as stop from the NEXT minute.
Costs remain payable, so an entry-price stop is not a net breakeven guarantee.
"""
from __future__ import annotations

import json
import math

MINUTE = 60000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str)


def settle(position, price, when, reason):
    """Source-identical effective exit, fees, adverse funding and cost fields."""
    sign = 1 if position['direction'] == 'LONG' else -1
    effective = price * (1 - sign * .0005)
    qty = position['quantity']
    entry = position['entry']
    fees = (entry + effective) * qty * .0004
    funding = entry * qty * .0001 * max(0, when - position['opened_ms']) / 28800000
    gross = sign * (effective - entry) * qty
    costs = {'fees': fees, 'funding': funding, 'funding_source': 'adverse_1bp_per8h_buffer',
             'entry_slippage': abs(entry - position['entry_mid']) * qty,
             'exit_slippage': abs(price - effective) * qty,
             'gross_before_exit_slippage': sign * (price - entry) * qty}
    return {'closed_ms': when, 'exit_price': effective, 'net_usdt': gross - fees - funding,
            'reason': reason, 'costs': canonical(costs)}


def advance_control(position, bars, *, interval_ms=MINUTE):
    """Source-identical control, including first-stop ambiguity and adverse gaps.

    Callers validate public-market rows with validate_bars before execution.
    The copied control retains the source's supported 1m/15m API for parity.
    """
    if interval_ms not in (MINUTE, 900000):
        raise ValueError('Unsupported closed-bar interval')
    cursor = position['next_ms']
    sign = 1 if position['direction'] == 'LONG' else -1
    for b in bars:
        if int(b[0]) != cursor:
            raise ValueError('MISSING_MINUTE: refusing to bridge a data gap')
        o, h, l, close = map(float, b[1:5])
        when = int(b[6])
        hit_stop = l <= position['stop'] if sign == 1 else h >= position['stop']
        hit_target = h >= position['target'] if sign == 1 else l <= position['target']
        if hit_stop:
            price = min(o, position['stop']) if sign == 1 else max(o, position['stop'])
            return settle(position, price, when, 'STOP_FIRST_AMBIGUOUS' if hit_target else 'STOP'), cursor + interval_ms
        if hit_target:
            return settle(position, position['target'], when, 'TARGET'), cursor + interval_ms
        if when >= position['expires_ms']:
            return settle(position, close, when, 'TIMEOUT'), cursor + interval_ms
        cursor += interval_ms
    return None, cursor


def _integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('INVALID_' + label)
    return value


def _price(value):
    if isinstance(value, bool):
        raise ValueError('INVALID_OHLC_PRICE')
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('INVALID_OHLC_PRICE') from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError('INVALID_OHLC_PRICE')
    return result


def validate_bars(bars, start_ms, end_ms):
    """Return row copies of contiguous, closed 1m OHLC bars, refusing any gaps.

    Binance rows need at least their first seven fields. Other fields are
    preserved. Empty results are allowed; they never manufacture a timeout.
    Timestamps must be integer JSON values, rather than silently truncated floats.
    """
    cursor = _integer(start_ms, 'START_TIME')
    end = _integer(end_ms, 'END_TIME')
    if cursor % MINUTE:
        raise ValueError('START_NOT_MINUTE_ALIGNED')
    if not isinstance(bars, (list, tuple)):
        raise ValueError('INVALID_BARS')
    validated = []
    for raw in bars:
        if not isinstance(raw, (list, tuple)) or len(raw) < 7:
            raise ValueError('INVALID_BAR_ROW')
        opened = _integer(raw[0], 'BAR_OPEN_TIME')
        closed = _integer(raw[6], 'BAR_CLOSE_TIME')
        if opened != cursor:
            raise ValueError('MISSING_MINUTE: refusing to bridge a data gap')
        if closed != opened + MINUTE - 1:
            raise ValueError('INVALID_BAR_CLOSE_TIME')
        if closed > end:
            raise ValueError('UNFINISHED_OR_FUTURE_MINUTE')
        o, h, l, close = map(_price, raw[1:5])
        if h < max(o, close) or l > min(o, close) or h < l:
            raise ValueError('INVALID_OHLC_RANGE')
        row = list(raw)
        row[0] = opened
        row[1:5] = [o, h, l, close]
        row[6] = closed
        validated.append(row)
        cursor += MINUTE
    return validated


def advance_protected(plan, state, bars):
    """Advance the fixed close-at-1R/next-minute entry-stop variant.

    Returns (outcome_or_none, copied_state), with state keys next_ms/armed_ms.
    The original plan (including its stop, size and target) stays immutable.
    Only full minute rows after the source observed entry can be processed.
    """
    if plan['direction'] not in ('LONG', 'SHORT'):
        raise ValueError('INVALID_DIRECTION')
    sign = 1 if plan['direction'] == 'LONG' else -1
    entry, initial_stop, target = (_price(plan[key]) for key in ('entry', 'stop', 'target'))
    initial_risk = sign * (entry - initial_stop)
    if min(initial_risk, sign * (target - entry)) <= 0:
        raise ValueError('INVALID_INITIAL_STOP_OR_TARGET')
    opened = _integer(plan['opened_ms'], 'ENTRY_TIME')
    _integer(plan['expires_ms'], 'EXPIRY_TIME')
    cursor = _integer(state['next_ms'], 'NEXT_TIME')
    first_full = (opened // MINUTE + 1) * MINUTE
    if cursor % MINUTE or cursor < first_full:
        raise ValueError('INVALID_NEXT_FULL_MINUTE')
    armed = state['armed_ms']
    if armed is not None:
        armed = _integer(armed, 'ARM_TIME')
        if armed % MINUTE != MINUTE - 1 or not first_full + MINUTE - 1 <= armed < cursor:
            raise ValueError('INVALID_ARM_TIME')
    if not isinstance(bars, (list, tuple)):
        raise ValueError('INVALID_BARS')
    end = bars[-1][6] if bars and isinstance(bars[-1], (list, tuple)) and len(bars[-1]) >= 7 else cursor
    validated = validate_bars(bars, cursor, end)
    threshold = entry + sign * initial_risk
    for b in validated:
        o, h, l, close = b[1:5]
        when = b[6]
        stop = entry if armed is not None else initial_stop
        hit_stop = l <= stop if sign == 1 else h >= stop
        hit_target = h >= target if sign == 1 else l <= target
        if hit_stop:
            price = min(o, stop) if sign == 1 else max(o, stop)
            reason = 'PROTECTED_STOP' if armed is not None else 'STOP'
            if hit_target:
                reason += '_FIRST_AMBIGUOUS'
            return settle(plan, price, when, reason), {'next_ms': cursor + MINUTE, 'armed_ms': armed}
        if hit_target:
            return settle(plan, target, when, 'TARGET'), {'next_ms': cursor + MINUTE, 'armed_ms': armed}
        if when >= plan['expires_ms']:
            return settle(plan, close, when, 'TIMEOUT'), {'next_ms': cursor + MINUTE, 'armed_ms': armed}
        if armed is None and sign * (close - threshold) >= 0:
            armed = when
        cursor += MINUTE
    return None, {'next_ms': cursor, 'armed_ms': armed}
