"""Future-only paired RANGE exit experiment. Public minutes; no order capability."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import urllib.parse
import urllib.request

from . import range_oi_flow_shadow as source_contract
from .range_profit_execution import MINUTE, advance_control, advance_protected, validate_bars

STUDY = 'range-profit-protection-shadow-v1'
COHORT = '2026-11-21-range-close-1r-entry-stop-v1'
SOURCE = source_contract.SOURCE
STRATEGY = source_contract.STRATEGY
MAX_DISCOVERY_MS = 180_000
MAX_STORE_BYTES = 128 * 1024**2
PROTOCOL = {
    'arm_at_r': 1, 'arm_on': 'CLOSED_1M_CLOSE', 'protected_stop': 'ENTRY',
    'effective': 'NEXT_FULL_MINUTE', 'fees_bps_per_side': 4,
    'slippage_bps_per_side': 5, 'adverse_funding_bps_per_8h': 1, 'risk_usdt': 2,
}
RULE = {
    'study': STUDY, 'cohort': COHORT, 'mode': 'SHADOW', 'order_capability': False,
    'combined_with_main_balance': False, 'source_strategy': STRATEGY,
    'source_manifest_sha256': source_contract.SOURCE_HASH, 'protocol': PROTOCOL,
    'selection': 'All future source RANGE entries independent of OI/flow; id above registration watermark; opened_ms and signal bar_ms at/after registration. No replacements after an early variant exit.',
    'timing': 'Admission committed before outcomes/minute requests. Discovery over 180 seconds is permanently UNKNOWN. As-of source plans are observational labels, not executable fills.',
    'execution': 'First full 1m bar after entry; original stop then target then timeout before arming; arm at close >=1 initial price-stop R; entry-price stop from next full minute; stop first on ambiguity, adverse open on stop gaps.',
    'comparison': 'Both arms closed, original control replay verified against frozen source, errors excluded from paired totals. UNKNOWN controls separate. Never revise old admissions.',
    'costs': 'Source entry, quantity, target, expiry and costs unchanged; price-entry stop still loses fees/slippage/funding. Funding changes only with actual modeled holding time.',
    'review': 'Descriptive review after 14 days and 50 matched closed pairs; no automatic promotion or profitability claim.',
}
LIMITATIONS = [
    'Сравниваются только новые исходные RANGE-входы. Ранний выход не добавляет новых входов.',
    'Стоп на цене входа не гарантирует безубыток после комиссий, проскальзывания и funding.',
    'Итоги сравнения включают только пары с двумя закрытыми вариантами и проверенным контролем.',
    'Минутные свечи и отложенная публикация входа не доказывают реальное исполнение. Пропуски данных не считаются нулём.',
    'Модельные суммы коррелированных наблюдений не являются доходностью реального портфеля.',
]

canonical = source_contract.canonical
digest = source_contract.digest
readonly = source_contract.readonly


def identity(source):
    names = ('range_profit_protection.py', 'range_profit_execution.py',
             'range_oi_flow_shadow.py', 'range_oi_flow_gate.py')
    return {'rule': RULE, 'source_path': str(Path(source).resolve()),
            'code_sha256': {n: hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in names}}


def open_store(directory, source=SOURCE, now_ms=None):
    now = int(time.time()*1000) if now_ms is None else source_contract.integer(now_ms)
    directory = Path(directory).resolve()
    if directory == Path(source).resolve().parent:
        raise ValueError('USE_SEPARATE_DIRECTORY')
    with closing(readonly(source)) as src:
        source_contract.validate_source(src)
        watermark = src.execute('SELECT coalesce(max(id),0) FROM positions').fetchone()[0]
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory/'range_profit_protection.sqlite3', timeout=.5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA cache_size=-512')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pairs(
            id INTEGER PRIMARY KEY,opened_ms INTEGER NOT NULL,plan TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL,snapshot TEXT NOT NULL,admission TEXT NOT NULL,
            reason TEXT NOT NULL,saved_ms INTEGER NOT NULL,control TEXT,
            control_replay TEXT,protected TEXT,execution_state TEXT NOT NULL,
            parity_valid INTEGER NOT NULL DEFAULT 0,last_checked_ms INTEGER NOT NULL DEFAULT 0,error TEXT);
        CREATE TABLE IF NOT EXISTS exclusions(id INTEGER PRIMARY KEY,strategy TEXT NOT NULL,
            opened_ms INTEGER NOT NULL,reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS minute_receipts(id INTEGER PRIMARY KEY,symbol TEXT NOT NULL,
            start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,received_ms INTEGER NOT NULL,
            response_sha256 TEXT NOT NULL,rows INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS minutes(symbol TEXT NOT NULL,open_ms INTEGER NOT NULL,
            payload TEXT NOT NULL,receipt_id INTEGER NOT NULL,PRIMARY KEY(symbol,open_ms));
    ''')
    try:
        fixed = identity(source)
        row = db.execute("SELECT value FROM meta WHERE key='registration'").fetchone()
        if row:
            registration = json.loads(row[0])
            if registration['identity'] != fixed:
                raise ValueError('FROZEN_STUDY_CHANGED')
            cursor = int(db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0])
            if watermark < max(cursor, registration['source_watermark']):
                raise ValueError('SOURCE_WATERMARK_REWOUND')
        else:
            registration = {'identity': fixed, 'registered_ms': now,
                            'source_watermark': watermark,
                            'source_manifest_sha256': source_contract.SOURCE_HASH}
            with db:
                db.execute('INSERT INTO meta VALUES(?,?)', ('registration', canonical(registration)))
                db.execute('INSERT INTO meta VALUES(?,?)', ('cursor', str(watermark)))
        return db, registration
    except BaseException:
        db.close()
        raise


def ingest(db, src, registration, now):
    """Read plan fields only, and commit the admission before any outcome read."""
    cursor = int(db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0])
    watermark = src.execute('SELECT coalesce(max(id),0) FROM positions').fetchone()[0]
    if watermark < cursor:
        raise ValueError('SOURCE_WATERMARK_REWOUND')
    rows = src.execute('SELECT '+','.join(source_contract.PLAN_FIELDS)+
                       ' FROM positions WHERE id>? ORDER BY id LIMIT 256', (cursor,)).fetchall()
    for row in rows:
        plan = dict(row)
        sid = source_contract.integer(plan['id'])
        if plan['opened_ms'] > now:
            break
        reason = None
        if plan['strategy'] != STRATEGY:
            reason = 'OTHER_STRATEGY'
        elif plan['opened_ms'] < registration['registered_ms']:
            reason = 'PRE_REGISTRATION'
        snapshot = src.execute('SELECT id,symbol,observed_ms,bar_ms FROM snapshots WHERE id=?',
                               (plan['snapshot_id'],)).fetchone() if reason is None else None
        if reason is None and snapshot is None:
            raise ValueError('SOURCE_SNAPSHOT_MISSING')
        if reason is None and snapshot['bar_ms'] < registration['registered_ms']:
            reason = 'PRE_REGISTRATION_SIGNAL_BAR'
        if reason:
            with db:
                db.execute('INSERT OR IGNORE INTO exclusions VALUES(?,?,?,?)',
                           (sid, plan['strategy'], plan['opened_ms'], reason))
                db.execute("UPDATE meta SET value=? WHERE key='cursor'", (str(sid),))
            continue
        snapshot = dict(snapshot)
        source_contract.validate_plan(plan, snapshot, now)
        admission = 'UNKNOWN' if now-plan['opened_ms'] > MAX_DISCOVERY_MS else 'ELIGIBLE'
        reason = 'SOURCE_DISCOVERY_AFTER_180_SECONDS' if admission == 'UNKNOWN' else 'FUTURE_RANGE_PAIR'
        first_minute = (plan['opened_ms']//MINUTE+1)*MINUTE
        state = {'control_next_ms': first_minute, 'protected': {'next_ms': first_minute, 'armed_ms': None}}
        with db:
            db.execute('INSERT INTO pairs(id,opened_ms,plan,plan_sha256,snapshot,admission,reason,saved_ms,execution_state) VALUES(?,?,?,?,?,?,?,?,?)',
                       (sid, plan['opened_ms'], canonical(plan), digest(plan), canonical(snapshot),
                        admission, reason, now, canonical(state)))
            db.execute("UPDATE meta SET value=? WHERE key='cursor'", (str(sid),))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('PUBLIC_MINUTE_REDIRECT_REFUSED')


def fetch_minutes(symbol, start_ms, end_ms):
    if symbol not in source_contract.SYMBOLS or not 0 <= start_ms <= end_ms:
        raise ValueError('INVALID_MINUTE_REQUEST')
    query = urllib.parse.urlencode({'symbol': symbol, 'interval': '1m', 'startTime': start_ms,
                                   'endTime': end_ms, 'limit': 500})
    request = urllib.request.Request('https://fapi.binance.com/fapi/v1/klines?'+query,
                                     headers={'User-Agent': STUDY}, method='GET')
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=6) as response:
        raw = response.read(512*1024+1)
    if len(raw) > 512*1024:
        raise ValueError('MINUTE_RESPONSE_TOO_LARGE')
    return json.loads(raw), hashlib.sha256(raw).hexdigest(), int(time.time()*1000)


def store_minutes(db, symbol, start, end, bars, response_sha256, received_ms):
    if not isinstance(response_sha256, str) or len(response_sha256) != 64:
        raise ValueError('INVALID_RESPONSE_HASH')
    # Preserve the first observed closed minute. Conflicting revisions are an error.
    for bar in bars:
        old = db.execute('SELECT payload FROM minutes WHERE symbol=? AND open_ms=?', (symbol, bar[0])).fetchone()
        if old and old[0] != canonical(bar):
            raise ValueError('CLOSED_MINUTE_CHANGED')
    with db:
        rid = db.execute('INSERT INTO minute_receipts(symbol,start_ms,end_ms,received_ms,response_sha256,rows) VALUES(?,?,?,?,?,?)',
                         (symbol, start, end, received_ms, response_sha256, len(bars))).lastrowid
        db.executemany('INSERT OR IGNORE INTO minutes VALUES(?,?,?,?)',
                       ((symbol, b[0], canonical(b), rid) for b in bars))


def same_outcome(replay, control):
    if replay['closed_ms'] != control['closed_ms'] or replay['reason'] != control['reason']:
        return False
    for key in ('exit_price', 'net_usdt'):
        if not math.isclose(replay[key], control[key], rel_tol=1e-10, abs_tol=1e-9):
            return False
    a = json.loads(replay['costs']) if isinstance(replay['costs'], str) else replay['costs']
    b = control['costs']
    return set(a) == set(b) and all(a[k] == b[k] if isinstance(a[k], str) else
        math.isclose(a[k], b[k], rel_tol=1e-10, abs_tol=1e-9) for k in a)


def update_pair(db, src, pair, now, fetcher=fetch_minutes):
    plan = json.loads(pair['plan'])
    current = src.execute('SELECT '+','.join(source_contract.PLAN_FIELDS)+' FROM positions WHERE id=?', (pair['id'],)).fetchone()
    if current is None or digest(dict(current)) != pair['plan_sha256']:
        raise ValueError('SOURCE_PLAN_CHANGED')
    if pair['control'] is None:
        source_contract.update_control(db, src, pair, now)
    pair = db.execute('SELECT * FROM pairs WHERE id=?', (pair['id'],)).fetchone()
    if pair['admission'] == 'UNKNOWN':
        with db:
            db.execute('UPDATE pairs SET error=NULL,last_checked_ms=? WHERE id=?', (now, pair['id']))
        return
    state = json.loads(pair['execution_state'])
    control = json.loads(pair['control_replay']) if pair['control_replay'] else None
    protected = json.loads(pair['protected']) if pair['protected'] else None
    cursors = ([state['control_next_ms']] if control is None else []) + ([state['protected']['next_ms']] if protected is None else [])
    if cursors:
        start = min(cursors)
        end = min(now//MINUTE*MINUTE-1, (plan['expires_ms']//MINUTE+1)*MINUTE-1)
        if start <= end:
            raw, response_sha256, received_ms = fetcher(plan['symbol'], start, end)
            bars = validate_bars(raw, start, end)
            if not bars:
                raise ValueError('MINUTES_NOT_AVAILABLE')
            store_minutes(db, plan['symbol'], start, end, bars, response_sha256, received_ms)
            if control is None:
                control, state['control_next_ms'] = advance_control(
                    {**plan, 'next_ms': state['control_next_ms']},
                    [b for b in bars if b[0] >= state['control_next_ms']])
            if protected is None:
                protected, state['protected'] = advance_protected(
                    plan, state['protected'], [b for b in bars if b[0] >= state['protected']['next_ms']])
    verified = False
    if control is not None and pair['control']:
        if not same_outcome(control, json.loads(pair['control'])):
            raise ValueError('SOURCE_CONTROL_REPLAY_MISMATCH')
        verified = True
    with db:
        db.execute('UPDATE pairs SET control_replay=?,protected=?,execution_state=?,parity_valid=?,last_checked_ms=?,error=NULL WHERE id=?',
                   (canonical(control) if control else None, canonical(protected) if protected else None,
                    canonical(state), int(verified), now, pair['id']))


def input_health(source, now):
    try:
        path = Path(source).with_name('status.json')
        if path.stat().st_size > 131072:
            raise ValueError('STATUS_TOO_LARGE')
        value = json.loads(path.read_text())
        age = now-source_contract.integer(value['generated_at_ms'])
        if value.get('health') != 'OK' or not 0 <= age <= 900000:
            raise ValueError('SOURCE_NOT_FRESH_AND_HEALTHY')
        return {'health': 'OK', 'age_seconds': age/1000}, []
    except (OSError, ValueError, TypeError, KeyError):
        return {'health': 'UNAVAILABLE', 'age_seconds': None}, ['SOURCE_NOT_FRESH_AND_HEALTHY']


def status(db, registration, now, source, errors=()):
    errors = list(errors)
    skipped = db.execute('SELECT count(*) FROM exclusions WHERE strategy=? AND opened_ms>=?',
                         (STRATEGY, registration['registered_ms'])).fetchone()[0]
    counts = dict.fromkeys(('observed', 'eligible', 'skipped', 'unscorable', 'open_pairs', 'closed_pairs'), 0)
    counts['skipped'] = skipped
    results = dict.fromkeys(('paired_control_net_usdt', 'protected_net_usdt', 'paired_delta_usdt',
                             'improved_pairs', 'worsened_pairs', 'unchanged_pairs',
                             'unscorable_control_net_usdt', 'unscorable_controls_closed'), 0)
    recent = []
    for row in db.execute('SELECT * FROM pairs ORDER BY id'):
        plan = json.loads(row['plan'])
        control = json.loads(row['control']) if row['control'] else None
        protected = json.loads(row['protected']) if row['protected'] else None
        state = json.loads(row['execution_state'])
        counts['observed'] += 1
        matched = False
        if row['admission'] == 'UNKNOWN':
            counts['unscorable'] += 1
            if control:
                results['unscorable_controls_closed'] += 1
                results['unscorable_control_net_usdt'] += control['net_usdt']
        else:
            counts['eligible'] += 1
            matched = bool(control and protected and row['parity_valid'] and not row['error'])
            counts['closed_pairs' if matched else 'open_pairs'] += 1
            if matched:
                delta = protected['net_usdt']-control['net_usdt']
                results['paired_control_net_usdt'] += control['net_usdt']
                results['protected_net_usdt'] += protected['net_usdt']
                results['improved_pairs' if delta > 1e-9 else 'worsened_pairs' if delta < -1e-9 else 'unchanged_pairs'] += 1
        if row['error']:
            errors.append(str(row['id'])+':'+row['error'])
        recent.append({'id': row['id'], 'symbol': plan['symbol'], 'direction': plan['direction'],
            'opened_ms': row['opened_ms'], 'admission': row['admission'], 'reason': row['reason'],
            'state': 'UNKNOWN' if row['admission'] == 'UNKNOWN' else 'CLOSED' if matched else 'OPEN',
            'armed_ms': state['protected']['armed_ms'],
            'control_net_usdt': control['net_usdt'] if control else None,
            'protected_net_usdt': protected['net_usdt'] if protected else None,
            'delta_usdt': protected['net_usdt']-control['net_usdt'] if matched else None,
            'control_reason': control['reason'] if control else None,
            'protected_reason': protected['reason'] if protected else None, 'error': row['error']})
    counts['observed'] += skipped
    results['paired_delta_usdt'] = results['protected_net_usdt']-results['paired_control_net_usdt']
    elapsed = max(0, (now-registration['registered_ms'])/86400000)
    return {'study': STUDY, 'cohort': COHORT, 'mode': 'SHADOW', 'order_capability': False,
        'combined_with_main_balance': False, 'registered_ms': registration['registered_ms'],
        'generated_at_ms': now, 'source_watermark': registration['source_watermark'],
        'health': 'DEGRADED' if errors or source['health'] != 'OK' else 'OK',
        'counts': counts, 'results': results, 'source': source, 'protocol': PROTOCOL,
        'review': {'elapsed_days': elapsed, 'required_days': 14, 'required_closed_pairs': 50,
                   'ready': elapsed >= 14 and counts['closed_pairs'] >= 50, 'automatic_promotion': False},
        'recent_pairs': list(reversed(recent[-12:])), 'errors': list(dict.fromkeys(errors)),
        'limitations': LIMITATIONS}


def cycle(db, registration, source=SOURCE, now_ms=None, fetcher=fetch_minutes):
    start_wall = time.monotonic()
    now = int(time.time()*1000) if now_ms is None else source_contract.integer(now_ms)
    telemetry, errors = input_health(source, now)
    directory = Path(db.execute('PRAGMA database_list').fetchone()[2]).parent
    size = sum(p.stat().st_size for p in directory.glob('range_profit_protection.sqlite3*'))
    try:
        with closing(readonly(source)) as src:
            source_contract.validate_source(src)
            if size > MAX_STORE_BYTES:
                errors.append('STORAGE_LIMIT: admissions and minute writes paused')
            elif telemetry['health'] == 'OK':
                ingest(db, src, registration, now)
        rows = db.execute('SELECT * FROM pairs WHERE control IS NULL OR (admission=\'ELIGIBLE\' AND (protected IS NULL OR parity_valid=0 OR error IS NOT NULL)) ORDER BY last_checked_ms,id LIMIT 6').fetchall()
        for row in rows:
            if time.monotonic()-start_wall > 40:
                errors.append('CYCLE_BUDGET: pending work retained')
                break
            try:
                if size > MAX_STORE_BYTES and row['admission'] == 'ELIGIBLE':
                    continue
                # Each bounded source snapshot gets its own deadline; public
                # HTTP time in an earlier pair must not expire later reads.
                with closing(readonly(source)) as src:
                    source_contract.validate_source(src)
                    update_pair(db, src, row, now, fetcher)
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
                marker = type(exc).__name__+':'+str(exc)[:160]
                with db:
                    db.execute('UPDATE pairs SET error=?,last_checked_ms=? WHERE id=?', (marker, now, row['id']))
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        errors.append(type(exc).__name__+':'+str(exc)[:180])
    return status(db, registration, now, telemetry, errors)


def write_status(directory, value):
    path = Path(directory)/'status.json'
    temp = path.with_suffix('.json.tmp')
    temp.write_text(canonical(value), encoding='utf-8')
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--init-only', action='store_true')
    args = parser.parse_args()
    db, registration = open_store(args.data_dir, args.source)
    try:
        if args.init_only:
            now = int(time.time()*1000)
            health, errors = input_health(args.source, now)
            value = status(db, registration, now, health, errors)
        else:
            value = cycle(db, registration, args.source)
        write_status(args.data_dir, value)
        print(canonical({'health': value['health'], 'counts': value['counts'], 'errors': value['errors']}))
    finally:
        db.close()


if __name__ == '__main__':
    main()
