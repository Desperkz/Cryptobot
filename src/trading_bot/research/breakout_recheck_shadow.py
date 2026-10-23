"""Future-only paired SQZ admission labels; no network, orders or source writes.

The source pilot supplies its unchanged minute execution model. This observer
freezes an entry-open decision before reading any control outcome.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import time
import zlib
from contextlib import closing
from pathlib import Path

MINUTE = 60000
DAY = 86400000
MAX_PAYLOAD = 8 * 1024 * 1024
MAX_NEW = 32
MAX_PENDING = 64
MAX_PRODUCER_AGE_MS = 1200000
STUDY = 'sqz-entry-recheck-shadow-v1'
COHORT = '2026-10-23-sqz-entry-recheck-shadow-v1'
DATABASE = 'breakout_recheck_shadow.sqlite3'
RULE = {
    'study': STUDY, 'cohort': COHORT, 'mode': 'SHADOW', 'order_capability': False,
    'selection': 'new FIRST_OBSERVATION/BASELINE_2R positions; observed and signal close >= registration',
    'gate': 'ALL sources: LONG raw entry-minute open > compression_high; SHORT open < compression_low; unchanged stop valid',
    'timing': 'prospective source selection, delayed minute-open measurement; zero-latency model, not live quote capture',
    'pairing': 'same source entry and unchanged stop/size/2R/costs; accepted result identical to control; rejection zero cash',
    'capacity': 'no replacement entries or portfolio readmission; original pilot occupancy retained',
    'risk_usdt_before_costs': 2, 'fees_bps_each_side': 4, 'slippage_bps_each_side': 5,
    'maximum_holding_minutes': 1440,
    'funding': 'unchanged signed entry rate estimate or adverse 1bp/8h fallback',
    'review': '14 elapsed days and 50 closed pairs; descriptive, no automatic promotion',
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('NONFINITE_NUMBER')
    return result


def integer(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('INVALID_TIMESTAMP')
    return value


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.25)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA cache_size=-512')
    return db


def source_manifest(db):
    row = db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
    if row is None:
        raise ValueError('SOURCE_MANIFEST_MISSING')
    value = json.loads(row[0])
    if (value.get('mode') != 'LOCAL_PAPER_ONLY'
            or value.get('data_venue') != 'https://fapi.binance.com'
            or 'BASELINE_2R' not in value.get('arms', [])
            or 'FIRST_OBSERVATION' not in value.get('policies', [])
            or value.get('risk_usdt') != 2
            or value.get('entry') != 'next_minute_open_after_observation_plus_5bps'
            or value.get('fees_bps_each_side') != 4
            or value.get('exit_slippage_bps') != 5
            or value.get('maximum_holding_minutes') != 1440):
        raise ValueError('UNEXPECTED_SOURCE_PROTOCOL')
    return value


def producer_health(src, now_ms):
    """A live follower does not imply that its source is still producing data."""
    values = {r['key']: r['value'] for r in src.execute(
        "SELECT key,value FROM meta WHERE key IN ('scan','monitor')")}
    telemetry = {'maximum_age_seconds': MAX_PRODUCER_AGE_MS//1000}
    errors = []
    for name in ('scan', 'monitor'):
        try:
            value = json.loads(values[name])
            stamp = integer(value['finished_ms'] if name == 'scan' else value['at_ms'])
            elapsed = now_ms-stamp
            telemetry[name] = {'at_ms': stamp, 'age_seconds': elapsed/1000,
                               'status': value.get('status')}
            if value.get('status') != 'OK' or value.get('errors'):
                errors.append({'source': name, 'error': 'SOURCE_'+name.upper()+'_ERROR'})
            if elapsed < 0 or elapsed > MAX_PRODUCER_AGE_MS:
                errors.append({'source': name, 'error': 'SOURCE_'+name.upper()+'_STALE_OR_FUTURE'})
        except (ValueError, KeyError, TypeError, OverflowError):
            telemetry[name] = {'status': 'MISSING_OR_INVALID'}
            errors.append({'source': name, 'error': 'SOURCE_'+name.upper()+'_HEALTH_MISSING_OR_INVALID'})
    return telemetry, errors


def identity(settings, code_root, source):
    if settings != {'study': STUDY, 'cohort': COHORT, 'mode': 'SHADOW', 'order_capability': False}:
        raise ValueError('UNEXPECTED_SETTINGS')
    model = Path(code_root) / 'src/trading_bot/research/paper_execution.py'
    return {'rule': RULE, 'settings': settings, 'source_path': str(Path(source).resolve()),
            'worker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'execution_model_sha256': hashlib.sha256(model.read_bytes()).hexdigest()}


def open_store(directory, source, settings, code_root, now_ms=None):
    """Register atomically at the source watermark, or verify frozen identity."""
    now = int(time.time()*1000) if now_ms is None else integer(now_ms)
    fixed = identity(settings, code_root, source)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / DATABASE, timeout=.25)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA cache_size=-512')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY,source_id TEXT UNIQUE,
            entry_ms INTEGER NOT NULL,plan TEXT NOT NULL,provenance TEXT NOT NULL,
            decision TEXT,control_result TEXT,error TEXT,last_checked_ms INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS excluded(id INTEGER PRIMARY KEY,reason TEXT NOT NULL);
    ''')
    try:
        with closing(readonly(source)) as src:
            src.execute('BEGIN')
            original = source_manifest(src)
            saved = db.execute("SELECT value FROM meta WHERE key='registration'").fetchone()
            if saved:
                registration = json.loads(saved[0])
                if registration['identity'] != fixed or registration['source_manifest'] != original:
                    raise ValueError('FROZEN_STUDY_CHANGED')
            else:
                watermark = src.execute('SELECT coalesce(max(id),0) FROM positions').fetchone()[0]
                registration = {'identity': fixed, 'source_manifest': original,
                                'registered_ms': now, 'source_watermark': watermark}
                with db:
                    db.execute('INSERT INTO meta VALUES(?,?)', ('registration', canonical(registration)))
                    db.execute('INSERT INTO meta VALUES(?,?)', ('cursor', str(watermark)))
            src.rollback()
        return db, registration
    except Exception:
        db.close()
        raise


def decode_observation(row):
    blob = row['payload_zlib']
    if blob is None or len(blob) > MAX_PAYLOAD:
        raise ValueError('SOURCE_PAYLOAD_TOO_LARGE')
    decoder = zlib.decompressobj()
    raw = decoder.decompress(blob, MAX_PAYLOAD + 1)
    if len(raw) > MAX_PAYLOAD or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise ValueError('SOURCE_PAYLOAD_TOO_LARGE_OR_INVALID')
    if hashlib.sha256(raw).hexdigest() != row['payload_sha256']:
        raise ValueError('SOURCE_CHECKSUM_MISMATCH')
    payload = json.loads(raw)
    signal = payload['signal']
    metadata = signal['metadata']
    return {'source_id': payload['source_id'], 'observed_ms': payload['observed_ms'],
            'symbol': signal['symbol'], 'direction': signal['direction'],
            'signal_entry': signal['entry_price'], 'stop': signal['stop_loss'],
            'signal_bar_close_time': metadata['signal_bar_close_time'],
            'compression_low': metadata.get('compression_low'),
            'compression_high': metadata.get('compression_high'),
            'volume_ratio': metadata.get('volume_ratio'), 'regime': metadata.get('regime'),
            'observed_market': observed_market(payload),
            'payload_sha256': row['payload_sha256'], 'observation_id': row['id']}


def observed_market(payload):
    """Optional transition diagnostic; never used for the entry decision."""
    requests = payload.get('evidence', {}).get('public_requests', {})
    for key, value in requests.items():
        if key.startswith('/fapi/v1/ticker/24hr:') and value.get('at_ms', math.inf) <= payload['observed_ms']:
            try:
                return finite(value['data']['lastPrice'])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def validate_candidate(row, observation, registered, now_ms):
    plan = json.loads(row['plan'])
    observed = integer(plan['observed_ms'])
    closed = integer(observation['signal_bar_close_time'])
    entry = integer(row['entry_ms'])
    if observed > now_ms or closed > now_ms:
        raise ValueError('FUTURE_SOURCE_DEFERRED')
    if min(observed, closed) < registered or entry <= registered:
        raise ValueError('PRE_REGISTRATION')
    expected = f"SQZ:{row['symbol']}:{row['direction']}:{closed}"
    if (row['source_id'] != expected or observation['source_id'] != expected
            or observation['symbol'] != row['symbol'] or observation['direction'] != row['direction']
            or observation['observed_ms'] != observed or row['direction'] not in ('LONG', 'SHORT')
            or closed > observed or entry != (observed // MINUTE + 1) * MINUTE
            or plan.get('source_id') != expected or plan.get('risk_usdt') != 2
            or plan.get('max_minutes') != 1440
            or plan.get('targets') != [[2., 1., False, False]]
            or plan.get('exit_model') != 'minute_ohlc_conservative_v1'
            or plan.get('venue') != 'https://fapi.binance.com'
            or finite(plan['stop']) != finite(observation['stop'])
            or finite(plan['signal_entry']) != finite(observation['signal_entry'])):
        raise ValueError('SOURCE_IDENTITY_OR_PLAN_MISMATCH')
    low, high = finite(observation['compression_low']), finite(observation['compression_high'])
    sign = 1 if row['direction'] == 'LONG' else -1
    stop, signal_entry = finite(plan['stop']), finite(plan['signal_entry'])
    if not 0 < low < high or min(stop, signal_entry) <= 0 or sign * (signal_entry - stop) <= 0:
        raise ValueError('INVALID_SOURCE_BOUNDARY_OR_STOP')
    if plan.get('funding_rate') is not None:
        finite(plan['funding_rate'])
    return {**plan, 'symbol': row['symbol'], 'direction': row['direction'], 'entry_ms': entry,
            'compression_low': low, 'compression_high': high,
            'observed_market': observation['observed_market']}


def entry_decision(plan, bar, now_ms):
    """Pure gate: no result/outcome inputs; only raw first-minute open is used."""
    if len(bar) < 7 or integer(bar[0]) != plan['entry_ms'] or integer(bar[6]) != bar[0]+MINUTE-1:
        raise ValueError('INVALID_ENTRY_CANDLE_TIME')
    if bar[6] >= now_ms:
        raise ValueError('ENTRY_CANDLE_NOT_CLOSED')
    o, h, low, c = (finite(bar[i]) for i in (1, 2, 3, 4))
    if min(o, h, low, c) <= 0 or not low <= min(o, c) <= max(o, c) <= h:
        raise ValueError('INVALID_ENTRY_CANDLE_OHLC')
    sign = 1 if plan['direction'] == 'LONG' else -1
    boundary = plan['compression_high'] if sign == 1 else plan['compression_low']
    beyond = sign*(o-boundary) > 0
    valid_stop = sign*(o-finite(plan['stop'])) > 0
    observed = plan.get('observed_market')
    before = 'UNKNOWN' if observed is None else ('OUTSIDE' if sign*(observed-boundary)>0 else 'INSIDE')
    return {'kept': beyond and valid_stop, 'reason': 'KEEP' if beyond and valid_stop else
            ('STOP_WRONG_SIDE_RAW_OPEN' if not valid_stop else 'BREAKOUT_NOT_PRESERVED'),
            'raw_open': o, 'boundary': boundary, 'entry_ms': plan['entry_ms'],
            'first_bar_sha256': digest(bar), 'transition': before+'->'+('OUTSIDE' if beyond else 'INSIDE')}


def ingest(db, src, registration, now_ms):
    cursor = int(db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0])
    rows = src.execute('''SELECT id,source_id,symbol,direction,entry_ms,plan FROM positions
        WHERE id>? AND policy='FIRST_OBSERVATION' AND arm='BASELINE_2R' ORDER BY id LIMIT ?''',
        (cursor, MAX_NEW)).fetchall()
    for row in rows:
        try:
            plan = json.loads(row['plan'])
            if integer(plan['observed_ms']) > now_ms:
                break  # Do not advance the cursor over a racing/future observation.
            obs = src.execute('''SELECT id,source_id,observed_ms,payload_sha256,
                CASE WHEN length(payload_zlib)<=? THEN payload_zlib ELSE NULL END AS payload_zlib
                FROM observations WHERE source_id=? AND observed_ms=? ORDER BY id LIMIT 2''',
                (MAX_PAYLOAD, row['source_id'], plan['observed_ms'])).fetchall()
            if not obs:
                break  # Retain the candidate until its transaction/evidence is visible.
            if len(obs) != 1:
                raise ValueError('DUPLICATE_SOURCE_OBSERVATION')
            first = src.execute('SELECT min(id) FROM observations WHERE source_id=?', (row['source_id'],)).fetchone()[0]
            if obs[0]['id'] != first:
                raise ValueError('NOT_FIRST_OBSERVATION')
            observation = decode_observation(obs[0])
            copied = validate_candidate(row, observation, registration['registered_ms'], now_ms)
            provenance = {'position_plan_sha256': hashlib.sha256(row['plan'].encode()).hexdigest(),
                          'source_position_id': row['id'], 'source_manifest_sha256': digest(registration['source_manifest']),
                          'observation': observation}
            with db:
                db.execute('INSERT OR IGNORE INTO sources(id,source_id,entry_ms,plan,provenance) VALUES(?,?,?,?,?)',
                           (row['id'], row['source_id'], row['entry_ms'], canonical(copied), canonical(provenance)))
                db.execute("UPDATE meta SET value=? WHERE key='cursor'", (str(row['id']),))
        except (ValueError, KeyError, TypeError, OverflowError, zlib.error) as exc:
            if str(exc) == 'FUTURE_SOURCE_DEFERRED':
                break
            with db:
                db.execute('INSERT OR IGNORE INTO excluded VALUES(?,?)', (row['id'], str(exc)[:300]))
                db.execute("UPDATE meta SET value=? WHERE key='cursor'", (str(row['id']),))


def update(db, src, row, now_ms):
    plan = json.loads(row['plan'])
    # Deliberately omit result here: entry decisions cannot depend on outcomes.
    source = src.execute('SELECT status,plan FROM positions WHERE id=?', (row['id'],)).fetchone()
    provenance = json.loads(row['provenance'])
    if source is None or hashlib.sha256(source['plan'].encode()).hexdigest() != provenance['position_plan_sha256']:
        raise ValueError('SOURCE_POSITION_MISSING_OR_CHANGED')
    if source['status'] == 'INVALID_ENTRY':
        raise ValueError('UNSCORABLE_SOURCE_INVALID_ENTRY')
    if source['status'] not in ('PENDING', 'OPEN', 'CLOSED'):
        raise ValueError('UNEXPECTED_SOURCE_STATUS')
    if row['decision'] is None:
        saved = src.execute('SELECT payload FROM execution_candles WHERE symbol=? AND open_ms=?',
                            (plan['symbol'], plan['entry_ms'])).fetchone()
        if saved is None:
            return
        decision = entry_decision(plan, json.loads(saved[0]), now_ms)
        # Commit immutable decision BEFORE any SELECT of a control result.
        with db:
            db.execute('UPDATE sources SET decision=? WHERE id=? AND decision IS NULL',
                       (canonical(decision), row['id']))
    source_result = src.execute('SELECT status,result FROM positions WHERE id=?', (row['id'],)).fetchone()
    if source_result['status'] != 'CLOSED':
        return
    result = json.loads(source_result['result'])
    pnl, r = finite(result['model_pnl_usdt']), finite(result['R'])
    decision = json.loads(db.execute('SELECT decision FROM sources WHERE id=?', (row['id'],)).fetchone()[0])
    sign = 1 if plan['direction'] == 'LONG' else -1
    expected_entry = decision['raw_open']*(1+sign*.0005)
    component_net = (finite(result['gross_R'])-finite(result['fees_R'])
                     -finite(result['exit_slippage_R'])-finite(result['funding_R']))
    if (result.get('status') != 'CLOSED' or result.get('entry_ms') != plan['entry_ms']
            or not plan['entry_ms'] <= integer(result['last_ms']) < plan['entry_ms']+DAY
            or not math.isclose(finite(result['entry_price']), expected_entry, abs_tol=1e-10, rel_tol=1e-12)
            or not math.isclose(r, component_net, abs_tol=1e-10, rel_tol=1e-12)
            or not math.isclose(pnl, 2*r, abs_tol=1e-10, rel_tol=1e-12)):
        raise ValueError('INVALID_CLOSED_CONTROL_RESULT')
    with db:
        db.execute('UPDATE sources SET control_result=? WHERE id=? AND control_result IS NULL',
                   (canonical(result), row['id']))


def status(db, registration, now_ms, errors=()):
    counts = {key: 0 for key in ('sources', 'waiting_entry', 'decided', 'kept', 'rejected',
                                 'unscorable', 'open_controls', 'closed_pairs')}
    pnl = {key: 0. for key in ('control_net_usdt', 'recheck_net_usdt', 'delta_usdt',
                               'avoided_losses_usdt', 'lost_profits_usdt')}
    transitions = {}
    # Pre-registration sources are ineligible, whereas malformed new records
    # are visible as unscorable instead of silently disappearing.
    invalid = db.execute("SELECT count(*) FROM excluded WHERE reason<>'PRE_REGISTRATION'").fetchone()[0]
    counts['sources'] += invalid
    counts['unscorable'] += invalid
    for row in db.execute('SELECT decision,control_result,error FROM sources'):
        counts['sources'] += 1
        if row['error']:
            counts['unscorable'] += 1
            continue
        if row['decision'] is None:
            counts['waiting_entry'] += 1
            continue
        decision = json.loads(row['decision'])
        counts['decided'] += 1
        counts['kept' if decision['kept'] else 'rejected'] += 1
        key = decision['transition']
        transitions[key] = transitions.get(key, 0)+1
        if row['control_result'] is None:
            counts['open_controls'] += 1
            continue
        control = finite(json.loads(row['control_result'])['model_pnl_usdt'])
        counts['closed_pairs'] += 1
        pnl['control_net_usdt'] += control
        pnl['recheck_net_usdt'] += control if decision['kept'] else 0.
        if not decision['kept']:
            pnl['avoided_losses_usdt'] += max(0., -control)
            pnl['lost_profits_usdt'] += max(0., control)
    pnl['delta_usdt'] = pnl['recheck_net_usdt']-pnl['control_net_usdt']
    elapsed = max(0., (now_ms-registration['registered_ms'])/DAY)
    return {'study': STUDY, 'mode': 'SHADOW', 'order_capability': False, 'cohort': COHORT,
            'registered_ms': registration['registered_ms'], 'generated_at_ms': now_ms,
            'health': 'DEGRADED' if errors or counts['unscorable'] else 'OK', 'errors': list(errors),
            'counts': counts, 'pnl': pnl, 'transitions': transitions,
            'review': {'elapsed_days': elapsed, 'required_days': 14, 'required_closed_pairs': 50,
                       'ready': elapsed >= 14 and counts['closed_pairs'] >= 50},
            'timing': RULE['timing'], 'automatic_promotion': False}


def write_status(directory, value):
    path = Path(directory)/'status.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def cycle(db, source, registration, now_ms=None):
    now = int(time.time()*1000) if now_ms is None else integer(now_ms)
    errors = []
    with closing(readonly(source)) as src:
        if source_manifest(src) != registration['source_manifest']:
            raise ValueError('FROZEN_SOURCE_MANIFEST_CHANGED')
        telemetry, source_errors = producer_health(src, now)
        if source_errors:
            value = status(db, registration, now, source_errors)
            value['source_health'] = telemetry
            return value
        ingest(db, src, registration, now)
        rows = db.execute('''SELECT * FROM sources WHERE control_result IS NULL AND error IS NULL
            ORDER BY last_checked_ms,id LIMIT ?''', (MAX_PENDING,)).fetchall()
        for row in rows:
            try:
                update(db, src, row, now)
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                error = str(exc)[:300]
                if error == 'ENTRY_CANDLE_NOT_CLOSED':
                    continue
                errors.append({'source_position_id': row['id'], 'error': error})
                with db:
                    db.execute('UPDATE sources SET error=? WHERE id=?', (error, row['id']))
            finally:
                with db:
                    db.execute('UPDATE sources SET last_checked_ms=? WHERE id=?', (now, row['id']))
    value = status(db, registration, now, errors)
    value['source_health'] = telemetry
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', default='/root/bot_mainnet_paper/data/mainnet_paper_lab.sqlite3')
    parser.add_argument('--data-dir', default='/root/bot_breakout_recheck_shadow/data')
    parser.add_argument('--code-root', default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument('--init-only', action='store_true')
    args = parser.parse_args()
    settings = json.loads((Path(args.code_root)/'breakout_recheck_shadow_settings.json').read_text(encoding='utf-8'))
    db, registration = open_store(args.data_dir, args.source_db, settings, args.code_root)
    try:
        try:
            if args.init_only:
                now = int(time.time()*1000)
                with closing(readonly(args.source_db)) as src:
                    telemetry, errors = producer_health(src, now)
                value = status(db, registration, now, errors)
                value['source_health'] = telemetry
            else:
                value = cycle(db, args.source_db, registration)
        except Exception as exc:
            value = status(db, registration, int(time.time()*1000), [{'error': str(exc)[:300]}])
        write_status(args.data_dir, value)
        print(canonical(value))
        return 0 if value['health'] == 'OK' else 1
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
