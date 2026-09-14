"""Read-only release check. Run with the project's Python from its root."""
import json
from copy import deepcopy
from decimal import Decimal

from trading_bot.bot import _p8_shadow_variants, _order_flow_entry_rejection_reason
from trading_bot.config import load_config
from trading_bot.models import Direction, Signal, TradingMode, TradingStyle


def main():
    cfg=load_config('config.yaml', '.env')
    assert cfg.strategy.order_flow_entry_gate_mode=='measure'
    assert not cfg.strategy.shadow_conditional_neutralize_order_flow
    assert cfg.strategy.p8_shadow_enabled
    assert cfg.risk.max_concurrent_positions==4
    flow={'alignment':'against','score':'0.20','risk_flags':['taker_flow_against'],
          'reasons':['structure_break_aligned']}
    s=Signal(symbol='BTCUSDT',direction=Direction.LONG,style=TradingStyle.INTRADAY,
             entry_price=Decimal('100'),stop_loss=Decimal('95'),take_profit=Decimal('111'),
             confidence=Decimal('.8'),reason='read-only release check',timeframe='15m',metadata={
                 'strategy':'SQUEEZE_BREAKOUT','regime':'TREND_UP','squeeze_bars_4h':2,
                 'squeeze_retest_confirmed':True,'signal_bar_close_time':1789380000000,
                 'relative_strength':{'alignment':'aligned','score':'0.80'},
                 'order_flow':deepcopy(flow),'p8_order_flow':deepcopy(flow),
             })
    variants,decisions=_p8_shadow_variants(s,cfg.strategy)
    assert len(variants)==1
    v=variants[0]
    assert v.metadata['strategy']=='P8_SQZ_COND_OBSERVE_SHADOW'
    assert _order_flow_entry_rejection_reason(s,cfg.strategy)
    assert _order_flow_entry_rejection_reason(v,cfg.strategy)[0]=='SHADOW_ONLY'
    assert not any(x.startswith('P8_') for x in cfg.strategy.execution_strategies(TradingMode.PAPER_TRADING))
    print(json.dumps({'cohort':cfg.strategy.p8_shadow_cohort,'paper_mode':cfg.strategy.order_flow_entry_gate_mode,
                      'max_positions':cfg.risk.max_concurrent_positions,'virtual_variants':len(variants),
                      'production_admission_blocked':True,'writes_to_database':0}))


if __name__=='__main__':
    main()
