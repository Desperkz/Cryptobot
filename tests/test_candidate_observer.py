import hashlib
import json
import zlib
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from test_early_paper import candidate, config
from trading_bot.research.candidate_observer import (
    CandidateObserver, ObserverStore, book_impact, closed_frame, liquidity_failures, validate_settings)


def settings():
    return json.loads(open('candidate_observer_settings.json', encoding='utf-8').read())


def book():
    return {'bids': [['99', '1'], ['98', '10']], 'asks': [['101', '1'], ['102', '10']]}


@pytest.mark.parametrize('direction,stop,expected', [('LONG', 99, 101.5), ('SHORT', 101, 98.5)])
def test_impact_walks_correct_side_and_size(direction, stop, expected):
    r = book_impact(book(), direction, 100, stop)
    assert r['base_quantity'] == 2
    assert r['vwap'] == expected
    assert r['vwap_vs_mid_bps'] == pytest.approx(150)
    assert r['status'] == 'SNAPSHOT_FILLED'
    assert r['visible_ranges']['asks']['10']['visible_notional_usdt'] == 0
    assert r['visible_ranges']['asks']['10']['boundary_covered']


def test_partial_depth_does_not_claim_a_full_size_price():
    r = book_impact(book(), 'LONG', 100, 99.99)
    assert r['status'] == 'INSUFFICIENT_VISIBLE_DEPTH'
    assert r['vwap'] is None and r['walk_from_best_bps'] is None
    assert r['unfilled_quantity'] > 180
    r = book_impact({'bids': [['99.99', '10']], 'asks': [['100.01', '10']]}, 'LONG', 100, 90)
    assert not r['visible_ranges']['asks']['10']['boundary_covered']


@pytest.mark.parametrize('direction,entry,stop', [('LONG',100,101),('SHORT',100,99),
    ('LONG',100,100),('LONG','NaN',90),('NONE',100,90)])
def test_bad_sizing_rejected(direction, entry, stop):
    with pytest.raises(ValueError):
        book_impact(book(), direction, entry, stop)


def test_crossed_and_unsorted_books_rejected():
    for bids in [[['102','1']], [['99','1'],['100','1']], [['99','NaN']]]:
        with pytest.raises(ValueError):
            book_impact({**book(), 'bids': bids}, 'LONG', 100, 90)


def raw_frame():
    return [[i*900000, '100','110','90','100','2',(i+1)*900000-1,'200'] for i in range(500)]


def test_closed_frame_has_no_future_bars_and_rejects_gaps_stale_short():
    raw = raw_frame()
    cutoff = 499*900000+10
    bars = closed_frame(raw, '15m', cutoff)
    assert len(bars) == 499 and bars[-1].close_time == 499*900000-1
    for damaged in [raw[:498], raw[:10]+raw[11:], raw[1:]]:
        with pytest.raises(ValueError):
            closed_frame(damaged, '15m', cutoff)


def test_all_liquidity_failures_are_kept():
    metrics = SimpleNamespace(quote_volume_24h=Decimal('1'), spread_bps=Decimal('9'),
                              top_book_liquidity_usdt=Decimal('NaN'))
    assert liquidity_failures(metrics, config().universe) == ['VOLUME','SPREAD','DEPTH5']


def test_store_freeze_hour_grouping_and_payload_integrity(tmp_path):
    store = ObserverStore(tmp_path, {'version': 'test'})
    s = candidate()
    store.observe(s, {'observed_ms': 3661000})
    store.observe(s, {'observed_ms': 4561000})
    assert store.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
    assert store.db.execute('SELECT count(*) FROM observations').fetchone()[0] == 2
    h, b = store.db.execute('SELECT payload_sha256,payload_zlib FROM observations LIMIT 1').fetchone()
    assert hashlib.sha256(zlib.decompress(b)).hexdigest() == h
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='positions'").fetchone()
    store.db.close()
    store = ObserverStore(tmp_path, {'version': 'test'})
    assert store.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
    store.db.close()
    with pytest.raises(ValueError, match='Frozen'):
        ObserverStore(tmp_path, {'version': 'other'})


@pytest.mark.asyncio
@pytest.mark.parametrize('metrics_fail', [False, True])
async def test_pre_liquidity_candidate_survives_rejection_and_enrichment_failure(tmp_path, metrics_fail):
    s = candidate()
    store = ObserverStore(tmp_path, {'test': True})
    observer = CandidateObserver(config(), {**settings(), 'symbols':['BTCUSDT']}, store)
    real_client = observer.client
    bars = closed_frame(raw_frame(), '15m', 499*900000+10)
    calls = []
    class Feed:
        evidence = {}
        async def exchange_info(self):
            return {'symbols':[{'symbol':'BTCUSDT','status':'TRADING','contractType':'PERPETUAL','quoteAsset':'USDT'}]}
        async def depth(self, symbol, limit):
            return book()
    class Market:
        async def symbol_metrics(self, symbol):
            calls.append('metrics')
            if metrics_fail:
                raise RuntimeError('fixture unavailable')
            from trading_bot.models import MarketMetrics
            return MarketMetrics(symbol=symbol, quote_volume_24h=Decimal('1'),spread_bps=Decimal('9'),
                                 top_book_liquidity_usdt=Decimal('1'))
    class Strategy:
        def generate(self, *args):
            calls.append('generate')
            assert args[-1] is None
            return s
    async def frame(*args):
        return bars
    observer.client = Feed()
    observer.market = Market()
    observer.strategy = Strategy()
    observer.frame = frame
    try:
        await observer.scan()
        assert calls == ['generate','metrics']
        payload = json.loads(zlib.decompress(store.db.execute('SELECT payload_zlib FROM observations').fetchone()[0]))
        assert payload['impact']['status'] == 'SNAPSHOT_FILLED'
        if metrics_fail:
            assert payload['status'] == 'PARTIAL' and payload['liquidity_failures'] is None
            assert payload['errors'][0]['stage'] == 'METRICS_OR_GATES'
        else:
            assert payload['status'] == 'OK'
            assert payload['liquidity_failures'] == ['VOLUME','SPREAD','DEPTH5']
            assert payload['gates']['early'] is not None
        assert store.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
    finally:
        await real_client.close()
        store.db.close()


def test_settings_only_allow_registered_observer():
    good = settings()
    validate_settings(good)
    assert len(good['symbols']) == len(set(good['symbols'])) == 62
    for change in [{'execution_mode':'LOCAL_PAPER_ONLY'},{'scan_interval_sec':5},
                   {'symbols':['BTCUSDT','BTCUSDT']},{'risk_usdt_for_depth':200}]:
        with pytest.raises(ValueError):
            validate_settings({**good, **change})


def test_report_uses_first_source_and_retains_unknowns(tmp_path):
    import runpy
    from dataclasses import asdict
    report = runpy.run_path('scripts/candidate_observer_report.py')['report']
    store = ObserverStore(tmp_path, {'version':'test'})
    s = candidate()
    payload = {'observed_ms':3661000, 'signal':asdict(s), 'errors':[], 'status':'PARTIAL',
               'liquidity_failures':None, 'gates':None, 'impact':None}
    store.observe(s, payload)
    store.observe(s, {**payload, 'observed_ms':4561000, 'liquidity_failures':[]})
    store.db.close()
    r = report(tmp_path/'candidate_observer.sqlite3')
    assert r['sources'] == 1 and r['observations'] == 2
    assert r['first_source_counts'] == {'liquidity_unknown':1,'early_sources':1,'early_gate_unknown':1}
