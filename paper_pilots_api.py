"""Bounded read-only presentation of isolated paper experiments.

No candle reads, network calls, schema setup, order functions or DB writes.
Each (policy, arm) is separate; correlated virtual copies are never summed.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

PILOTS = (
    ('mainnet', 'Базовый mainnet', 'bot_mainnet_paper'),
    ('early', 'Ранний вход', 'bot_mainnet_early'),
    ('btc_rs', 'BTC / относительная сила', 'bot_btc_rs_paper'),
    ('direction', 'Направление SQZ', 'bot_sqz_direction_paper'),
    ('confirmation', 'Подтверждение SQZ', 'bot_sqz_confirmation_paper'),
    ('promotion', 'Перенос кандидатов в paper', 'bot_sqz_promotion_paper'),
    ('c2_high', 'SQZ · v2 HIGH · paper-проверка', 'bot_sqz_c2_high_paper'),
)
RECENT_LIMIT = 50
OPEN_LIMIT = 128
CACHE_SECONDS = 15
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {'expires': 0.0, 'data': None}


def _strict_json(raw):
    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('NONFINITE_RECORD')
        return number

    def invalid_constant(value: str) -> None:
        raise ValueError('NONFINITE_RECORD')

    return json.loads(raw, parse_float=finite_float, parse_constant=invalid_constant)


def _object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    if len(raw) > 65536:
        raise ValueError('OVERSIZED_RECORD')
    value = _strict_json(raw)
    if not isinstance(value, dict):
        raise ValueError('INVALID_JSON_OBJECT')
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _pnl(result: dict[str, Any], plan: dict[str, Any]) -> float | None:
    value = _number(result.get('model_pnl_usdt'))
    if value is not None:
        return value
    r, risk = _number(result.get('R')), _number(plan.get('risk_usdt'))
    return _number(r * risk) if r is not None and risk is not None else None


def _row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    try:
        value['plan'] = _object(value['plan'])
        value['result'] = _object(value['result']) if value['result'] else None
    except (ValueError, TypeError):
        value['plan'] = value['result'] = None
        value['data_error'] = 'INVALID_POSITION_JSON'
    return value


def read_pilot(key: str, label: str, directory: Path, *, now_ms: int) -> dict[str, Any]:
    base: dict[str, Any] = {'id': key, 'label': label, 'execution': 'LOCAL_PAPER_ONLY',
                            'order_capability': False, 'health': 'UNAVAILABLE',
                            'groups': [], 'recent_positions': [], 'open_positions': []}
    path = directory / 'mainnet_paper_lab.sqlite3'
    if not path.is_file():
        return {**base, 'error': 'DATABASE_MISSING'}
    try:
        # Never use immutable=1: active WALs must remain visible to a reader.
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.25)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            deadline = time.monotonic() + .6
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            conn.execute('BEGIN')
            meta = {r['key']: _object(r['value']) for r in conn.execute(
                "SELECT key,value FROM meta WHERE key IN ('manifest','scan','monitor','cost_monitor')")}
            manifest = meta.get('manifest', {})
            if manifest.get('mode') != 'LOCAL_PAPER_ONLY' or manifest.get('data_venue') != 'https://fapi.binance.com':
                return {**base, 'error': 'UNEXPECTED_EXECUTION_OR_VENUE'}
            arms, policies = manifest.get('arms', []), manifest.get('policies', [])
            if not isinstance(arms, list) or not isinstance(policies, list) or not arms or not policies:
                raise ValueError('INVALID_MANIFEST')
            grouped = {}
            for policy in policies:
                for arm in arms:
                    grouped[(policy, arm)] = {'policy': policy, 'arm': arm, 'closed': 0,
                        'open': 0, 'pending': 0, 'other': 0, 'invalid_results': 0,
                        'net_usdt': 0.0, 'wins': 0, 'losses': 0, '_gain': 0.0, '_loss': 0.0}
            # Streaming positions only; cached candles/observations can be large.
            for row in conn.execute('SELECT policy,arm,status,plan,result FROM positions'):
                group = grouped.setdefault((row['policy'], row['arm']), {'policy': row['policy'],
                    'arm': row['arm'], 'closed': 0, 'open': 0, 'pending': 0, 'other': 0,
                    'invalid_results': 0, 'net_usdt': 0.0, 'wins': 0, 'losses': 0, '_gain': 0.0, '_loss': 0.0})
                status = row['status']
                group[{'CLOSED': 'closed', 'OPEN': 'open', 'PENDING': 'pending'}.get(status, 'other')] += 1
                if status != 'CLOSED':
                    continue
                try:
                    result, plan = _object(row['result']), _object(row['plan'])
                    pnl = _pnl(result, plan)
                    if result.get('status') != 'CLOSED' or pnl is None:
                        raise ValueError('INVALID_CLOSED_RESULT')
                except (ValueError, TypeError, json.JSONDecodeError):
                    group['invalid_results'] += 1
                    continue
                group['net_usdt'] += pnl
                group['wins'] += int(pnl > 0)
                group['losses'] += int(pnl < 0)
                group['_gain'] += max(pnl, 0)
                group['_loss'] += max(-pnl, 0)
            for group in grouped.values():
                if not all(math.isfinite(group[k]) for k in ('net_usdt', '_gain', '_loss')):
                    group['invalid_results'] += 1
                if group['invalid_results']:
                    group['net_usdt'] = None
                    group['winrate'] = group['profit_factor'] = None
                else:
                    group['winrate'] = group['wins'] / group['closed'] * 100 if group['closed'] else None
                    group['profit_factor'] = _number(group['_gain'] / group['_loss']) if group['_loss'] else None
                del group['_gain'], group['_loss']
            recent = [_row(r) for r in conn.execute('SELECT * FROM positions ORDER BY id DESC LIMIT ?', (RECENT_LIMIT,))]
            active = [_row(r) for r in conn.execute("SELECT * FROM positions WHERE status IN ('OPEN','PENDING') ORDER BY id DESC LIMIT ?", (OPEN_LIMIT,))]
            counts = {table: conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in ('sources', 'observations', 'positions')}
            conn.rollback()
        scan, monitor = meta.get('scan', {}), meta.get('monitor', {})
        monitor_at = _number(monitor.get('at_ms'))
        scan_at = _number(scan.get('finished_ms'))
        cadence = _number(manifest.get('monitor_interval_sec')) or 30
        scan_cadence = _number(manifest.get('scan_interval_sec')) or 300
        monitor_age = max(0, (now_ms - monitor_at) / 1000) if monitor_at else None
        scan_age = max(0, (now_ms - scan_at) / 1000) if scan_at else None
        stale_after = max(180, cadence * 3)
        scan_stale_after = max(600, scan_cadence * 3)
        health = ('STALE' if monitor_age is None or scan_age is None or monitor_age > stale_after or scan_age > scan_stale_after
                  else 'DEGRADED' if scan.get('status') != 'OK' or monitor.get('status') != 'OK'
                  or meta.get('cost_monitor', {}).get('status', 'OK') != 'OK'
                  or any(g['invalid_results'] for g in grouped.values()) else 'OK')
        return {**base, 'health': health, 'venue': manifest['data_venue'], 'cohort': manifest.get('cohort'),
            'manifest': manifest, 'meta': meta, 'counts': counts, 'groups': list(grouped.values()),
            'recent_positions': recent, 'recent_limit': RECENT_LIMIT, 'open_positions': active,
            'open_limit': OPEN_LIMIT, 'open_total': sum(g['open'] + g['pending'] for g in grouped.values()),
            'monitor_at_ms': monitor_at, 'monitor_age_seconds': monitor_age,
            'scan_age_seconds': scan_age, 'stale_after_seconds': stale_after,
            'scan_stale_after_seconds': scan_stale_after}
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
        # An unreadable experiment does not erase any other pilot's results.
        return {**base, 'error': type(exc).__name__}


def build_paper_pilots(root: Path = Path('/root'), *, now_ms: int | None = None) -> dict[str, Any]:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return {'generated_at_ms': now_ms, 'scope': 'ISOLATED_MAINNET_PAPER',
        'combined_pnl': None, 'combined_pnl_reason': 'CORRELATED_ARMS_AND_POLICIES_NOT_ADDITIVE',
        'pilots': [read_pilot(key, label, root / name / 'data', now_ms=now_ms) for key, label, name in PILOTS]}


def api_paper_pilots() -> dict[str, Any]:
    with _CACHE_LOCK:
        if _CACHE['data'] is not None and time.monotonic() < _CACHE['expires']:
            return _CACHE['data']
        data = build_paper_pilots()
        _CACHE.update(data=data, expires=time.monotonic() + CACHE_SECONDS)
        return data


def api_runner_comparison(path: Path = Path('/root/bot_runner_comparison/data/status.json'), *, now_ms: int | None = None) -> dict[str, Any]:
    """A saved paired study, separate from the public-mainnet pilot databases."""
    unavailable = {'health': 'UNAVAILABLE', 'mode': 'LOCAL_PAPER_ONLY', 'order_capability': False}
    try:
        with path.open('rb') as file:
            raw = file.read(512*1024+1)
        if len(raw)>512*1024:
            raise ValueError('OVERSIZED_STATUS')
        data = _strict_json(raw)
        if not isinstance(data,dict) or not isinstance(data.get('protocol'),dict) or data.get('mode')!='LOCAL_PAPER_ONLY' or data.get('order_capability') is not False or data['protocol'].get('name')!='main-paper-runner-paired-v1-20261006':
            raise ValueError('UNEXPECTED_STUDY')
        now_ms=now_ms if now_ms is not None else int(time.time()*1000)
        generated=_number(data.get('generated_at_ms'))
        if generated is None:
            raise ValueError('INVALID_TIMESTAMP')
        data['age_seconds']=max(0,(now_ms-generated)/1000)
        if data['age_seconds']>900:
            data['health']='STALE'
        return data
    except (OSError,ValueError,TypeError) as exc:
        return {**unavailable,'source_error':type(exc).__name__}


def api_diversified_shadow(path: Path = Path('/root/bot_diversified_v3/data/status.json'), *, now_ms: int | None = None) -> dict[str, Any]:
    """Bounded saved-status read only; no source DB access or research execution."""
    unavailable={'health':'UNAVAILABLE','mode':'SHADOW_ONLY','order_capability':False,'auto_promotion':False}
    try:
        with path.open('rb') as file:raw=file.read(512*1024+1)
        if len(raw)>512*1024:raise ValueError('OVERSIZED_STATUS')
        data=_strict_json(raw)
        if not isinstance(data,dict) or data.get('mode')!='SHADOW_ONLY' or data.get('order_capability') is not False or data.get('auto_promotion') is not False:
            raise ValueError('UNEXPECTED_STUDY')
        if not isinstance(data.get('groups'),list) or len(data['groups'])>11 or not isinstance(data.get('protocol'),dict):raise ValueError('INVALID_GROUPS')
        stamp=_number(data.get('generated_at_ms'));now_ms=now_ms if now_ms is not None else int(time.time()*1000)
        if stamp is None or stamp>now_ms+60000:raise ValueError('INVALID_TIMESTAMP')
        data['age_seconds']=max(0,(now_ms-stamp)/1000)
        if data['age_seconds']>1200:data['health']='STALE'
        return data
    except (OSError,ValueError,TypeError):return unavailable


def api_breakout_recheck_shadow(
    path: Path = Path('/root/bot_breakout_recheck_shadow/data/status.json'),
    *, now_ms: int | None = None,
) -> dict[str, Any]:
    """Read one bounded saved result; never open the source DB or run a study."""
    unavailable = {'health': 'UNAVAILABLE', 'study': 'sqz-entry-recheck-shadow-v1',
                   'mode': 'SHADOW', 'order_capability': False}
    try:
        with path.open('rb') as saved:
            raw = saved.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError('OVERSIZED_STATUS')
        data = _strict_json(raw)
        if (not isinstance(data, dict) or data.get('study') != unavailable['study']
                or data.get('mode') != 'SHADOW' or data.get('order_capability') is not False
                or data.get('cohort') != '2026-10-23-sqz-entry-recheck-shadow-v1'
                or data.get('health') not in ('OK', 'DEGRADED')):
            raise ValueError('UNEXPECTED_STUDY')

        def finite_number(value):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and _number(value) is not None

        current = int(time.time() * 1000) if now_ms is None else now_ms
        registered, generated = data.get('registered_ms'), data.get('generated_at_ms')
        if (not finite_number(registered) or not finite_number(generated)
                or not 0 < registered <= generated <= current + 60_000):
            raise ValueError('INVALID_TIMESTAMP')
        counts, pnl, review = data.get('counts'), data.get('pnl'), data.get('review')
        if not all(isinstance(item, dict) for item in (counts, pnl, review, data.get('transitions'))):
            raise ValueError('INVALID_RESULTS')
        for key in ('sources', 'waiting_entry', 'decided', 'kept', 'rejected',
                    'unscorable', 'open_controls', 'closed_pairs'):
            value = counts.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError('INVALID_COUNTS')
        for key in ('control_net_usdt', 'recheck_net_usdt', 'delta_usdt',
                    'avoided_losses_usdt', 'lost_profits_usdt'):
            if not finite_number(pnl.get(key)):
                raise ValueError('INVALID_PNL')
        if (not finite_number(review.get('elapsed_days')) or review['elapsed_days'] < 0
                or review.get('required_days') != 14 or review.get('required_closed_pairs') != 50
                or not isinstance(review.get('ready'), bool)
                or (review['ready'] and (review['elapsed_days'] < 14 or counts['closed_pairs'] < 50))
                or not isinstance(data.get('errors'), list)
                or any(not isinstance(error, (str, dict)) for error in data['errors'])):
            raise ValueError('INVALID_REVIEW_OR_ERRORS')
        data['age_seconds'] = max(0, (current - generated) / 1000)
        data['stale_after_seconds'] = 900
        if data['age_seconds'] > data['stale_after_seconds']:
            data['health'] = 'STALE'
        return data
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        return {**unavailable, 'source_error': type(exc).__name__}


def api_sqz_entry_quality_shadow(
    path: Path = Path('/root/bot_sqz_entry_quality_shadow/data/status.json'),
    *, now_ms: int | None = None,
) -> dict[str, Any]:
    """Bounded saved-status reader for the separate future-only quality study."""
    unavailable = {'health': 'UNAVAILABLE', 'study': 'sqz-entry-quality-shadow-v1',
                   'mode': 'SHADOW', 'order_capability': False}
    try:
        with path.open('rb') as saved:
            raw = saved.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError('OVERSIZED_STATUS')
        data = _strict_json(raw)
        if (not isinstance(data, dict) or data.get('study') != unavailable['study']
                or data.get('mode') != 'SHADOW' or data.get('order_capability') is not False
                or data.get('cohort') != '2026-11-09-sqz-entry-quality-v1'
                or data.get('health') not in ('OK', 'DEGRADED')):
            raise ValueError('UNEXPECTED_STUDY')
        finite = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and _number(x) is not None
        count = lambda x: isinstance(x, int) and not isinstance(x, bool) and x >= 0
        current = int(time.time() * 1000) if now_ms is None else now_ms
        registered, generated = data.get('registered_ms'), data.get('generated_at_ms')
        if not finite(registered) or not finite(generated) or not 0 < registered <= generated <= current + 60_000:
            raise ValueError('INVALID_TIMESTAMP')
        counts, pnl, review = (data.get(k) for k in ('counts', 'pnl', 'review'))
        if not all(isinstance(x, dict) for x in (counts, pnl, review)):
            raise ValueError('INVALID_RESULTS')
        for key in ('sources', 'waiting_entry', 'decided', 'kept', 'rejected',
                    'unscorable', 'open_controls', 'closed_pairs'):
            if not count(counts.get(key)):
                raise ValueError('INVALID_COUNTS')
        pnl_keys = ('control_net_usdt', 'recheck_net_usdt', 'delta_usdt',
                    'avoided_losses_usdt', 'lost_profits_usdt')
        if any(not finite(pnl.get(k)) for k in pnl_keys):
            raise ValueError('INVALID_PNL')
        if (review.get('required_days') != 14 or review.get('required_closed_pairs') != 50
                or not finite(review.get('elapsed_days')) or review['elapsed_days'] < 0
                or not count(review.get('accepted_closed'))
                or review['accepted_closed'] > counts['closed_pairs']
                or not isinstance(review.get('ready'), bool)
                or (review['ready'] and (review['elapsed_days'] < 14 or counts['closed_pairs'] < 50))):
            raise ValueError('INVALID_REVIEW')
        if data.get('quality_rule') != {'min_volume_ratio': 2.5, 'min_breakout_atr': 0.3,
                                        'comparison': 'AND', 'inclusive': True}:
            raise ValueError('UNEXPECTED_RULE')
        buckets = data.get('quality_buckets')
        expected = {'BOTH_PASS', 'VOLUME_ONLY_FAIL', 'BREAKOUT_ONLY_FAIL', 'BOTH_FAIL'}
        if not isinstance(buckets, list) or len(buckets) != 4:
            raise ValueError('INVALID_BUCKETS')
        seen = set()
        for bucket in buckets:
            if not isinstance(bucket, dict) or bucket.get('bucket') not in expected or bucket['bucket'] in seen:
                raise ValueError('INVALID_BUCKETS')
            seen.add(bucket['bucket'])
            if (not count(bucket.get('n_sources')) or not count(bucket.get('n_closed'))
                    or bucket['n_closed'] > bucket['n_sources']
                    or any(not finite(bucket.get(k)) for k in pnl_keys if k != 'delta_usdt')):
                raise ValueError('INVALID_BUCKET_RESULTS')
        if not isinstance(data.get('errors'), list):
            raise ValueError('INVALID_ERRORS')
        data['age_seconds'] = max(0, (current - generated) / 1000)
        data['stale_after_seconds'] = 900
        if data['age_seconds'] > 900:
            data['health'] = 'STALE'
        return data
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        return {**unavailable, 'source_error': type(exc).__name__}


def api_funding_carry_shadow(
    path: Path = Path('/root/bot_funding_carry_shadow/data/status.json'),
    *, now_ms: int | None = None,
) -> dict[str, Any]:
    """Read the separate paired-leg balance; never add it to main paper PnL."""
    absent = {'study': 'funding-carry-shadow-v1', 'health': 'UNAVAILABLE',
              'mode': 'SHADOW', 'order_capability': False, 'combined_with_main_balance': False}
    try:
        with path.open('rb') as saved:
            raw = saved.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError('OVERSIZED_STATUS')
        data = _strict_json(raw)
        if (not isinstance(data, dict) or data.get('study') != absent['study']
                or data.get('cohort') != '2026-11-09-btc-eth-funding-carry-v1'
                or data.get('mode') != 'SHADOW' or data.get('order_capability') is not False
                or data.get('combined_with_main_balance') is not False
                or data.get('health') not in ('OK', 'DEGRADED')
                or data.get('capital_usdt') != 1000 or data.get('target_spot_notional_usdt') != 100
                or data.get('symbols') != ['BTCUSDT', 'ETHUSDT']
                or data.get('spot_fee_bps') != 10 or data.get('perp_fee_bps') != 4
                or data.get('adverse_slippage_bps_each_leg') != 5):
            raise ValueError('UNEXPECTED_CARRY_PROTOCOL')
        valid = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and _number(x) is not None
        count = lambda x: isinstance(x, int) and not isinstance(x, bool) and x >= 0
        now = int(time.time() * 1000) if now_ms is None else now_ms
        registered, generated, next_at = (data.get(k) for k in ('registered_ms', 'generated_at_ms', 'next_execution_ms'))
        if (not all(valid(x) for x in (registered, generated, next_at))
                or not 0 < registered <= generated <= now + 60_000
                or not registered <= next_at <= generated + 2 * 86400000):
            raise ValueError('INVALID_CARRY_CLOCK')
        counts = data.get('counts')
        if (not isinstance(counts, dict) or any(not count(counts.get(k)) for k in
                ('orders', 'funding_payments', 'missed_days', 'minimum_order_skips'))
                or not count(data.get('open_hedge_pairs')) or data['open_hedge_pairs'] > 2
                or not isinstance(data.get('errors'), list) or not isinstance(data.get('blocked'), bool)):
            raise ValueError('INVALID_CARRY_COUNTS')
        holdings = data.get('holdings')
        if not isinstance(holdings, list) or len(holdings) != data['open_hedge_pairs']:
            raise ValueError('INVALID_HEDGES')
        seen = set()
        for row in holdings:
            if not isinstance(row, dict):
                raise ValueError('INVALID_HEDGE_ROW')
            symbol = row.get('symbol')
            long, short, opened = (row.get(k) for k in ('spot_long_quantity', 'perp_short_quantity', 'opened_ms'))
            if (symbol not in data['symbols'] or symbol in seen
                    or not all(valid(x) for x in (long, short, opened))
                    or long <= 0 or long != short or not registered <= opened <= generated):
                raise ValueError('UNMATCHED_HEDGE')
            seen.add(symbol)
        pnl = data.get('pnl')
        if pnl is None:
            if data['health'] == 'OK':
                raise ValueError('HEALTHY_WITHOUT_VALUE')
        else:
            keys = ('balance_usdt', 'net_usdt', 'capital_return_pct', 'actual_funding_cash_usdt',
                    'model_fees_usdt', 'model_slippage_usdt', 'observed_spread_usdt', 'basis_pnl_usdt', 'stress_net_usdt')
            if not isinstance(pnl, dict) or any(not valid(pnl.get(k)) for k in keys):
                raise ValueError('INVALID_CARRY_VALUE')
            if any(pnl[k] < 0 for k in ('model_fees_usdt', 'model_slippage_usdt', 'observed_spread_usdt')):
                raise ValueError('NEGATIVE_MODEL_COST')
            total = pnl['basis_pnl_usdt'] + pnl['actual_funding_cash_usdt'] - sum(pnl[k] for k in
                    ('model_fees_usdt', 'model_slippage_usdt', 'observed_spread_usdt'))
            if (abs(total - pnl['net_usdt']) > 1e-6 or abs(pnl['balance_usdt'] - 1000 - pnl['net_usdt']) > 1e-6
                    or abs(pnl['capital_return_pct'] - pnl['net_usdt']/10) > 1e-6
                    or pnl['stress_net_usdt'] > pnl['net_usdt'] + 1e-6):
                raise ValueError('CARRY_CASH_IDENTITY_FAILED')
        if counts['orders'] == 0 and (counts['funding_payments'] or holdings):
            raise ValueError('INCOME_BEFORE_HEDGE')
        data['age_seconds'] = max(0, (now-generated)/1000)
        data['stale_after_seconds'] = 180
        if data['age_seconds'] > 180:
            data['health'] = 'STALE'
        return data
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        return {**absent, 'source_error': type(exc).__name__}


def api_research_storage(path: Path = Path('/root/bot_research_storage/data/status.json'),
                         *, now_ms: int | None = None) -> dict[str, Any]:
    """Saved maintenance health only; never start maintenance in a request."""
    absent = {'study': 'research-storage-maintenance-v1', 'health': 'UNAVAILABLE',
              'order_capability': False}
    try:
        with path.open('rb') as saved:
            raw = saved.read(256*1024+1)
        if len(raw) > 256*1024:
            raise ValueError('OVERSIZED_STATUS')
        data = _strict_json(raw)
        if (not isinstance(data, dict) or data.get('study') != absent['study']
                or data.get('order_capability') is not False
                or data.get('health') not in ('OK', 'IDLE', 'DEGRADED')
                or not isinstance(data.get('errors'), list)):
            raise ValueError('INVALID_MAINTENANCE_STATUS')
        current = int(time.time()*1000) if now_ms is None else now_ms
        for key in ('generated_at_ms', 'free_disk_bytes'):
            if isinstance(data.get(key), bool) or _number(data.get(key)) is None or data[key] < 0:
                raise ValueError('INVALID_TIMESTAMP_OR_DISK')
        if data['generated_at_ms'] > current+60_000:
            raise ValueError('FUTURE_TIMESTAMP')
        if data.get('last_success_ms') is not None and (
                isinstance(data['last_success_ms'], bool) or _number(data['last_success_ms']) is None
                or not 0 < data['last_success_ms'] <= data['generated_at_ms']):
            raise ValueError('INVALID_LAST_SUCCESS')
        data['age_seconds'] = max(0, (current-data['generated_at_ms'])/1000)
        if data['age_seconds'] > 48*3600:
            data['health'] = 'STALE'
        return data
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return absent
