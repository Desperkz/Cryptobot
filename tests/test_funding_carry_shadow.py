import copy,json,sqlite3
import pytest
from trading_bot.research.funding_carry_ledger import CarryLedger,finite
from trading_bot.research import funding_carry_shadow as f

DAY=f.DAY;H=f.HOUR;T=1800000000000//DAY*DAY;REG=T+18*H
def payload(now):
 prices={'BTCUSDT':100000.,'ETHUSDT':3000.}
 return {'quotes':{s:{'spot_bid':p-.1,'spot_ask':p+.1,'perp_bid':p-.1,'perp_ask':p+.1,'mark':p,'received_ms':now} for s,p in prices.items()},
         'funding':{s:[] for s in f.SYMBOLS},'filters':copy.deepcopy(f.FILTERS),'errors':[],
         'previous_day_close':{s:{'price':p,'close_ms':now//DAY*DAY-1} for s,p in prices.items()}}
def opened():
 now=T+DAY+H+1000
 state,_,orders,status=f.advance(f.initial_state(REG),now,payload(now))
 assert len(orders)==2 and status['health']=='OK'
 return state

def test_before_first_future_source_never_enters():
 now=REG+1000;state,pay,orders,status=f.advance(f.initial_state(REG),now,payload(now))
 assert not orders and not pay and state['orders']==0
 assert status['next_execution_ms']==T+DAY+H and status['pnl']['net_usdt']==0

def test_source_must_be_post_registration_even_if_execution_later():
 reg=T+DAY+H//2;now=T+DAY+H+1000
 _,_,orders,status=f.advance(f.initial_state(reg),now,payload(now))
 assert not orders and status['next_execution_ms']==T+2*DAY+H

def test_future_hedge_matched_quantized_and_costed():
 state=opened();b=CarryLedger.restore(state['books'][0])
 for s in f.SYMBOLS:
  assert b.qty(s)>0 and abs(b.qty(s)/.001-round(b.qty(s)/.001))<1e-9
 assert b.cash<1000
 now=T+DAY+H+2000;_,_,orders,status=f.advance(state,now,payload(now))
 assert not orders and status['pnl']['net_usdt']<0
 p=status['pnl'];assert p['net_usdt']==pytest.approx(p['basis_pnl_usdt']+p['actual_funding_cash_usdt']-p['model_fees_usdt']-p['model_slippage_usdt']-p['observed_spread_usdt'])
 assert status['combined_with_main_balance'] is False and status['order_capability'] is False

def test_missed_window_is_not_backfilled():
 now=T+DAY+H+f.WINDOW+1;state,_,orders,status=f.advance(f.initial_state(REG),now,payload(now))
 assert not orders and state['missed_days']==1
 again,_,_,_=f.advance(state,now+1000,payload(now+1000));assert again['missed_days']==1
 assert status['next_execution_ms']==T+2*DAY+H

def test_invalid_source_day_cannot_open():
 now=T+DAY+H+1000;p=payload(now);p['previous_day_close']['BTCUSDT']['close_ms']=now
 state,_,orders,status=f.advance(f.initial_state(REG),now,p)
 assert not orders and state['last_decision_ms']==0
 assert status['health']=='DEGRADED' and status['pnl'] is None
 assert status['next_execution_ms']==T+DAY+H

def test_missing_quote_does_not_invent_zero_profit():
 state=opened();now=T+DAY+2*H;p=payload(now);p['quotes'].pop('ETHUSDT')
 _,_,orders,status=f.advance(state,now,p)
 assert not orders and status['pnl'] is None and status['health']=='DEGRADED'
 assert status['open_hedge_pairs']==len(status['holdings'])==2

def test_stale_quote_blocks_entry():
 now=T+DAY+H+1000;p=payload(now);p['quotes']['ETHUSDT']['received_ms']-=31000
 _,_,orders,status=f.advance(f.initial_state(REG),now,p)
 assert not orders and status['pnl'] is None

def test_future_funding_not_credited():
 state=opened();now=T+DAY+2*H;p=payload(now)
 for s in f.SYMBOLS:p['funding'][s]=[{'symbol':s,'fundingTime':now+H,'fundingRate':'0.001','markPrice':'100'}]
 after,pay,_,_=f.advance(state,now,p)
 assert not pay and after['funding_cursors']==state['funding_cursors']

@pytest.mark.parametrize('rate',[.0001,-.0001])
def test_signed_funding_idempotent(rate):
 state=opened();at=T+DAY+8*H;now=at+1000;p=payload(now)
 for s in f.SYMBOLS:p['funding'][s]=[{'symbol':s,'fundingTime':at,'fundingRate':rate,'markPrice':p['quotes'][s]['mark']}]
 after,pay,orders,status=f.advance(state,now,p);assert len(pay)==2 and not orders
 assert status['pnl']['actual_funding_cash_usdt']*rate>0
 twice,pay2,_,status2=f.advance(after,now,p)
 assert not pay2 and twice['funding_payments']==after['funding_payments']
 assert status2['pnl']==status['pnl']

def test_missing_funding_blocks_entry_and_known_balance():
 now=T+DAY+H+1000;p=payload(now);p['funding'].pop('ETHUSDT')
 _,_,orders,status=f.advance(f.initial_state(REG),now,p)
 assert not orders and status['pnl'] is None

def test_margin_does_not_use_spot_unrealized_profit():
 state=opened();now=T+DAY+3*H;p=payload(now)
 for q in p['quotes'].values():
  for k in ('spot_bid','spot_ask','perp_bid','perp_ask','mark'):q[k]*=20
 after,_,orders,status=f.advance(state,now,p)
 assert after['blocked'] and all(o['reason']=='MARGIN_GUARD' for o in orders)
 assert status['open_hedge_pairs']==0 and status['pnl'] is None

@pytest.mark.parametrize('value',[True,float('nan'),float('inf'),-float('inf')])
def test_ledger_rejects_nonfinite_and_booleans(value):
 b=CarryLedger(1000);before=b.snapshot()
 with pytest.raises(ValueError):b.trade('BTCUSDT',value,100,100,0)
 assert b.snapshot()==before

def test_public_client_cannot_submit_an_order():
 with pytest.raises(ValueError,match='UNAPPROVED_PUBLIC_ENDPOINT'):
  f.PublicClient().get('perp','/fapi/v1/order',{})

def test_snapshot_restore_preserves_cash_and_hedges():
 b=CarryLedger(1000);b.trade('BTC',.5,100,100,1);b.settlement('BTC',-.0001,100,2)
 restored=CarryLedger.restore(json.loads(json.dumps(b.snapshot())))
 assert restored.snapshot()==b.snapshot()

def test_manifest_refuses_changed_code_without_reset(tmp_path,monkeypatch):
 c,m=f.connect(tmp_path,REG);before=c.execute('SELECT payload FROM state').fetchone()[0];c.close()
 monkeypatch.setattr(f,'code_hashes',lambda:{'changed':'x'})
 with pytest.raises(ValueError,match='FROZEN_COHORT_CHANGED'):f.connect(tmp_path,REG+1)
 c=sqlite3.connect(tmp_path/'funding_carry.sqlite3');assert c.execute('SELECT payload FROM state').fetchone()[0]==before;c.close()
