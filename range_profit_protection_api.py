"""Bounded passive presentation of the separate RANGE exit comparison."""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

STUDY = 'range-profit-protection-shadow-v1'
RULE = {
    'arm_at_r': 1,
    'arm_on': 'CLOSED_1M_CLOSE',
    'protected_stop': 'ENTRY',
    'effective': 'NEXT_FULL_MINUTE',
    'fees_bps_per_side': 4,
    'slippage_bps_per_side': 5,
    'adverse_funding_bps_per_8h': 1,
    'risk_usdt': 2,
}


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('DUPLICATE_JSON_KEY')
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError('NONFINITE_JSON')


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('NONFINITE_JSON')
    return number


def api_range_profit_protection_shadow(
    path: Path | None = None, *, now_ms: int | None = None,
) -> dict[str, Any]:
    """Read saved status only; no candle fetch, experiment setup or writes."""
    absent = {'study': STUDY, 'health': 'UNAVAILABLE', 'mode': 'SHADOW',
              'order_capability': False, 'combined_with_main_balance': False}
    try:
        if path is None:
            path = Path(os.environ.get('RANGE_PROFIT_PROTECTION_DIR', '/root/bot_range_profit_protection')) / 'data/status.json'
        with path.open('rb') as saved:
            raw = saved.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError('OVERSIZED_STATUS')
        data = json.loads(raw, object_pairs_hook=_unique, parse_float=_float,
                          parse_constant=_invalid_constant)
        if (not isinstance(data, dict) or data.get('study') != STUDY
                or data.get('mode') != 'SHADOW' or data.get('order_capability') is not False
                or data.get('combined_with_main_balance') is not False
                or data.get('health') not in ('OK', 'DEGRADED')):
            raise ValueError('UNEXPECTED_PROTECTION_STUDY')
        now = int(time.time() * 1000) if now_ms is None else now_ms
        registered, generated = data.get('registered_ms'), data.get('generated_at_ms')
        if (not _count(registered) or not _count(generated)
                or not 0 < registered <= generated <= now + 60_000):
            raise ValueError('INVALID_CLOCK')
        counts, results, source, protocol, review = (
            data.get(key) for key in ('counts', 'results', 'source', 'protocol', 'review'))
        if not all(isinstance(item, dict) for item in (counts, results, source, protocol, review)):
            raise ValueError('INVALID_RESULTS')
        for key, value in RULE.items():
            if (protocol.get(key) != value or isinstance(protocol.get(key), bool)):
                raise ValueError('UNEXPECTED_PROTECTION_RULE')
        for key in ('observed', 'eligible', 'skipped', 'unscorable', 'open_pairs', 'closed_pairs'):
            if not _count(counts.get(key)):
                raise ValueError('INVALID_COUNTS')
        if (counts['observed'] != counts['eligible'] + counts['skipped'] + counts['unscorable']
                or counts['eligible'] != counts['open_pairs'] + counts['closed_pairs']):
            raise ValueError('COHORT_IDENTITY_FAILED')
        pnl_keys = ('paired_control_net_usdt', 'protected_net_usdt', 'paired_delta_usdt',
                    'unscorable_control_net_usdt')
        if any(not _finite(results.get(key)) for key in pnl_keys):
            raise ValueError('INVALID_PNL')
        for key in ('improved_pairs', 'worsened_pairs', 'unchanged_pairs', 'unscorable_controls_closed'):
            if not _count(results.get(key)):
                raise ValueError('INVALID_RESULT_COUNTS')
        if (sum(results[key] for key in ('improved_pairs', 'worsened_pairs', 'unchanged_pairs')) != counts['closed_pairs']
                or results['unscorable_controls_closed'] > counts['unscorable']):
            raise ValueError('CLOSED_PAIR_IDENTITY_FAILED')
        if abs(results['protected_net_usdt'] - results['paired_control_net_usdt'] - results['paired_delta_usdt']) > 1e-6:
            raise ValueError('PAIRED_PNL_IDENTITY_FAILED')
        if counts['closed_pairs'] == 0 and any(abs(results[key]) > 1e-9 for key in pnl_keys[:3]):
            raise ValueError('PNL_WITHOUT_CLOSED_PAIRS')
        if results['unscorable_controls_closed'] == 0 and abs(results['unscorable_control_net_usdt']) > 1e-9:
            raise ValueError('UNKNOWN_PNL_WITHOUT_CLOSED_CONTROL')
        age = source.get('age_seconds')
        if (source.get('health') not in ('OK', 'DEGRADED', 'STALE', 'UNAVAILABLE')
                or (age is not None and (not _finite(age) or age < 0))
                or (source.get('health') == 'OK' and age is None)
                or (data['health'] == 'OK' and source['health'] != 'OK')):
            raise ValueError('INVALID_SOURCE_HEALTH')
        if (review.get('required_days') != 14 or isinstance(review.get('required_days'), bool)
                or review.get('required_closed_pairs') != 50 or isinstance(review.get('required_closed_pairs'), bool)
                or not _finite(review.get('elapsed_days')) or review['elapsed_days'] < 0
                or not isinstance(review.get('ready'), bool)
                or review.get('automatic_promotion') is not False
                or (review['ready'] and (review['elapsed_days'] < 14 or counts['closed_pairs'] < 50))):
            raise ValueError('INVALID_REVIEW')
        if (not isinstance(data.get('errors'), list)
                or any(not isinstance(item, (str, dict)) for item in data['errors'])
                or not isinstance(data.get('limitations'), list)
                or any(not isinstance(item, str) for item in data['limitations'])):
            raise ValueError('INVALID_DIAGNOSTICS')
        recent = data.get('recent_pairs')
        if not isinstance(recent, list) or len(recent) > 12:
            raise ValueError('INVALID_RECENT_PAIRS')
        seen = set()
        for pair in recent:
            if (not isinstance(pair, dict) or not _count(pair.get('id')) or pair['id'] in seen
                    or not isinstance(pair.get('symbol'), str) or not 1 <= len(pair['symbol']) <= 30
                    or pair.get('direction') not in ('LONG', 'SHORT')
                    or not _count(pair.get('opened_ms')) or not registered <= pair['opened_ms'] <= generated
                    or pair.get('admission') not in ('ELIGIBLE', 'UNKNOWN')
                    or pair.get('state') not in ('OPEN', 'CLOSED', 'UNKNOWN')):
                raise ValueError('INVALID_RECENT_PAIR')
            seen.add(pair['id'])
            if (pair.get('armed_ms') is not None and
                    (not _count(pair['armed_ms']) or not pair['opened_ms'] <= pair['armed_ms'] <= generated)):
                raise ValueError('INVALID_ARM_TIME')
            for key in ('control_net_usdt', 'protected_net_usdt', 'delta_usdt'):
                if pair.get(key) is not None and not _finite(pair[key]):
                    raise ValueError('INVALID_RECENT_PNL')
            for key in ('reason', 'control_reason', 'protected_reason', 'error'):
                if pair.get(key) is not None and not isinstance(pair[key], str):
                    raise ValueError('INVALID_RECENT_REASON')
            if pair['state'] == 'CLOSED':
                if (pair['admission'] != 'ELIGIBLE'
                        or any(not _finite(pair.get(key)) for key in ('control_net_usdt', 'protected_net_usdt', 'delta_usdt'))
                        or abs(pair['protected_net_usdt'] - pair['control_net_usdt'] - pair['delta_usdt']) > 1e-6):
                    raise ValueError('INVALID_CLOSED_RECENT_PAIR')
            elif pair.get('delta_usdt') is not None:
                raise ValueError('DELTA_BEFORE_BOTH_CLOSE')
            if ((pair['state'] == 'UNKNOWN') != (pair['admission'] == 'UNKNOWN')
                    or (pair['admission'] == 'UNKNOWN' and pair.get('protected_net_usdt') is not None)):
                raise ValueError('UNKNOWN_AS_PROTECTED_PNL')
        data['age_seconds'] = max(0, (now - generated) / 1000)
        data['stale_after_seconds'] = 900
        if data['age_seconds'] > 900:
            data['health'] = 'STALE'
        return data
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        return {**absent, 'source_error': type(exc).__name__}
