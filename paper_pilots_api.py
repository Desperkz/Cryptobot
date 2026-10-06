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
