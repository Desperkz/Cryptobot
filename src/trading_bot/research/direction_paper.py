"""Single-factor SQZ / 4h-regime direction comparison, local paper only."""
from trading_bot.models import Direction, MarketRegime
from trading_bot.research.early_paper import EarlyStore
from trading_bot.research.mainnet_paper import Lab, main

ARMS = ('DIRECTION_BASELINE_2R', 'DIRECTION_ALIGNED_2R')
RULE = {
    'name': 'sqz-direction-v1',
    'scope': 'Prospective SQZ source baseline versus one 4h-regime direction gate',
    'source': 'Unchanged SQZ generator, fixed original 32 symbols and liquidity floors',
    'control': 'All valid SQZ source signals; diagnostic baseline, not strict production admission',
    'treatment': 'Reject SHORT in TREND_UP and LONG in TREND_DOWN only',
    'other_regimes': 'All other valid regime labels retain baseline admission, including UNKNOWN',
    'invalid_input': 'Missing/invalid regime, direction or non-SQZ source fails closed in both arms',
    'identity': 'Symbol + direction + actual closed 1h bar, first observation only',
    'occupancy': 'Shared symbol occupancy across arms; no retry after refusal or occupancy',
    'execution': 'Same next-minute entry, original stop, 2R, 2 virtual USDT risk before costs, 24h',
    'isolation': 'No additional OF/RS/retest changes or new exits in this single-factor test',
    'evaluation': 'Net outcomes, drawdown and opportunity counts; correlated copies are not independent',
    'review': 'At least 50 closed control sources and 14 elapsed days; descriptive review, not live approval',
    'cost_stress': 'Offline replay of the same minute bars with 10bps each-side slippage; no refitting',
}


def direction_decisions(signal):
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    regime = metadata.get('regime')
    errors = []
    if metadata.get('strategy') != 'SQUEEZE_BREAKOUT':
        errors.append('WRONG_SOURCE')
    if signal.direction not in (Direction.LONG, Direction.SHORT):
        errors.append('INVALID_DIRECTION')
    if not isinstance(regime, str) or regime not in {r.value for r in MarketRegime}:
        errors.append('INVALID_REGIME')
    opposite = (signal.direction == Direction.SHORT and regime == 'TREND_UP') or (
        signal.direction == Direction.LONG and regime == 'TREND_DOWN')
    failures = errors + (['COUNTERTREND_4H'] if opposite else [])
    return {
        'allowed': {ARMS[0]: not errors, ARMS[1]: not failures},
        'regime': regime, 'direction': str(signal.direction),
        'source_quality_failures': errors, 'directional_gate_failures': failures,
        'rule_version': RULE['name'],
    }


class DirectionStore(EarlyStore):
    def source_id(self, signal):
        return super().source_id(signal).replace('SQZ_EARLY:', 'SQZ_DIRECTION:', 1)


class DirectionLab(Lab):
    arms = ARMS

    def accepts(self, signal):
        return signal.metadata.get('strategy') == 'SQUEEZE_BREAKOUT'

    def decisions(self, signal):
        return direction_decisions(signal)


if __name__ == '__main__':
    main(lab_class=DirectionLab, store_class=DirectionStore, experiment=RULE)
