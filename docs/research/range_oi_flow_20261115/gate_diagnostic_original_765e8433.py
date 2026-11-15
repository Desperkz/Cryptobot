"""Pure as-of OI/flow confirmation for a separate RANGE_REBOUND_V3 study.

The collector's minute segments are not intrinsically complete. A target minute
is admitted here only when its contiguous aggregate-ID span is bracketed by the
preceding and succeeding buckets of the same observed connection. This proves a
bounded observed aggTrade-stream interval, not a complete exchange trade tape,
liquidation total, order-book queue or executable prospective decision.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

MINUTE = 60_000
OI_LOOKBACK_MS = 5 * MINUTE
MAX_OI_AGE_MS = 90_000
MAX_OI_ROWS = 64
MAX_SEGMENT_ROWS = 24
MAX_CONNECTION_ROWS = 24
MAX_ISSUE_ROWS = 512
RULE = {
    'name': 'range-oi-flow-confirmation-v1',
    'order_capability': False,
    'decision_time': 'unchanged source opened_ms; feature received_ms and exchange time must not exceed this as-of time',
    'oi': 'latest valid sample received by decision and latest received by decision-5min; each endpoint at most90s old; strict decline',
    'flow': 'last completed UTC minute; LONG buy_quote>sell_quote; SHORT sell_quote>buy_quote; equality rejects',
    'flow_quality': 'one same-session target segment, full contiguous ID span/messages, adjacent previous-last and successor-first IDs; all used observations received by decision',
    'connection_quality': 'same session starts before flow minute, covers minute and known successor; no recorded lifecycle, ID-gap or invalid-stream issue in five-minute context plus endpoint age allowance',
    'unknown': 'missing, malformed, stale, late-mutated or uncertified evidence is UNKNOWN, never a rejection or zero return',
    'costs': 'unchanged source entries, exits, quantity, risk and modeled costs; no replacement trades',
    'scope': 'as-of paired observational label; delayed source publication is not proof of executable timing',
    'future_safety': 'future rows cannot add feature information; a final target aggregate containing post-decision receipts is UNKNOWN because its past prefix cannot be reconstructed',
    'market_scope': 'observed Binance aggTrade stream only, not all exchange transactions or complete liquidation volumes',
}


class _Unknown(ValueError):
    pass


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _Unknown('INVALID_TIMESTAMP_OR_ID')
    return value


def _number(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise _Unknown('INVALID_NUMBER')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise _Unknown('INVALID_NUMBER') from None
    if not number.is_finite() or not math.isfinite(float(number)) or (number <= 0 if positive else number < 0):
        raise _Unknown('INVALID_NUMBER')
    return number


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get('payload', row)
    if isinstance(value, str):
        if len(value) > 32_768:
            raise _Unknown('OVERSIZED_PAYLOAD')
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise _Unknown('INVALID_PAYLOAD') from None
    if not isinstance(value, dict):
        raise _Unknown('INVALID_PAYLOAD')
    return value


def _bounded(rows: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(rows, (list, tuple)) or len(rows) > limit:
        raise _Unknown('MISSING_OR_OVERSIZED_CONTEXT')
    if not all(isinstance(row, dict) for row in rows):
        raise _Unknown('INVALID_CONTEXT_ROW')
    return list(rows)


def _endpoint(samples: list[dict[str, Any]], symbol: str, anchor: int, decision: int) -> dict[str, Any]:
    available = []
    for row in samples:
        received = _integer(row.get('received_ms'))
        if received > decision:
            continue  # No parsing, values or ordering from a future response.
        if row.get('symbol', symbol) != symbol:
            continue
        payload = _payload(row)
        if _integer(payload['received_ms']) != received:
            raise _Unknown('OI_RECEIPT_TIME_MISMATCH')
        requested = _integer(payload['request_started_ms'])
        data = payload['data']
        if not isinstance(data, dict) or data.get('symbol') != symbol:
            raise _Unknown('OI_SYMBOL_MISMATCH')
        exchange = _integer(data['time'])
        value = _number(data['openInterest'], positive=True)
        if not 0 <= received-requested <= 20_000 or not -5_000 <= received-exchange <= 30_000:
            raise _Unknown('OI_INVALID_OBSERVATION_TIME')
        if received <= anchor and exchange <= anchor:
            available.append({'received_ms': received, 'exchange_ms': exchange,
                              'request_started_ms': requested, 'oi': value})
    if not available:
        raise _Unknown('OI_ENDPOINT_MISSING')
    latest_received = max(item['received_ms'] for item in available)
    latest = [item for item in available if item['received_ms'] == latest_received]
    if len({(item['oi'], item['exchange_ms']) for item in latest}) != 1:
        raise _Unknown('OI_ENDPOINT_AMBIGUOUS')
    result = latest[0]
    if anchor-result['received_ms'] > MAX_OI_AGE_MS or anchor-result['exchange_ms'] > MAX_OI_AGE_MS:
        raise _Unknown('OI_ENDPOINT_STALE')
    return result


def _issue_check(issues: list[dict[str, Any]], symbol: str, start: int, decision: int) -> None:
    for row in issues:
        at = _integer(row.get('at_ms'))
        if at > decision or at < start or row.get('symbol', '') not in ('', symbol):
            continue
        kind = row.get('kind')
        if kind in ('AGGREGATE_ID_GAP', 'INVALID_STREAM_EVENT',
                    'WEBSOCKET_DISCONNECTED', 'PROCESS_RESTART_GAP'):
            raise _Unknown('CONTEXT_'+kind)
        if kind == 'REST_ERROR':
            detail = row.get('detail', {})
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except ValueError:
                    raise _Unknown('INVALID_ISSUE_DETAIL') from None
            if not isinstance(detail, dict):
                raise _Unknown('INVALID_ISSUE_DETAIL')
            if detail.get('kind') == 'open_interest':
                raise _Unknown('CONTEXT_OI_COLLECTION_ERROR')


def _find_segment(rows: list[dict[str, Any]], symbol: str, minute: int,
                  session: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    matches = [row for row in rows if row.get('symbol') == symbol
               and row.get('minute_ms') == minute
               and (session is None or row.get('session') == session)]
    if len(matches) != 1:
        raise _Unknown('FLOW_SEGMENT_MISSING_OR_SPLIT')
    row = matches[0]
    if not isinstance(row.get('session'), str) or not row['session']:
        raise _Unknown('FLOW_SESSION_MISSING')
    return row, _payload(row)


def _flow(segments: list[dict[str, Any]], connections: list[dict[str, Any]],
          symbol: str, minute: int, decision: int) -> dict[str, Any]:
    row, flow = _find_segment(segments, symbol, minute)
    session = row['session']
    _, previous = _find_segment(segments, symbol, minute-MINUTE, session)
    _, succeeding = _find_segment(segments, symbol, minute+MINUTE, session)
    first, last, messages = (_integer(flow[k]) for k in ('first_id', 'last_id', 'messages'))
    first_event, last_event = (_integer(flow[k]) for k in ('first_event_ms', 'last_event_ms'))
    first_received, last_received = (_integer(flow[k]) for k in ('first_received_ms', 'last_received_ms'))
    if not first <= last or messages <= 0 or messages != last-first+1:
        raise _Unknown('FLOW_NONCONTIGUOUS_ID_SPAN')
    if not minute <= first_event <= last_event < minute+MINUTE:
        raise _Unknown('FLOW_EVENT_MINUTE_MISMATCH')
    if not first_received <= last_received <= decision:
        raise _Unknown('FLOW_LATE_MUTATED_AGGREGATE')
    if not -5_000 <= first_received-first_event <= 30_000 or not -5_000 <= last_received-last_event <= 30_000:
        raise _Unknown('FLOW_INVALID_RECEIPT_TIME')
    before_id = _integer(previous['last_id'])
    before_event = _integer(previous['last_event_ms'])
    before_received = _integer(previous['last_received_ms'])
    after_id = _integer(succeeding['first_id'])
    after_event = _integer(succeeding['first_event_ms'])
    after_received = _integer(succeeding['first_received_ms'])
    if not minute-MINUTE <= before_event < minute or not minute+MINUTE <= after_event < minute+2*MINUTE:
        raise _Unknown('FLOW_NEIGHBOR_EVENT_TIME_MISMATCH')
    if not before_received <= first_received or not last_received <= after_received <= decision:
        raise _Unknown('FLOW_NEIGHBOR_NOT_KNOWN_AS_OF_DECISION')
    if before_id+1 != first or last+1 != after_id:
        raise _Unknown('FLOW_UNCERTIFIED_BOUNDARY_IDS')
    matches = [r for r in connections if r.get('session') == session]
    if len(matches) != 1:
        raise _Unknown('FLOW_CONNECTION_MISSING_OR_AMBIGUOUS')
    connection = matches[0]
    started = _integer(connection['started_ms'])
    last_connection_receive = _integer(connection['last_received_ms'])
    ended = connection.get('ended_ms')
    if ended is not None:
        ended = _integer(ended)
    # Later lifecycle metadata may establish that this already-observed session
    # existed. No later trade values enter the gate; known successor is <=as-of.
    if started > minute or last_connection_receive < after_received or (ended is not None and ended < after_received):
        raise _Unknown('FLOW_CONNECTION_DOES_NOT_COVER_MINUTE')
    buys, sells = (_number(flow[k]) for k in ('buy_quote', 'sell_quote'))
    if buys+sells <= 0:
        raise _Unknown('FLOW_VOLUME_MISSING')
    return {'minute_ms': minute, 'minute_end_ms': minute+MINUTE, 'session': session,
            'buy_quote': float(buys), 'sell_quote': float(sells),
            'imbalance': float((buys-sells)/(buys+sells)),
            'first_id': first, 'last_id': last, 'messages': messages,
            'last_received_ms': last_received, 'successor_first_received_ms': after_received,
            'coverage': 'CONTIGUOUS_SAME_SESSION_OBSERVED_AGGTRADE_INTERVAL',
            '_buys': buys, '_sells': sells}


def evaluate_gate(symbol: str, direction: str, decision_ms: int, *, oi_samples: Any,
                  segments: Any, connections: Any, issues: Any,
                  collector_registered_ms: int) -> dict[str, Any]:
    """Return ACCEPT / REJECT / UNKNOWN without outcomes, network or writes.

    Each input is a bounded list of dictionaries matching collector SQLite rows;
    ``payload`` / ``detail`` may be decoded dictionaries or JSON strings. For a
    source decision T, the caller supplies OI context [T-390s,T+30s], target flow
    minute ``floor(T/60s)*60s-60s`` plus both neighboring minute buckets, relevant
    connections and issue rows. Truncated contexts must not be supplied as if
    complete. UNKNOWN must retain its source-control outcome separately.
    """
    evidence: dict[str, Any] = {}
    try:
        decision = _integer(decision_ms)
        registration = _integer(collector_registered_ms)
        if not isinstance(symbol, str) or not symbol.endswith('USDT') or direction not in ('LONG', 'SHORT'):
            raise _Unknown('INVALID_SOURCE_IDENTITY')
        context_start = decision-OI_LOOKBACK_MS-MAX_OI_AGE_MS
        minute = decision//MINUTE*MINUTE-MINUTE
        evidence.update({'decision_ms': decision, 'context_start_ms': context_start,
                         'flow_minute_ms': minute, 'oi_baseline_anchor_ms': decision-OI_LOOKBACK_MS})
        if context_start < registration:
            raise _Unknown('COLLECTOR_REGISTRATION_AFTER_REQUIRED_CONTEXT')
        oi_rows = _bounded(oi_samples, MAX_OI_ROWS)
        flow_rows = _bounded(segments, MAX_SEGMENT_ROWS)
        connection_rows = _bounded(connections, MAX_CONNECTION_ROWS)
        issue_rows = _bounded(issues, MAX_ISSUE_ROWS)
        _issue_check(issue_rows, symbol, context_start, decision)
        older = _endpoint(oi_rows, symbol, decision-OI_LOOKBACK_MS, decision)
        recent = _endpoint(oi_rows, symbol, decision, decision)
        if recent['exchange_ms'] <= older['exchange_ms'] or recent['received_ms'] <= older['received_ms']:
            raise _Unknown('OI_ENDPOINTS_NOT_CHRONOLOGICAL')
        evidence['oi'] = {
            'previous': float(older['oi']), 'recent': float(recent['oi']),
            'change_fraction': float(recent['oi']/older['oi']-1),
            'previous_received_ms': older['received_ms'], 'recent_received_ms': recent['received_ms'],
            'previous_exchange_ms': older['exchange_ms'], 'recent_exchange_ms': recent['exchange_ms'],
            'observed_interval_ms': recent['exchange_ms']-older['exchange_ms'],
        }
        flow = _flow(flow_rows, connection_rows, symbol, minute, decision)
        buys, sells = flow.pop('_buys'), flow.pop('_sells')
        evidence['flow'] = flow
        oi_declines = recent['oi'] < older['oi']
        flow_confirms = buys > sells if direction == 'LONG' else sells > buys
        evidence['conditions'] = {'oi_declines': oi_declines, 'flow_confirms': flow_confirms}
        reason = ('CONFIRMED' if oi_declines and flow_confirms else
                  'OI_AND_FLOW_NOT_CONFIRMING' if not oi_declines and not flow_confirms else
                  'OI_NOT_DECLINING' if not oi_declines else 'FLOW_NOT_CONFIRMING')
        return {'decision': 'ACCEPT' if oi_declines and flow_confirms else 'REJECT',
                'reason': reason, 'data_quality': 'PASS',
                'accepted': oi_declines and flow_confirms, 'evidence': evidence}
    except _Unknown as exc:
        reason = str(exc)
    except (KeyError, TypeError, ValueError, ArithmeticError, OverflowError):
        reason = 'MALFORMED_CONTEXT'
    return {'decision': 'UNKNOWN', 'reason': reason, 'data_quality': 'UNKNOWN',
            'accepted': None, 'evidence': evidence}
