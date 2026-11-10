import json,sqlite3
import pytest
from trading_bot.research.market_evidence_collector import Store,MINUTE,SYMBOLS,event_time

AT=1800000000000
def trade(i=100,maker=False,at=AT,symbol='BTCUSDT'):
 return {'e':'aggTrade','E':at,'T':at,'s':symbol,'a':i,'p':'100','q':'2','m':maker}
def make(tmp_path):
 store=Store(tmp_path,AT);store.begin(AT);return store
def test_aggressor_sides_and_duplicate_ids(tmp_path):
 s=make(tmp_path)
 assert s.event(trade(),AT)
 assert not s.event(trade(),AT)
 s.event(trade(101,True),AT)
 s.flush(AT+1000)
 value=json.loads(s.db.execute('SELECT payload FROM segments').fetchone()[0])
 assert value['buy_quote']==value['sell_quote']==200
 assert value['messages']==2
 s.close(AT+2000)
def test_restart_retains_watermark_and_records_gap(tmp_path):
 s=make(tmp_path);s.event(trade(),AT);s.close(AT+1000)
 again=Store(tmp_path,AT+2000);again.begin(AT+2000)
 assert not again.event(trade(),AT+2000)
 again.event(trade(102,at=AT+2000),AT+2000);again.flush(AT+2001)
 assert again.state['counts']['agg_trades']==2
 assert again.state['counts']['id_gaps']==1
 kinds=[r[0] for r in again.db.execute('SELECT kind FROM issues')]
 assert 'PROCESS_RESTART_GAP' in kinds and 'AGGREGATE_ID_GAP' in kinds
 assert again.manifest['registered_ms']==AT
 again.close(AT+3000)
def test_incomplete_coverage_never_presented_as_complete(tmp_path):
 s=make(tmp_path);s.event(trade(),AT);s.flush(AT+MINUTE)
 value=json.loads(s.db.execute('SELECT payload FROM segments').fetchone()[0])
 assert value['coverage']=='OBSERVED_SEGMENT_NOT_CERTIFIED_FULL_MINUTE'
 assert s.status(AT+MINUTE)['health']=='DEGRADED'
 s.close(AT+MINUTE)
@pytest.mark.parametrize('field,value',[('m','false'),('q','NaN'),('p','0'),('a',True),('E',AT-40000),('T',AT+6000)])
def test_invalid_stream_inputs_are_rejected(tmp_path,field,value):
 s=make(tmp_path);e=trade();e[field]=value
 with pytest.raises(ValueError):s.event(e,AT)
 assert s.state['counts']['agg_trades']==0
 s.close(AT)
def test_liquidation_snapshots_are_deduplicated(tmp_path):
 s=make(tmp_path)
 e={'e':'forceOrder','E':AT,'o':{'s':'BTCUSDT','S':'SELL','T':AT,'q':'3','ap':'100','z':'2'}}
 s.event(e,AT);s.event(e,AT);s.flush(AT+1)
 assert s.db.execute('SELECT count(*) FROM liquidations').fetchone()[0]==1
 assert s.state['counts']['liquidation_snapshots']==1
 assert 'not total liquidations' in s.status(AT+1)['limitations']
 s.close(AT+1)
def snapshots(s,symbol,at=AT):
 s.snapshot(symbol,'open_interest',{'symbol':symbol,'openInterest':'50','time':at},at,at)
 s.snapshot(symbol,'book5',{'bids':[['99','2']],'asks':[['101','2']],'E':at},at,at)
 s.snapshot(symbol,'mark_funding_indication',{'symbol':symbol,'markPrice':'100','lastFundingRate':'-.0001','time':at},at,at)
def test_health_requires_all_symbols_and_stream_freshness(tmp_path):
 s=make(tmp_path)
 for symbol in SYMBOLS:snapshots(s,symbol);s.event(trade(symbol=symbol),AT)
 assert s.status(AT+1)['health']=='OK'
 assert s.status(AT+151000)['health']=='DEGRADED'
 s.end(AT+2,'NETWORK_LOSS')
 assert 'WEBSOCKET_OFFLINE' in s.status(AT+3)['errors']
 s.close(AT+4)
def test_invalid_crossed_book_and_wrong_symbol_are_not_saved(tmp_path):
 s=make(tmp_path)
 with pytest.raises(ValueError,match='CROSSED_BOOK'):
  s.snapshot('BTCUSDT','book5',{'bids':[['102','2']],'asks':[['101','2']]},AT,AT)
 with pytest.raises(ValueError,match='SYMBOL_MISMATCH'):
  s.snapshot('BTCUSDT','open_interest',{'symbol':'ETHUSDT','openInterest':'1','time':AT},AT,AT)
 assert s.db.execute('SELECT count(*) FROM snapshots').fetchone()[0]==0
 s.close(AT)
def test_frozen_identity_cannot_be_silently_replaced(tmp_path):
 s=make(tmp_path);s.close(AT+1)
 with sqlite3.connect(tmp_path/'market_evidence.sqlite3') as c:
  v=json.loads(c.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()[0]);v['identity']['code_sha256']='changed'
  c.execute("UPDATE meta SET value=? WHERE key='manifest'",(json.dumps(v),))
 with pytest.raises(ValueError,match='FROZEN_COLLECTOR_CHANGED'):Store(tmp_path,AT+2)
def test_flush_is_idempotent_and_segments_survive_pruning(tmp_path):
 s=make(tmp_path);s.event(trade(),AT);s.flush(AT+1);s.flush(AT+2)
 assert s.db.execute('SELECT count(*) FROM segments').fetchone()[0]==1
 s.flush(AT+3*MINUTE)
 assert not s.buckets
 assert s.db.execute('SELECT count(*) FROM segments').fetchone()[0]==1
 s.close(AT+3*MINUTE)
