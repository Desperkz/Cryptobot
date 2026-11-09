import copy,json
import pytest
from paper_pilots_api import api_funding_carry_shadow
from trading_bot.research.funding_carry_shadow import RULE

NOW=2_000_000
def sample():
 return {**copy.deepcopy(RULE),'registered_ms':1_000_000,'generated_at_ms':NOW,'next_execution_ms':90_000_000,
  'health':'OK','errors':[],'counts':{'orders':0,'funding_payments':0,'missed_days':0,'minimum_order_skips':0},
  'open_hedge_pairs':0,'holdings':[],'blocked':False,'collateral_surplus_usdt':1000,
  'pnl':{'balance_usdt':1000.,'net_usdt':0.,'capital_return_pct':0.,'actual_funding_cash_usdt':0.,
         'model_fees_usdt':0.,'model_slippage_usdt':0.,'observed_spread_usdt':0.,'basis_pnl_usdt':0.,'stress_net_usdt':0.}}
def save(tmp_path,data):
 p=tmp_path/'status.json';p.write_text(json.dumps(data));return p
def test_initial_flat_and_stale_are_distinct(tmp_path):
 p=save(tmp_path,sample());before=p.read_bytes()
 assert api_funding_carry_shadow(p,now_ms=NOW)['health']=='OK'
 old=api_funding_carry_shadow(p,now_ms=NOW+181000)
 assert old['health']=='STALE' and old['pnl']['balance_usdt']==1000
 assert p.read_bytes()==before

@pytest.mark.parametrize('mutation',[
 lambda d:d.update(order_capability=True),lambda d:d.update(mode='LIVE'),
 lambda d:d.update(combined_with_main_balance=True),lambda d:d.update(cohort='old'),
 lambda d:d.update(capital_usdt=10000),lambda d:d.update(spot_fee_bps=0),
 lambda d:d['counts'].update(orders=True),lambda d:d['counts'].update(funding_payments=1),
 lambda d:d['pnl'].update(net_usdt=1),lambda d:d['pnl'].update(model_fees_usdt=-1),
 lambda d:d['pnl'].update(stress_net_usdt=1),lambda d:d['pnl'].update(balance_usdt=float('nan')),
 lambda d:d.update(generated_at_ms=NOW+61000),lambda d:d.update(open_hedge_pairs=1),
 lambda d:d.update(holdings=['broken'],open_hedge_pairs=1),
 lambda d:d.update(pnl=None),
])
def test_corrupt_or_wrong_protocol_unavailable(tmp_path,mutation):
 d=sample();mutation(d);r=api_funding_carry_shadow(save(tmp_path,d),now_ms=NOW)
 assert r['health']=='UNAVAILABLE' and 'pnl' not in r

def test_degraded_unknown_value_does_not_become_zero(tmp_path):
 d=sample();d.update(health='DEGRADED',errors=['QUOTE_UNAVAILABLE'],pnl=None)
 r=api_funding_carry_shadow(save(tmp_path,d),now_ms=NOW)
 assert r['health']=='DEGRADED' and r['pnl'] is None

def test_mismatched_underlying_quantities_rejected(tmp_path):
 d=sample();d['counts']['orders']=1;d['open_hedge_pairs']=1
 d['holdings']=[{'symbol':'BTCUSDT','spot_long_quantity':.001,'perp_short_quantity':.002,'opened_ms':1_500_000}]
 assert api_funding_carry_shadow(save(tmp_path,d),now_ms=NOW)['health']=='UNAVAILABLE'

def test_missing_and_oversized_sources_are_not_zero(tmp_path):
 p=tmp_path/'missing.json';assert api_funding_carry_shadow(p)['health']=='UNAVAILABLE'
 p.write_bytes(b' '*(128*1024+1));assert api_funding_carry_shadow(p)['health']=='UNAVAILABLE'
