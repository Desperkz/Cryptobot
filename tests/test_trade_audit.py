from dataclasses import replace
from decimal import Decimal

import pytest

from trading_bot.models import Candle
from trading_bot.research.trade_audit import aggregate
from trading_bot.strategy_engine.vwap_v3 import admission


def hours(up=True):
    return [Candle(i*3600000,Decimal(100+i if up else 400-i),
        Decimal(101+i if up else 401-i),Decimal(99+i if up else 399-i),
        Decimal(100+i if up else 400-i),Decimal(1),(i+1)*3600000-1) for i in range(205)]


@pytest.mark.parametrize('up,direction,expected',[(True,'LONG',True),(True,'SHORT',False),
    (False,'SHORT',True),(False,'LONG',False)])
def test_local_trend_requires_both_price_and_slope_to_oppose_entry(up,direction,expected):
    candles=hours(up)
    before=list(candles)
    result=admission(direction,candles,candles[-1].close_time)
    assert result['allowed'] is expected and candles==before


def test_flat_market_is_not_an_opposite_directional_trend():
    candles=[replace(c,close=Decimal(100)) for c in hours()]
    assert all(admission(d,candles,candles[-1].close_time)['allowed'] for d in ['LONG','SHORT'])


@pytest.mark.parametrize('problem',['future','stale','gap','nan','zero','short','direction'])
def test_invalid_or_noncausal_hypothesis_input_fails_closed(problem):
    candles=hours();bar=candles[-1].close_time;direction='LONG'
    if problem=='future':bar-=3600000
    elif problem=='stale':bar+=3600000
    elif problem=='gap':candles[50]=replace(candles[50],open_time=0)
    elif problem=='nan':candles[-1]=replace(candles[-1],close=Decimal('NaN'))
    elif problem=='zero':candles[-1]=replace(candles[-1],close=Decimal(0))
    elif problem=='short':candles=candles[:200]
    else:direction='INVALID'
    with pytest.raises(ValueError):admission(direction,candles,bar)


def test_attribution_does_not_double_count_entry_slippage_or_realize_open_marks():
    closed={'status':'CLOSED','net_usdt':-3,'risk_usdt':2,'reason':'STOP',
        'costs':{'gross_before_exit_slippage':-2,'entry_slippage':.25,
                 'exit_slippage':.4,'fees':.5,'funding':.1}}
    result=aggregate([closed,{'status':'OPEN','net_usdt':1000}])
    assert result['closed']==1 and result['open']==1
    assert result['net_usdt']==-3 and result['price_only_usdt']==-1.75
    assert result['expenses_usdt']==1.25
    assert result['avg_r']==-1.5


def test_cost_mismatch_never_turns_into_a_plausible_report():
    with pytest.raises(ValueError,match='RECONCILIATION'):
        aggregate([{'status':'CLOSED','net_usdt':10,'risk_usdt':2,'reason':'STOP',
            'costs':{'gross_before_exit_slippage':-2,'entry_slippage':.25,
                     'exit_slippage':.4,'fees':.5,'funding':.1}}])
