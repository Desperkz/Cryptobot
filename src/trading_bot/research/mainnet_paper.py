"""Credential-free prospective SQZ comparison on mainnet PUBLIC data.

This process never constructs TradingBot or OrderManager. Its only HTTP client
rejects every signed request and all methods/paths outside a public GET allowlist.
All positions live in a dedicated research SQLite database.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
import sqlite3
import time
import zlib
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

from trading_bot.bot import (_order_flow_entry_rejection_reason,
    _controlled_paper_sqz_override, _squeeze_order_flow_measurement_override,
    _p8_shadow_variants, _sqz_gate_cohort_shadow_variants,
    _squeeze_context_gate_rejection, _is_strong_clean_squeeze_release)
from trading_bot.config import load_config
from trading_bot.data_provider.binance_usdm import BinanceUSDMClient
from trading_bot.data_provider.market_data import MarketDataProvider
from trading_bot.market_regime_detector import MarketRegimeDetector
from trading_bot.models import TradingMode
from trading_bot.strategy_engine.order_flow import OrderFlowAnnotator
from trading_bot.strategy_engine.relative_strength import annotate_relative_strength
from trading_bot.strategy_engine.squeeze_breakout import SqueezeBreakoutStrategy
from trading_bot.research.paper_execution import simulate

PUBLIC_URL = 'https://fapi.binance.com'
PUBLIC_PATHS = frozenset({'/fapi/v1/exchangeInfo', '/fapi/v1/klines',
    '/fapi/v1/ticker/bookTicker', '/fapi/v1/ticker/24hr', '/fapi/v1/depth',
    '/fapi/v1/fundingRate', '/fapi/v1/openInterest', '/fapi/v1/aggTrades',
    '/futures/data/openInterestHist'})
MINUTE = 60000
ARMS = ('BASELINE_2R', 'CURRENT_GATE_2R', 'CURRENT_GATE_PROFILE', 'P8_OBSERVE_PROFILE')
POLICIES = ('FIRST_OBSERVATION', 'FIRST_ADMISSIBLE')
log = logging.getLogger('mainnet_paper_lab')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class PublicDataClient(BinanceUSDMClient):
    def __init__(self):
        super().__init__(base_url=PUBLIC_URL, timeout_sec=15, max_retries=2)
        self.evidence = {}

    async def _request(self, method, path, params=None, signed=False):
        if method != 'GET' or signed or path not in PUBLIC_PATHS or self.base_url != PUBLIC_URL:
            raise PermissionError('Research client permits only allowlisted unsigned mainnet GET data requests')
        # Even an accidentally assigned credential must not reach public requests.
        if self.api_key is not None or self.api_secret is not None:
            raise PermissionError('Credentials are forbidden in the research client')
        key = path + ':' + canonical(params or {})
        try:
            result = await super()._request(method, path, params, signed=False)
        except Exception as exc:
            self.evidence[key] = {'at_ms': int(time.time()*1000), 'error': str(exc)[:500]}
            raise
        self.evidence[key] = {'at_ms': int(time.time()*1000), 'data': result}
        return result


def gate_decisions(signal, config):
    rejection = _order_flow_entry_rejection_reason(signal, config)
    current = (rejection is None or _controlled_paper_sqz_override(signal, rejection, config) is not None
               or _squeeze_order_flow_measurement_override(signal, rejection, config) is not None)
    _, failures, safety = _sqz_gate_cohort_shadow_variants(
        signal, replace(config, squeeze_gate_cohort_shadow_enabled=True))
    variants, p8 = _p8_shadow_variants(signal, config)
    observe = 'observe' in p8 and p8['observe']['rejection'] is None
    m = signal.metadata; flow = m.get('p8_order_flow', {}); flags = set(flow.get('risk_flags', []))
    corrected = replace(signal, metadata={**m, 'order_flow': flow})
    structural = sorted(flags & {'liquidation_cascade', 'structure_break_against', 'adverse_liquidity_nearby', 'absorption_against'})
    observe_config = replace(config, squeeze_context_gate_enabled=True, squeeze_context_gate_require_4h_squeeze_or_trend=True, squeeze_context_gate_blocked_regimes=['RANGE'])
    p8_vector = {
        'structural_flags': structural,
        'context': _squeeze_context_gate_rejection(corrected, observe_config),
        'relative_strength': m.get('relative_strength', {}).get('alignment') == 'aligned',
        'retest_or_strong': bool(m.get('squeeze_retest_confirmed')) or _is_strong_clean_squeeze_release(corrected,
            alignment=str(flow.get('alignment', 'mixed')), score=Decimal(str(flow.get('score', 0))), risk_flags=flags, observe_mode=True),
        'structure_confirmation': 'structure_break_aligned' in flow.get('reasons', []),
    }
    return {'allowed': dict(zip(ARMS, [True, current, current, observe])),
            'current_first_rejection': rejection, 'current_all_gate_failures': failures,
            'current_structural_flags': safety, 'p8_all_gates': p8_vector, 'p8_decision': p8,
            'p8_profiles': [v.metadata.get('conditional_profile') for v in variants]}


class Store:
    def __init__(self, directory, manifest):
        self.directory = Path(directory).resolve(); self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'mainnet_paper_lab.sqlite3'
        self.db = sqlite3.connect(self.path, timeout=15); self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,first_ms INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY,source_id TEXT,observed_ms INTEGER,
            payload_sha256 TEXT,payload_zlib BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS coverage(id INTEGER PRIMARY KEY,at_ms INTEGER,symbol TEXT,payload TEXT);
          CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY,source_id TEXT,policy TEXT,arm TEXT,
            symbol TEXT,direction TEXT,entry_ms INTEGER,status TEXT,plan TEXT,result TEXT,
            UNIQUE(source_id,policy,arm));
          CREATE INDEX IF NOT EXISTS positions_active ON positions(status,symbol);
          CREATE TABLE IF NOT EXISTS execution_candles(symbol TEXT,open_ms INTEGER,payload TEXT,
            PRIMARY KEY(symbol,open_ms));
        ''')
        old = self.db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
        if old and old['value'] != canonical(manifest):
            self.db.close()
            raise ValueError('Frozen experiment changed: use a new cohort and a new data directory')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('manifest',?)", (canonical(manifest),))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('started_ms',?)", (str(int(time.time()*1000)),))

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, canonical(value)))

    def coverage(self, symbol, payload):
        with self.db:
            self.db.execute('INSERT INTO coverage(at_ms,symbol,payload) VALUES(?,?,?)', (int(time.time()*1000),symbol,canonical(payload)))

    def admit(self, signal, observed_ms, decision, evidence, profiles, cohort):
        source_id = f"SQZ:{signal.symbol}:{signal.direction.value}:{signal.metadata['signal_bar_close_time']}"
        outcomes = []
        with self.db:
            first = self.db.execute('INSERT OR IGNORE INTO sources VALUES(?,?)', (source_id, observed_ms)).rowcount == 1
            for policy in POLICIES:
                if policy == 'FIRST_OBSERVATION' and not first:
                    continue
                for arm in ARMS:
                    if not decision['allowed'][arm]:
                        outcomes.append({'policy':policy,'arm':arm,'result':'GATE_REJECTED'}); continue
                    if self.db.execute('SELECT 1 FROM positions WHERE source_id=? AND policy=? AND arm=?', (source_id,policy,arm)).fetchone():
                        outcomes.append({'policy':policy,'arm':arm,'result':'ALREADY_RECORDED'}); continue
                    if self.db.execute("SELECT 1 FROM positions WHERE symbol=? AND policy=? AND arm=? AND status IN ('PENDING','OPEN')", (signal.symbol,policy,arm)).fetchone():
                        outcomes.append({'policy':policy,'arm':arm,'result':'SAME_SYMBOL_ACTIVE'}); continue
                    if self.db.execute("SELECT count(*) FROM positions WHERE status IN ('PENDING','OPEN')").fetchone()[0] >= 128:
                        outcomes.append({'policy':policy,'arm':arm,'result':'RESEARCH_CAPACITY_128'}); continue
                    entry_ms = (observed_ms // MINUTE + 1) * MINUTE
                    plan = {'venue':PUBLIC_URL, 'cohort':cohort, 'source_id':source_id,
                        'stop':str(signal.stop_loss), 'signal_entry':str(signal.entry_price),
                        'targets':profiles[arm], 'risk_usdt':2.0, 'max_minutes':1440,
                        'funding_rate':signal.metadata.get('order_flow',{}).get('funding_rate'),
                        'observed_ms':observed_ms,'exit_model':'minute_ohlc_conservative_v1'}
                    self.db.execute('INSERT INTO positions(source_id,policy,arm,symbol,direction,entry_ms,status,plan) VALUES(?,?,?,?,?,?,?,?)',
                        (source_id,policy,arm,signal.symbol,signal.direction.value,entry_ms,'PENDING',canonical(plan)))
                    outcomes.append({'policy':policy,'arm':arm,'result':'PENDING_NEXT_MINUTE','entry_ms':entry_ms})
            payload = {'venue':PUBLIC_URL,'cohort':cohort,'source_id':source_id,'observed_ms':observed_ms,
                       'signal':asdict(signal),'decisions':decision,'admission_outcomes':outcomes,'evidence':evidence}
            raw = canonical(payload).encode()
            self.db.execute('INSERT INTO observations(source_id,observed_ms,payload_sha256,payload_zlib) VALUES(?,?,?,?)',
                            (source_id,observed_ms,hashlib.sha256(raw).hexdigest(),zlib.compress(raw)))
        return outcomes

    def status(self):
        return {'venue':PUBLIC_URL,'execution':'LOCAL_PAPER_ONLY','order_capability':False,
            'at_ms':int(time.time()*1000),
            'meta':{r['key']:json.loads(r['value']) for r in self.db.execute('SELECT * FROM meta')},
            'observations':self.db.execute('SELECT count(*) FROM observations').fetchone()[0],
            'sources':self.db.execute('SELECT count(*) FROM sources').fetchone()[0],
            'positions':[dict(r) for r in self.db.execute('SELECT policy,arm,status,count(*) AS n FROM positions GROUP BY policy,arm,status')],
            'database_bytes':sum(p.stat().st_size for p in self.directory.glob('mainnet_paper_lab.sqlite3*'))}


class Lab:
    def __init__(self, config, symbols, store, cohort):
        if config.mode != TradingMode.PAPER_TRADING or config.safety.enable_mainnet_live:
            raise ValueError('Lab requires PAPER_TRADING and disabled mainnet live execution')
        self.config=config; self.symbols=symbols; self.store=store; self.cohort=cohort
        self.client=PublicDataClient(); self.market=MarketDataProvider(self.client)
        self.strategy=SqueezeBreakoutStrategy(config.strategy,MarketRegimeDetector(config.strategy))
        self.legacy=OrderFlowAnnotator(config.edge_filters,legacy_liquidity=True)
        self.corrected=OrderFlowAnnotator(config.edge_filters)
        self.gate_config=replace(config.strategy,p8_shadow_enabled=True,p8_shadow_cohort=cohort)
        self.profile=[(float(t.reward_risk),float(t.fraction),t.move_stop_to_breakeven,t.activate_trailing)
                      for t in config.trade_management.strategy_exit_profiles['SQUEEZE_BREAKOUT']]

    def storage_ok(self):
        return (shutil.disk_usage(self.store.directory).free > 512*1024**2 and
                sum(p.stat().st_size for p in self.store.directory.glob('mainnet_paper_lab.sqlite3*')) < 1024**3)

    async def scan(self):
        if not self.storage_ok():
            self.store.set('scan',{'at_ms':int(time.time()*1000),'status':'PAUSED_STORAGE_LIMIT'}); return
        start=int(time.time()*1000); counts={'scanned':0,'eligible':0,'signals':0,'oi_present':0,'errors':0}
        info=await self.client.exchange_info()
        tradable={r['symbol'] for r in info['symbols'] if r.get('status')=='TRADING' and r.get('contractType')=='PERPETUAL' and r.get('quoteAsset')=='USDT'}
        btc=await self.market.candles('BTCUSDT','4h',limit=500)
        btc_change=btc[-1].close/btc[-2].close-1 if len(btc)>1 else None
        for symbol in self.symbols:
            if symbol not in tradable:
                self.store.coverage(symbol,{'status':'NOT_TRADABLE'}); continue
            # Capture per-symbol public input evidence; monitoring has a separate client.
            self.client.evidence.clear()
            try:
                metrics=await self.market.symbol_metrics(symbol)
                counts['scanned']+=1; counts['oi_present']+=int(metrics.open_interest_change_pct is not None)
                oi_calls={k:v for k,v in self.client.evidence.items() if 'openInterestHist' in k}
                oi_reason = 'present' if metrics.open_interest_change_pct is not None else (
                    'request_failed' if any('error' in v for v in oi_calls.values()) else 'insufficient_or_invalid_history')
                quality=(metrics.quote_volume_24h>=self.config.universe.min_24h_quote_volume_usdt and
                         metrics.spread_bps<=self.config.universe.max_spread_bps and
                         metrics.top_book_liquidity_usdt>=self.config.universe.min_order_book_top_liquidity_usdt)
                self.store.coverage(symbol,{'status':'ELIGIBLE' if quality else 'LIQUIDITY_FILTER',
                    'metrics':asdict(metrics),'oi_state':oi_reason,'oi_requests':oi_calls})
                if not quality:continue
                counts['eligible']+=1
                frames={tf:await self.market.candles(symbol,tf,limit=500) for tf in ('15m','1h','4h')}
                signal=self.strategy.generate(symbol,frames['15m'],frames['1h'],frames['4h'],metrics)
                if signal is None:continue
                observed=int(time.time()*1000); counts['signals']+=1
                flow=self.legacy.annotate(frames['15m'],signal.direction,metrics).to_metadata()
                corrected=self.corrected.annotate(frames['15m'],signal.direction,metrics).to_metadata()
                rs=annotate_relative_strength(frames['4h'],signal.direction,btc_change).to_metadata()
                signal=replace(signal,metadata={**signal.metadata,'order_flow':flow,'p8_order_flow':corrected,
                    'relative_strength':rs,'venue':'BINANCE_USDM_MAINNET_PUBLIC','data_endpoint':PUBLIC_URL,
                    'research_cohort':self.cohort,'observed_ms':observed})
                assert signal.metadata['signal_bar_close_time']<=observed
                decisions=gate_decisions(signal,self.gate_config)
                closed_frames={tf:[asdict(c) for c in cs] for tf,cs in frames.items()}
                evidence={'closed_frames':closed_frames,'closed_frames_sha256':digest(closed_frames),
                    'btc_4h': [asdict(c) for c in btc], 'metrics':asdict(metrics),
                    'oi_state':oi_reason,'public_requests':dict(self.client.evidence)}
                rr=float(signal.metadata.get('rr',2.4))
                profile=[(min(x[0],rr),*x[1:]) for x in self.profile]
                profiles={arm:([(2.,1.,False,False)] if arm.endswith('_2R') else profile) for arm in ARMS}
                outcomes=self.store.admit(signal,observed,decisions,evidence,profiles,self.cohort)
                log.info('Signal %s %s arms=%s',symbol,signal.direction.value,canonical(outcomes))
            except Exception as exc:
                counts['errors']+=1; self.store.coverage(symbol,{'status':'ERROR','error':str(exc)[:500]})
                log.warning('Scan %s: %s',symbol,exc)
        self.store.set('scan',{'started_ms':start,'finished_ms':int(time.time()*1000),'status':'OK' if not counts['errors'] else 'PARTIAL',**counts})
        log.info('Scan complete %s',canonical(counts))

    async def monitor(self, client):
        rows=self.store.db.execute("SELECT * FROM positions WHERE status IN ('PENDING','OPEN') ORDER BY id").fetchall()
        by={}
        for row in rows:by.setdefault(row['symbol'],[]).append(row)
        now=int(time.time()*1000); errors=[]
        for symbol, positions in by.items():
            try:
                earliest=min(r['entry_ms'] for r in positions)
                # Resume from the FIRST missing minute, not the largest cached
                # timestamp. A partial API response must remain repairable.
                cursor=earliest
                for saved in self.store.db.execute('SELECT open_ms FROM execution_candles WHERE symbol=? AND open_ms>=? ORDER BY open_ms',(symbol,earliest)):
                    if saved[0]!=cursor:break
                    cursor+=MINUTE
                end=min(now//MINUTE*MINUTE,max(r['entry_ms']+1440*MINUTE for r in positions))
                while cursor<end:
                    raw=await client.klines(symbol,'1m',limit=1000,start_time=cursor,end_time=end-1)
                    closed=[b for b in raw if int(b[6])<now and cursor<=int(b[0])<end]
                    if not closed:break
                    with self.store.db:
                        self.store.db.executemany('INSERT OR IGNORE INTO execution_candles VALUES(?,?,?)',[(symbol,int(b[0]),canonical(b)) for b in closed])
                    cursor=int(closed[-1][0])+MINUTE
                for row in positions:
                    bars=[json.loads(r[0]) for r in self.store.db.execute('SELECT payload FROM execution_candles WHERE symbol=? AND open_ms>=? AND open_ms<? ORDER BY open_ms',
                        (symbol,row['entry_ms'],row['entry_ms']+1440*MINUTE))]
                    if not bars:continue
                    if any(int(b[0])!=row['entry_ms']+i*MINUTE for i,b in enumerate(bars)):
                        errors.append({'position':row['id'],'error':'EXECUTION_CANDLE_GAP'}); continue
                    plan=json.loads(row['plan'])
                    result=simulate(bars,float(plan['stop']),row['direction'],plan['targets'],complete=len(bars)==1440,funding_rate=plan['funding_rate'])
                    if 'R' in result:result['model_pnl_usdt']=result['R']*plan['risk_usdt']
                    with self.store.db:self.store.db.execute('UPDATE positions SET status=?,result=? WHERE id=?',(result['status'],canonical(result),row['id']))
            except Exception as exc:errors.append({'symbol':symbol,'error':str(exc)[:500]})
        # Execution evidence cache must not retain every HTTP reply indefinitely.
        client.evidence.clear()
        self.store.set('monitor',{'at_ms':now,'status':'OK' if not errors else 'DEGRADED','errors':errors,'active_checked':len(rows)})
        status=self.store.status();tmp=self.store.directory/'status.tmp';tmp.write_text(json.dumps(status,indent=2),encoding='utf-8');tmp.replace(self.store.directory/'status.json')

    async def run(self, once=False):
        execution_client=PublicDataClient()
        try:
            if once:
                await self.scan();await self.monitor(execution_client);return
            async def scans():
                while True:
                    started=time.monotonic()
                    try:await self.scan()
                    except Exception as exc:
                        self.store.set('scan',{'at_ms':int(time.time()*1000),'status':'ERROR','error':str(exc)[:500]});log.exception('Scan failed')
                    await asyncio.sleep(max(5,300-(time.monotonic()-started)))
            async def monitors():
                while True:
                    try:await self.monitor(execution_client)
                    except Exception:log.exception('Monitor failed')
                    await asyncio.sleep(30)
            await asyncio.gather(scans(),monitors())
        finally:
            await self.client.close();await execution_client.close();self.store.db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True);parser.add_argument('--settings',required=True)
    parser.add_argument('--data-dir',required=True);parser.add_argument('--once',action='store_true')
    args=parser.parse_args(); logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    config_path=Path(args.config).resolve(); settings=json.loads(Path(args.settings).read_text())
    if settings.get('execution_mode')!='LOCAL_PAPER_ONLY' or settings.get('market_data_base_url')!=PUBLIC_URL:
        raise ValueError('Settings must explicitly select public mainnet data and local paper execution')
    cfg=load_config(config_path,config_path.parent/'NO_CREDENTIALS.env')
    if cfg.mode!=TradingMode.PAPER_TRADING or cfg.safety.enable_mainnet_live:raise ValueError('Paper-only configuration required')
    symbols=settings['symbols'];cohort=settings['cohort']
    if not cohort or not symbols or len(symbols)>40 or len(set(symbols))!=len(symbols):raise ValueError('Invalid frozen cohort/universe')
    package=Path(__file__).resolve().parents[1]
    code_hash=digest({str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(package.rglob('*.py'))})
    manifest={'cohort':cohort,'symbols':symbols,'data_venue':PUBLIC_URL,'mode':'LOCAL_PAPER_ONLY',
        'code_sha256':code_hash,'config_sha256':hashlib.sha256(config_path.read_bytes()).hexdigest(),
        'settings_sha256':digest(settings),
        'scan_interval_sec':300,'monitor_interval_sec':30,'arms':ARMS,'policies':POLICIES,
        'risk_usdt':2,'single_symbol_per_arm_policy':True,'capacity':128,
        'entry':'next_minute_open_after_observation_plus_5bps','fees_bps_each_side':4,
        'exit_slippage_bps':5,'funding':'entry_signed_rate_continuous_estimate_or_adverse_1bp_buffer',
        'maximum_holding_minutes':1440,'maximum_database_bytes':1024**3,
        'scope':'SQZ admission experiment, not a full production portfolio replay'}
    lock_directory=Path(args.data_dir).resolve();lock_directory.mkdir(parents=True,exist_ok=True)
    lock_file=(lock_directory/'process.lock').open('a+b')
    import os
    if os.name=='posix':
        import fcntl
        fcntl.flock(lock_file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    else:
        import msvcrt
        lock_file.seek(0);lock_file.write(b'0');lock_file.flush();lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(),msvcrt.LK_NBLCK,1)
    store=Store(args.data_dir,manifest)
    try:asyncio.run(Lab(cfg,symbols,store,cohort).run(args.once))
    finally:lock_file.close()


if __name__=='__main__':main()
