"""Prospective SQZ coverage and liquidity observations; no position creation."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import shutil
import sqlite3
import time
import zlib
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

from trading_bot.config import load_config
from trading_bot.data_provider.market_data import MarketDataProvider
from trading_bot.market_regime_detector import MarketRegimeDetector
from trading_bot.models import Candle, TradingMode
from trading_bot.research.early_paper import early_decisions, is_early
from trading_bot.research.mainnet_paper import PublicDataClient, PUBLIC_URL, canonical, digest, gate_decisions
from trading_bot.strategy_engine.order_flow import OrderFlowAnnotator
from trading_bot.strategy_engine.relative_strength import annotate_relative_strength
from trading_bot.strategy_engine.squeeze_breakout import SqueezeBreakoutStrategy

LOG = logging.getLogger('candidate_observer')
INTERVALS = {'15m': 900000, '1h': 3600000, '4h': 14400000}
VERSION = 'sqz-candidate-observer-v1'


def closed_frame(raw, interval, cutoff_ms):
    """Use one cutoff for the entire scan; reject stale, gapped or short history."""
    step = INTERVALS[interval]
    bars = [Candle.from_binance(r) for r in raw if int(r[6]) < cutoff_ms][-499:]
    expected = cutoff_ms // step * step - 1
    if len(bars) != 499 or bars[-1].close_time != expected:
        raise ValueError('INSUFFICIENT_OR_STALE_CANDLES:' + interval)
    for i, bar in enumerate(bars):
        if (bar.open_time % step or bar.close_time != bar.open_time + step - 1 or
                (i and bar.open_time != bars[i-1].open_time + step)):
            raise ValueError('CANDLE_GAP_OR_ALIGNMENT:' + interval)
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if (not all(v.is_finite() for v in values) or min(values[:4]) <= 0 or
                bar.volume < 0 or bar.low > min(bar.open, bar.close) or
                bar.high < max(bar.open, bar.close)):
            raise ValueError('INVALID_OHLCV:' + interval)
    return bars


def liquidity_failures(metrics, config):
    checks = [('VOLUME', metrics.quote_volume_24h, config.min_24h_quote_volume_usdt, False),
              ('SPREAD', metrics.spread_bps, config.max_spread_bps, True),
              ('DEPTH5', metrics.top_book_liquidity_usdt, config.min_order_book_top_liquidity_usdt, False)]
    return [name for name, value, threshold, upper in checks
            if not value.is_finite() or value < 0 or (value > threshold if upper else value < threshold)]


def book_impact(book, direction, entry, stop, risk_usdt=2):
    """Walk one side for base quantity risk/stop distance. Snapshot estimate only.

    A partial book produces no full-size VWAP. Range sums explicitly say whether
    the returned levels cover their outer boundary. Fees/lot rounding are excluded.
    """
    entry, stop, risk = float(entry), float(stop), float(risk_usdt)
    if (direction not in ('LONG', 'SHORT') or not all(map(math.isfinite, (entry, stop, risk)))
            or min(entry, stop, risk) <= 0 or
            (entry <= stop if direction == 'LONG' else entry >= stop)):
        raise ValueError('Invalid direction, stop or sizing inputs')
    sides = {}
    for side in ('bids', 'asks'):
        levels = [(float(p), float(q)) for p, q in book[side]]
        if not levels or any(not math.isfinite(p) or not math.isfinite(q) or min(p, q) <= 0 for p, q in levels):
            raise ValueError('Invalid book levels')
        if any((levels[i][0] >= levels[i-1][0] if side == 'bids' else levels[i][0] <= levels[i-1][0])
               for i in range(1, len(levels))):
            raise ValueError('Unsorted or duplicate book levels')
        sides[side] = levels
    bid, ask = sides['bids'][0][0], sides['asks'][0][0]
    if bid >= ask:
        raise ValueError('Crossed book')
    mid = (bid + ask) / 2
    selected = 'asks' if direction == 'LONG' else 'bids'
    quantity = risk / abs(entry-stop)
    remaining, cost = quantity, 0.0
    for price, available in sides[selected]:
        filled = min(remaining, available)
        cost += filled * price
        remaining -= filled
        if remaining <= quantity * 1e-12:
            remaining = 0.0
            break
    complete = remaining == 0
    vwap = cost / quantity if complete else None
    sign = 1 if direction == 'LONG' else -1
    best = ask if direction == 'LONG' else bid
    ranges = {}
    for side, levels in sides.items():
        distance = lambda p: abs(p/mid-1)*10000
        ranges[side] = {str(bps): {
            'visible_notional_usdt': sum(p*q for p, q in levels if distance(p) <= bps),
            'boundary_covered': distance(levels[-1][0]) >= bps,
        } for bps in (5, 10, 20)}
    return {'status': 'SNAPSHOT_FILLED' if complete else 'INSUFFICIENT_VISIBLE_DEPTH',
            'side': selected, 'risk_usdt_before_costs': risk, 'base_quantity': quantity,
            'signal_notional_usdt': quantity*entry, 'unfilled_quantity': remaining,
            'vwap': vwap, 'vwap_vs_mid_bps': sign*(vwap/mid-1)*10000 if complete else None,
            'walk_from_best_bps': sign*(vwap/best-1)*10000 if complete else None,
            'vwap_vs_signal_bps': sign*(vwap/entry-1)*10000 if complete else None,
            'spread_bps': (ask-bid)/mid*10000,
            'top5_both_sides_usdt': sum(p*q for levels in sides.values() for p, q in levels[:5]),
            'visible_ranges': ranges,
            'scope': 'Snapshot only; no latency/fill guarantee, fees, funding or lot/min-notional rounding'}


class ObserverStore:
    def __init__(self, directory, manifest):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory/'candidate_observer.sqlite3', timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, started_ms INTEGER, payload TEXT);
            CREATE TABLE IF NOT EXISTS coverage(id INTEGER PRIMARY KEY, scan_id INTEGER, symbol TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, first_ms INTEGER);
            CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, source_id TEXT,
                observed_ms INTEGER, payload_sha256 TEXT, payload_zlib BLOB);
            CREATE INDEX IF NOT EXISTS observations_source ON observations(source_id,id);
        ''')
        old = self.db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
        if old and old[0] != canonical(manifest):
            self.db.close()
            raise ValueError('Frozen observer changed: use a new data directory')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('manifest',?)", (canonical(manifest),))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('started_ms',?)", (str(int(time.time()*1000)),))

    def storage_ok(self):
        return (shutil.disk_usage(self.directory).free > 512*1024**2 and
                sum(p.stat().st_size for p in self.directory.glob('candidate_observer.sqlite3*')) < 1024**3)

    def coverage(self, scan_id, symbol, payload):
        with self.db:
            self.db.execute('INSERT INTO coverage(scan_id,symbol,payload) VALUES(?,?,?)',
                            (scan_id, symbol, canonical(payload)))

    def observe(self, signal, payload):
        m = signal.metadata
        source = f"SQZ:{signal.symbol}:{signal.direction.value}:{m['source_hour_close_time']}"
        payload = {**payload, 'source_id': source}
        raw = canonical(payload).encode()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO sources VALUES(?,?)', (source, payload['observed_ms']))
            self.db.execute('INSERT INTO observations(source_id,observed_ms,payload_sha256,payload_zlib) VALUES(?,?,?,?)',
                            (source, payload['observed_ms'], hashlib.sha256(raw).hexdigest(), zlib.compress(raw)))

    def status(self, scan):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('scan',?)", (canonical(scan),))
        result = {'execution': 'OBSERVATION_ONLY', 'order_capability': False, 'scan': scan,
                  'sources': self.db.execute('SELECT count(*) FROM sources').fetchone()[0],
                  'observations': self.db.execute('SELECT count(*) FROM observations').fetchone()[0]}
        tmp = self.directory/'status.tmp'
        tmp.write_text(json.dumps(result, indent=2), encoding='utf-8')
        tmp.replace(self.directory/'status.json')


class CandidateObserver:
    def __init__(self, config, settings, store):
        if config.mode != TradingMode.PAPER_TRADING or config.safety.enable_mainnet_live:
            raise ValueError('Paper-only configuration required')
        self.config, self.settings, self.store = config, settings, store
        self.client = PublicDataClient()
        self.market = MarketDataProvider(self.client)
        self.strategy = SqueezeBreakoutStrategy(config.strategy, MarketRegimeDetector(config.strategy))
        self.legacy = OrderFlowAnnotator(config.edge_filters, legacy_liquidity=True)
        self.corrected = OrderFlowAnnotator(config.edge_filters)
        self.gate_config = replace(config.strategy, p8_shadow_enabled=True, p8_shadow_cohort=settings['cohort'])

    async def frame(self, symbol, tf, cutoff):
        raw = await self.client.klines(symbol, tf, limit=500, end_time=cutoff-1)
        return closed_frame(raw, tf, cutoff)

    async def candidate(self, signal, frames, btc, cutoff):
        observed = int(time.time()*1000)
        signal = replace(signal, metadata={**signal.metadata, 'observed_ms': observed,
                         'source_hour_close_time': frames['1h'][-1].close_time})
        payload = {'observed_ms': observed, 'cutoff_ms': cutoff, 'signal': asdict(signal),
                   'frames': {tf: [asdict(c) for c in bars] for tf, bars in frames.items()},
                   'btc_4h': [asdict(c) for c in btc], 'errors': [],
                   'liquidity_failures': None, 'gates': None, 'impact': None}
        # Once the unchanged source exists, preserve it even if enrichment fails.
        try:
            metrics = await self.market.symbol_metrics(signal.symbol)
            payload['metrics'] = asdict(metrics)
            payload['liquidity_failures'] = liquidity_failures(metrics, self.config.universe)
            legacy = self.legacy.annotate(frames['15m'], signal.direction, metrics).to_metadata()
            corrected = self.corrected.annotate(frames['15m'], signal.direction, metrics).to_metadata()
            rs = annotate_relative_strength(frames['4h'], signal.direction, btc[-1].close/btc[-2].close-1).to_metadata()
            signal = replace(signal, metadata={**signal.metadata, 'order_flow': legacy,
                             'p8_order_flow': corrected, 'relative_strength': rs})
            payload['signal'] = asdict(signal)
            payload['gates'] = {'original': gate_decisions(signal, self.gate_config),
                                'early': early_decisions(signal, self.gate_config) if is_early(signal) else None}
        except Exception as exc:
            payload['errors'].append({'stage': 'METRICS_OR_GATES', 'error': str(exc)[:500]})
        try:
            depth = await self.client.depth(signal.symbol, limit=100)
            payload['depth100'] = depth
            payload['impact'] = book_impact(depth, signal.direction.value, signal.entry_price,
                                            signal.stop_loss, self.settings['risk_usdt_for_depth'])
        except Exception as exc:
            payload['errors'].append({'stage': 'DEPTH_IMPACT', 'error': str(exc)[:500]})
        payload['finished_ms'] = int(time.time()*1000)
        payload['public_requests'] = dict(self.client.evidence)
        payload['status'] = 'PARTIAL' if payload['errors'] else 'OK'
        self.store.observe(signal, payload)
        return payload['status']

    async def scan(self):
        cutoff = int(time.time()*1000)
        counts = {'cutoff_ms': cutoff, 'scanned': 0, 'candidates': 0, 'errors': 0, 'not_tradable': 0}
        if not self.store.storage_ok():
            self.store.status({**counts, 'status': 'PAUSED_STORAGE_LIMIT'})
            return
        self.client.evidence.clear()
        info = await self.client.exchange_info()
        tradable = {r['symbol'] for r in info['symbols'] if r.get('status') == 'TRADING'
                    and r.get('contractType') == 'PERPETUAL' and r.get('quoteAsset') == 'USDT'}
        # Every scan persists the registered list, current membership and exclusions.
        with self.store.db:
            scan_id = self.store.db.execute('INSERT INTO scans(started_ms,payload) VALUES(?,?)',
                (cutoff, canonical({'symbols': self.settings['symbols'], 'tradable': sorted(tradable),
                                   'exchange_info_received_ms': int(time.time()*1000)}))).lastrowid
        btc = await self.frame('BTCUSDT', '4h', cutoff)
        for symbol in self.settings['symbols']:
            if not self.store.storage_ok():
                self.store.status({**counts, 'status': 'PAUSED_STORAGE_LIMIT'})
                return
            if symbol not in tradable:
                counts['not_tradable'] += 1
                self.store.coverage(scan_id, symbol, {'status': 'NOT_TRADABLE'})
                continue
            self.client.evidence.clear()
            try:
                frames = {tf: await self.frame(symbol, tf, cutoff) for tf in INTERVALS}
                # SQZ.generate uses OHLCV only; metrics are intentionally collected AFTER source generation.
                signal = self.strategy.generate(symbol, frames['15m'], frames['1h'], frames['4h'], None)
                counts['scanned'] += 1
                coverage = {'status': 'NO_SIGNAL', 'frame_sha256': digest(
                                {tf: [asdict(c) for c in bars] for tf, bars in frames.items()}),
                            'closed_through': {tf: bars[-1].close_time for tf, bars in frames.items()}}
                if signal is not None:
                    counts['candidates'] += 1
                    state = await self.candidate(signal, frames, btc, cutoff)
                    counts['errors'] += int(state != 'OK')
                    coverage['status'] = 'CANDIDATE_' + state
                self.store.coverage(scan_id, symbol, coverage)
            except Exception as exc:
                counts['errors'] += 1
                self.store.coverage(scan_id, symbol, {'status': 'ERROR', 'error': str(exc)[:500]})
                LOG.warning('Symbol %s: %s', symbol, exc)
        self.client.evidence.clear()
        result = {**counts, 'finished_ms': int(time.time()*1000), 'status': 'PARTIAL' if counts['errors'] else 'OK'}
        self.store.status(result)
        LOG.info('Scan complete %s', canonical(result))

    async def run(self, once=False):
        try:
            while True:
                started = time.monotonic()
                try:
                    await self.scan()
                except Exception as exc:
                    self.store.status({'at_ms': int(time.time()*1000), 'status': 'ERROR', 'error': str(exc)[:500]})
                    LOG.exception('Scan failed')
                    if once:
                        raise
                if once:
                    return
                await asyncio.sleep(max(30, self.settings['scan_interval_sec']-(time.monotonic()-started)))
        finally:
            await self.client.close()
            self.store.db.close()


def validate_settings(settings):
    if (settings.get('experiment') != VERSION or settings.get('execution_mode') != 'OBSERVATION_ONLY'
            or settings.get('market_data_base_url') != PUBLIC_URL or not settings.get('cohort')):
        raise ValueError('Explicit observation-only settings required')
    symbols = settings['symbols']
    if not symbols or len(symbols) > 80 or len(set(symbols)) != len(symbols):
        raise ValueError('Invalid frozen universe')
    if settings.get('scan_interval_sec') != 900 or settings.get('risk_usdt_for_depth') != 2:
        raise ValueError('Version 1 requires a 900s scan and 2 USDT sizing probe')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--settings', required=True)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    settings = json.loads(Path(args.settings).read_text(encoding='utf-8'))
    validate_settings(settings)
    path = Path(args.config).resolve()
    config = load_config(path, path.parent/'NO_CREDENTIALS.env')
    if config.mode != TradingMode.PAPER_TRADING or config.safety.enable_mainnet_live:
        raise ValueError('Paper-only configuration required')
    package = Path(__file__).resolve().parents[1]
    manifest = {'settings': settings, 'config_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'code_sha256': digest({str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                                       for p in sorted(package.rglob('*.py'))})}
    directory = Path(args.data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # Match the existing research single-writer lock, including Windows local probes.
    with (directory/'process.lock').open('a+b') as lock:
        import os
        if os.name == 'posix':
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        store = ObserverStore(directory, manifest)
        asyncio.run(CandidateObserver(config, settings, store).run(args.once))


if __name__ == '__main__':
    main()
