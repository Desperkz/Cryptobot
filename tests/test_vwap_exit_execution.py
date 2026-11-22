from copy import deepcopy
import pytest
from trading_bot.research import vwap_exit_execution as e
from trading_bot.research import diversified_v4_common as c

AT=1800000000000


def plan(direction='LONG'):
    side=1 if direction=='LONG' else -1
    p={'strategy':'VWAP_REVERSION_V3_RECLAIM','symbol':'BTCUSDT','direction':direction,
       'opened_ms':AT+1000,'expires_ms':AT+1000+28800000,'entry':100.,'entry_mid':100/(1+side*.0005),
       'stop':100-side*10,'target':100+side*25,'risk_usdt':2.,'id':1,'snapshot_id':1}
    p['quantity']=c.quantity(p)
    return p


def bar(i,o,h,l,close):
    stamp=AT+(i+1)*60000
    return [stamp,o,h,l,close,10,stamp+59999]


def test_target_and_protection_can_preserve_move_before_later_original_stop():
    p=plan();bars=[bar(0,100,112,99,110),bar(1,108,109,99,101),bar(2,101,102,89,91)]
    before=deepcopy((p,bars));r=e.replay(p,bars)
    assert r['control']['reason']=='STOP' and r['control']['net_usdt']<0
    assert r['VWAP_TARGET_1R']['closed_ms']==bars[0][6] and r['VWAP_TARGET_1R']['net_usdt']>0
    assert r['VWAP_PROTECT_1R']['reason']=='PROTECTED_STOP'
    assert r['VWAP_PROTECT_1R']['closed_ms']==bars[1][6]
    assert r['protected_state']['armed_ms']==bars[0][6]
    assert r['VWAP_PROTECT_1R']['net_usdt']<0  # entry-price stop still pays costs
    assert (p,bars)==before


def test_wick_touch_does_not_arm_close_based_protection():
    p=plan();bars=[bar(0,100,111,99,109),bar(1,109,109,89,91)]
    r=e.replay(p,bars)
    assert r['VWAP_TARGET_1R']['net_usdt']>0
    assert r['protected_state']['armed_ms'] is None
    assert r['VWAP_PROTECT_1R']==r['control']


def test_gap_on_armed_stop_fills_at_adverse_open_not_entry():
    p=plan();bars=[bar(0,100,112,99,110),bar(1,98,99,96,97),bar(2,97,100,89,91)]
    r=e.replay(p,bars)
    assert r['VWAP_PROTECT_1R']['exit_price']==pytest.approx(98*.9995)


def test_stop_first_ambiguity_precedes_target_or_arming():
    p=plan();bars=[bar(0,100,126,89,115)]
    r=e.replay(p,bars)
    assert r['protected_state']['armed_ms'] is None
    assert r['control']['reason']==r['VWAP_TARGET_1R']['reason']==r['VWAP_PROTECT_1R']['reason']=='STOP_FIRST_AMBIGUOUS'


def test_short_direction_is_symmetric_and_original_target_never_extended():
    p=plan('SHORT');bars=[bar(0,100,101,88,90),bar(1,92,101,91,99),bar(2,99,111,98,109)]
    r=e.replay(p,bars)
    assert r['VWAP_TARGET_1R']['exit_price']==pytest.approx(90*1.0005)
    assert r['VWAP_PROTECT_1R']['exit_price']==pytest.approx(100*1.0005)
    p['target']=95
    assert e.closer_target(p)['target']==95


def test_missing_minutes_are_not_silently_skipped():
    with pytest.raises(ValueError,match='MISSING_MINUTE'):
        e.replay(plan(),[bar(0,100,101,99,100),bar(2,100,101,99,100)])


def test_diagnostics_exclude_ambiguous_last_minute():
    p=plan();bars=[bar(0,100,109,99,108),bar(1,108,125,89,115)]
    control=e.replay(p,bars)['control'];d=e.diagnostics(p,bars,control)
    assert d['best_close_r_before_exit']==.8 and not d['closed_above_1r_before_exit']
