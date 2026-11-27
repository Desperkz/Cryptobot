"""Compare fixed VWAP admissions on a dated, read-only V3 evidence export."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from statistics import median

from trading_bot.research import diversified_v4_common as source
from trading_bot.research import diversified_v4_gates as gates
from trading_bot.research.range_profit_execution import MINUTE, advance_control, validate_bars
from trading_bot.research.vwap_exit_execution import diagnostics, initial_state
from vwap_exit_review import parity

PROTOCOL_SHA256 = '36cb2a63d1e99e24c36d56ab12cba0a0edba55aa0bc5b6ea9093e38208e9a4c4'
STRATEGY = 'VWAP_REVERSION_V3_RECLAIM'
COST_KEYS = ('fees', 'funding', 'entry_slippage', 'exit_slippage')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def projected_net_rr(plan, bps):
    """Compare target and stop under the same full-horizon funding allowance."""
    target = source.expected_settlement(plan, plan['target'], plan['expires_ms'], bps)['net_usdt']
    stop = source.expected_settlement(plan, plan['stop'], plan['expires_ms'], bps)['net_usdt']
    return target / -stop


def score(item, captured_ms):
    plan, snapshot = item['plan'], item['snapshot']
    features = item['features']
    source.validate_plan(plan, snapshot, captured_ms)
    if plan['strategy'] != STRATEGY or any(
        features[key] != value for key, value in (
            ('source_strategy', STRATEGY), ('symbol', plan['symbol']), ('direction', plan['direction']),
            ('observed_ms', plan['opened_ms']), ('signal_bar_ms', snapshot['bar_ms']),
        )
    ):
        raise ValueError('INPUT_IDENTITY_MISMATCH')
    decision = gates.evaluate(features)
    if decision != item['decision']:
        raise ValueError('HOURLY_GATE_MISMATCH')
    side = 1 if plan['direction'] == 'LONG' else -1
    alignment = side * features['hourly_trend']
    rr5 = projected_net_rr(plan, 5)
    rr10 = projected_net_rr(plan, 10)
    if rr5 < .8 - 1e-10:
        raise ValueError('SOURCE_ADMISSION_MISMATCH')
    # Admission decisions are fixed using entry inputs before scoring any outcome.
    row = {
        'id': plan['id'], 'opened_ms': plan['opened_ms'], 'signal_bar_ms': snapshot['bar_ms'],
        'symbol': plan['symbol'], 'direction': plan['direction'],
        'hourly_alignment': {-1: 'against', 0: 'neutral', 1: 'aligned'}[alignment],
        'hourly_guard': decision['accepted'], 'target_headroom': rr10 >= .8,
        'target_rr5': rr5, 'target_rr10': rr10,
    }
    settled = source.validate_control(plan, item['outcome'], captured_ms)
    if settled is None:
        return {**row, 'status': 'OPEN'}
    costs = sum(settled['costs'][key] for key in COST_KEYS)
    trigger = settled['exit_price'] / (1 - side * .0005)
    stress = source.expected_settlement(plan, trigger, settled['closed_ms'], 10)
    return {**row, 'status': 'CLOSED', 'net': settled['net_usdt'], 'stress': stress['net_usdt'],
            'exit_reason': settled['reason'], 'price_only': settled['net_usdt'] + costs,
            'costs': costs, 'holding_minutes': (settled['closed_ms'] - plan['opened_ms']) / 60000}


def stats(rows):
    closed = [row for row in rows if row['status'] == 'CLOSED']
    net = sum(row['net'] for row in closed)
    return {
        'closed': len(closed), 'open': len(rows) - len(closed),
        'net': net, 'stress': sum(row['stress'] for row in closed),
        'avg_r': net / len(closed) / 2 if closed else None,
        'price_only': sum(row['price_only'] for row in closed), 'costs': sum(row['costs'] for row in closed),
        'wins': sum(row['net'] > 0 for row in closed), 'losses': sum(row['net'] < 0 for row in closed),
        'negative_before_costs': sum(row['price_only'] < 0 for row in closed),
        'days_utc': len({datetime.fromtimestamp(row['opened_ms'] / 1000, timezone.utc).date() for row in closed}),
        'median_holding_minutes': median(row['holding_minutes'] for row in closed) if closed else None,
        'exits': dict(Counter(row['exit_reason'] for row in closed)),
    }


def compare(rows, gate, midpoint):
    retained = [row for row in rows if row[gate]]
    rejected = [row for row in rows if not row[gate]]
    selected, removed, baseline = stats(retained), stats(rejected), stats(rows)
    share = selected['closed'] / baseline['closed'] if baseline['closed'] else 0
    removed_winners = sum(max(row['net'], 0) for row in rejected if row['status'] == 'CLOSED')
    removed_losers = -sum(min(row['net'], 0) for row in rejected if row['status'] == 'CLOSED')
    halves = [stats([r for r in retained if (r['signal_bar_ms'] <= midpoint) == first])
              for first in (True, False)]
    without_best = selected['net'] - max((row['net'] for row in retained if row['status'] == 'CLOSED'), default=0)
    delta = selected['net'] - baseline['net']
    checks = {
        'primary_and_stress': selected['net'] > 0 and selected['stress'] > 0,
        'better_mean_r': selected['avg_r'] is not None and selected['avg_r'] > baseline['avg_r'],
        'paired_delta': delta > 0,
        'both_halves': all(half['closed'] > 0 and half['net'] >= 0 for half in halves),
        'without_best': selected['closed'] > 0 and without_best >= 0,
        'sample': selected['closed'] >= 12 and selected['days_utc'] >= 5 and share >= .15,
    }
    return {'accepted': selected, 'rejected': removed, 'retained_fraction': share,
            'delta': delta, 'avoided_losses': removed_losers, 'missed_profits': removed_winners,
            'halves': halves, 'without_best': without_best, 'checks': checks,
            'passes': all(checks.values())}


def audit(evidence_bytes, protocol_bytes, reference):
    if sha(protocol_bytes) != PROTOCOL_SHA256:
        raise ValueError('PROTOCOL_CHANGED')
    ev, protocol = json.loads(evidence_bytes), json.loads(protocol_bytes)
    if ev['manifest_sha256'] != source.SOURCE_HASH or ev['gate_sha256'] != sha(Path(gates.__file__).read_bytes()):
        raise ValueError('FROZEN_SOURCE_CHANGED')
    captured = ev['captured_ms']
    prior = {r['source_id']: r for r in reference['rows'] if r['source_strategy'] == STRATEGY}
    if len(prior) != 54 or reference['source_watermark'] != 300:
        raise ValueError('REFERENCE_COHORT_CHANGED')
    rows = []
    unknown = []
    seen = set()
    for item in ev['rows']:
        sid = item['plan']['id']
        if sid in seen or sid > ev['watermark']:
            raise ValueError('SOURCE_ID_MISMATCH')
        seen.add(sid)
        if item.get('input_error'):
            unknown.append({'id': sid, 'reason': item['input_error'], 'status': item['outcome']['status']})
            continue
        rows.append(score(item, captured))
    old = [r for r in rows if r['id'] in prior]
    if len(old) != len(prior) or any(
        not math.isclose(row['net'], prior[row['id']]['net_usdt'], abs_tol=1e-9)
        or row['hourly_guard'] != prior[row['id']]['accepted'] for row in old
    ):
        raise ValueError('REFERENCE_PARITY_FAILED')
    cutoff = protocol['later_window']['signal_after_ms']
    later = [r for r in rows if r['id'] > protocol['later_window']['id_above'] and r['signal_bar_ms'] > cutoff]
    bridge = [r for r in rows if r not in later and r['id'] not in prior]
    mid = (cutoff + captured) // 2
    earlier_mid = (min(r['signal_bar_ms'] for r in old) + max(r['signal_bar_ms'] for r in old)) // 2
    return {
        'study': protocol['study'], 'captured_ms': captured, 'watermark': ev['watermark'], 'cutoff_ms': cutoff,
        'evidence_sha256': sha(evidence_bytes), 'protocol_sha256': sha(protocol_bytes),
        'reference_parity': len(old), 'all': stats(rows), 'bridge': stats(bridge), 'unknown': unknown,
        'reference': {'control': stats(old), **{gate: compare(old, gate, earlier_mid)
                                               for gate in ('hourly_guard', 'target_headroom')}},
        'later': {'control': stats(later), **{gate: compare(later, gate, mid)
                                            for gate in ('hourly_guard', 'target_headroom')}},
        'later_by_alignment': {key: stats([r for r in later if r['hourly_alignment'] == key])
                               for key in ('against', 'neutral', 'aligned')},
        'later_by_direction': {key: stats([r for r in later if r['direction'] == key]) for key in ('LONG', 'SHORT')},
        'later_by_exit': {key: stats([r for r in later if r.get('exit_reason') == key])
                          for key in sorted({r['exit_reason'] for r in later if r['status'] == 'CLOSED'})},
        'later_rows': later,
    }


def add_paths(report, evidence, requests_bytes, minutes_bytes):
    """Verify the source path first, then describe moves before its close."""
    requested = json.loads(requests_bytes)
    minutes = json.loads(minutes_bytes)
    if minutes['requests_sha256'] != sha(requests_bytes):
        raise ValueError('MINUTE_REQUESTS_CHANGED')
    closed_ids = {r['id'] for r in report['later_rows'] if r['status'] == 'CLOSED'}
    requests = {r['id']: r for r in requested}
    windows = {w['id']: w for w in minutes['windows']}
    if (len(requested) != len(requests) or len(minutes['windows']) != len(windows)
            or set(windows) != closed_ids or set(requests) != closed_ids):
        raise ValueError('MINUTE_COHORT_CHANGED')
    items = {item['plan']['id']: item for item in evidence['rows']}
    paths = []
    seen_bars = {}
    for row in report['later_rows']:
        sid = row['id']
        if sid not in closed_ids:
            continue
        item, window = items[sid], windows[sid]
        plan = item['plan']
        control = source.validate_control(plan, item['outcome'], evidence['captured_ms'])
        start = initial_state(plan)['next_ms']
        end = control['closed_ms']
        expected = {'id': sid, 'symbol': plan['symbol'], 'start_ms': start, 'end_ms': end}
        if requests[sid] != expected or any(window[k] != v for k, v in expected.items()):
            raise ValueError('MINUTE_WINDOW_CHANGED')
        bars = validate_bars(window['bars'], start, end)
        if len(bars) != (end + 1 - start) // MINUTE:
            raise ValueError('INCOMPLETE_MINUTE_WINDOW')
        for bar in bars:
            key = (plan['symbol'], bar[0])
            if bar != seen_bars.setdefault(key, bar):
                raise ValueError('OVERLAPPING_MINUTES_CHANGED')
        replay, _ = advance_control({**plan, 'next_ms': start}, bars)
        parity(replay, control)
        diag = diagnostics(plan, bars, control)
        side = 1 if plan['direction'] == 'LONG' else -1
        risk = abs(plan['entry'] - plan['stop'])
        # A wick only bounds the excursion. The closing candle may move again after the exit.
        favorable_wicks = [side * (bar[2 if side == 1 else 3] - plan['entry']) / risk for bar in bars]
        paths.append({
            'id': sid, 'net': row['net'], 'hourly_alignment': row['hourly_alignment'],
            'exit_reason': row['exit_reason'], 'minutes': len(bars), **diag,
            'best_wick_r_before_exit_minute': max(favorable_wicks[:-1], default=None),
            'best_wick_r_including_exit_minute': max(favorable_wicks),
        })
    losing = [p for p in paths if p['net'] < 0]
    report['path_review'] = {
        'replayed': len(paths), 'minutes': sum(p['minutes'] for p in paths), 'unique_minutes': len(seen_bars),
        'minutes_sha256': sha(minutes_bytes), 'losing_controls': len(losing),
        'losers_closed_at_half_r': sum(p['closed_above_half_r_before_exit'] for p in losing),
        'losers_closed_at_one_r': sum(p['closed_above_1r_before_exit'] for p in losing),
        'losers_touched_half_r_before_exit_minute': sum(
            p['best_wick_r_before_exit_minute'] is not None and p['best_wick_r_before_exit_minute'] >= .5
            for p in losing
        ),
        'rows': paths,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('protocol', type=Path)
    parser.add_argument('reference', type=Path)
    parser.add_argument('--windows', type=Path)
    parser.add_argument('--minutes', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence.read_bytes()
    report = audit(evidence, args.protocol.read_bytes(),
                   json.loads(args.reference.read_text(encoding='utf-8')))
    if bool(args.windows) != bool(args.minutes):
        parser.error('--windows and --minutes must be supplied together')
    if args.minutes:
        add_paths(report, json.loads(evidence), args.windows.read_bytes(), args.minutes.read_bytes())
    args.output.write_bytes((json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n').encode())
