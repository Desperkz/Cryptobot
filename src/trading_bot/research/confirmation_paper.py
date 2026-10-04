"""Prospective same-level price breakout/retest comparison, local paper only."""
import time
from dataclasses import replace
from decimal import Decimal

from trading_bot.bot import _is_strong_clean_squeeze_release
from trading_bot.models import Direction
from trading_bot.research.early_paper import EarlyStore, finite
from trading_bot.research.mainnet_paper import Lab, main
from trading_bot.strategy_engine.indicators import atr
from trading_bot.strategy_engine.squeeze_breakout import SqueezeBreakoutStrategy, _compression_range

ARMS = ('CONFIRM_SOURCE_2R', 'CONFIRM_LEGACY_2R', 'CONFIRM_LEVEL_2R')
RULE = {
    'name': 'sqz-confirmation-v1',
    'scope': 'Same SQZ source, legacy confirmation pair versus chronological same-level confirmation',
    'source': 'Unchanged generator, original fixed 32 symbols and liquidity floors',
    'baseline': 'All valid sources; supplies paired outcomes for refusals',
    'control': '(Saved 1h retest OR existing strict strong-release exception) AND corrected 15m structure reason',
    'treatment': '1h close and latest 15m close hold the compression level; earlier closed 15m price breakout followed by a distinct closed 15m retest OR same strong-release exception',
    'level': 'Recompute unchanged 1h compression range preceding release/build anchor; require exact metadata match',
    'retest': 'Reuse configured 0.25 ATR tolerance, 0.10 ATR rejection body and 1.5 wick multiplier; no outcome fitting',
    'sequence': 'A close through the wrong side resets the sequence; do not count the first price breakout as its own retest',
    'timing': 'First observation per actual closed hour; no waiting/retry or backdated entry; all inputs closed and available',
    'occupancy': 'Shared across arms; keep all refusals/skips; identical accepted sources have identical plans',
    'execution': 'Existing next-minute open, original stop, 2R, 2 virtual USDT before costs, 24h',
    'isolation': 'No direction gate, RS bypass, additional OF admission, new exits or cost sizing',
    'review': 'At least 50 closed source-control entries and 14 days; descriptive, not live approval',
    'cost_stress': 'Offline same-bar replay with 10bps each-side slippage instead of 5bps; no refitting',
}


def _valid_frames(candles, duration, observed):
    if not candles or not isinstance(observed, int) or isinstance(observed, bool):
        return False
    previous = None
    for c in candles:
        values = [finite(getattr(c, k)) for k in ('open', 'high', 'low', 'close')]
        if any(v is None or v <= 0 for v in values):
            return False
        o, h, l, close = values
        if not l <= min(o, close) <= max(o, close) <= h:
            return False
        if (type(c.open_time) is not int or type(c.close_time) is not int
                or c.open_time % duration or c.close_time != c.open_time + duration - 1
                or c.close_time >= observed or (previous is not None and c.open_time != previous + duration)):
            return False
        previous = c.open_time
    return 0 < observed - candles[-1].close_time <= duration


def level_confirmation(signal, candles_1h, candles_15m, config, observed_ms):
    """Judge a completed sequence at observation; never enter on an earlier bar."""
    m = signal.metadata if isinstance(signal.metadata, dict) else {}
    invalid = {'status': 'INVALID', 'failures': ['INVALID_CONFIRMATION_INPUT']}
    bars, offset = m.get('squeeze_bars'), m.get('squeeze_release_offset')
    state = m.get('squeeze_state')
    if (m.get('strategy') != 'SQUEEZE_BREAKOUT' or signal.direction not in (Direction.LONG, Direction.SHORT)
            or not isinstance(bars, int) or isinstance(bars, bool) or bars < 2
            or not candles_1h or len(candles_1h) < config.atr_period + 1
            or not _valid_frames(candles_1h, 3600000, observed_ms)
            or not _valid_frames(candles_15m, 900000, observed_ms)
            or candles_15m[-1].close_time < candles_1h[-1].close_time):
        return invalid
    if state == 'build' and m.get('squeeze_entry_timing') == 'early_breakout' and offset is None:
        offset = 0
    elif not (state == 'release' and m.get('squeeze_entry_timing') == 'release_followthrough'
              and isinstance(offset, int) and not isinstance(offset, bool)
              and 0 <= offset < config.squeeze_release_lookback_bars):
        return invalid
    anchor = len(candles_1h) - 1 - offset
    if anchor < min(max(2, bars), 30):
        return invalid
    compression = _compression_range(candles_1h, bars, offset)
    if compression is None or compression != (finite(m.get('compression_high')), finite(m.get('compression_low'))):
        return invalid
    atr_value = finite(atr(candles_1h, config.atr_period)[-1])
    tolerance, body_min = finite(config.squeeze_retest_tolerance_atr), finite(config.squeeze_retest_min_rejection_body_atr)
    if atr_value is None or atr_value <= 0 or tolerance is None or tolerance < 0 or body_min is None or body_min < 0:
        return invalid
    level = compression[0] if signal.direction == Direction.LONG else compression[1]
    start = candles_1h[anchor].open_time
    lower_bound = max(start, candles_15m[-1].close_time + 1 - config.squeeze_retest_lookback_bars * 3600000)
    window = [c for c in candles_15m if c.open_time >= lower_bound]
    if not window or window[0].open_time != lower_bound or not lower_bound % 900000 == 0:
        return invalid
    sign = Decimal('1') if signal.direction == Direction.LONG else Decimal('-1')
    held = lambda close: sign * (close - level) > 0
    first_break, retest = None, None
    for c in window:
        if not held(c.close):
            first_break, retest = None, None
            continue
        if first_break is None:
            first_break = c.close_time
            continue
        # Closed bars are contiguous, so this candle starts after the breakout closed.
        body = abs(c.close - c.open)
        wick = min(c.open, c.close) - c.low if sign > 0 else c.high - max(c.open, c.close)
        touch = c.low <= level + atr_value * tolerance and c.high >= level - atr_value * tolerance
        rejection = sign * (c.close - c.open) > 0 and body >= atr_value * body_min
        absorption = wick >= max(body, atr_value * body_min) * Decimal('1.5')
        if touch and (rejection or absorption):
            retest = c.close_time
    return {'status': 'VALID', 'failures': [], 'as_of_ms': observed_ms,
            'level': str(level), 'atr_1h': str(atr_value), 'anchor_open_ms': start,
            'window_start_ms': lower_bound, 'breakout_close_ms': first_break,
            'retest_close_ms': retest, 'confirmed_retest': retest is not None,
            'hourly_level_held': held(candles_1h[-1].close),
            'latest_15m_level_held': held(candles_15m[-1].close),
            'last_1h_close_ms': candles_1h[-1].close_time,
            'last_15m_close_ms': candles_15m[-1].close_time}


def _valid_evidence(evidence, metadata):
    if not isinstance(evidence, dict) or evidence.get('status') != 'VALID':
        return False
    if not all(isinstance(evidence.get(k), bool) for k in (
            'confirmed_retest', 'hourly_level_held', 'latest_15m_level_held')):
        return False
    keys = ('as_of_ms', 'anchor_open_ms', 'window_start_ms', 'last_1h_close_ms', 'last_15m_close_ms')
    if not all(type(evidence.get(k)) is int for k in keys) or type(metadata.get('observed_ms')) is not int:
        return False
    if not (evidence['last_1h_close_ms'] == metadata.get('source_hour_close_time')
            and evidence['last_1h_close_ms'] <= evidence['last_15m_close_ms'] < evidence['as_of_ms']
            <= metadata['observed_ms'] and evidence['anchor_open_ms'] <= evidence['window_start_ms']):
        return False
    if (evidence['anchor_open_ms'] % 3600000 or evidence['window_start_ms'] % 900000
            or (evidence['last_1h_close_ms'] + 1) % 3600000
            or (evidence['last_15m_close_ms'] + 1) % 900000
            or evidence['as_of_ms'] - evidence['last_15m_close_ms'] > 900000):
        return False
    if any(finite(evidence.get(k)) is None or finite(evidence.get(k)) <= 0 for k in ('level', 'atr_1h')):
        return False
    first, retest = evidence.get('breakout_close_ms'), evidence.get('retest_close_ms')
    if first is not None and (type(first) is not int or (first + 1) % 900000
                             or not evidence['window_start_ms'] <= first <= evidence['last_15m_close_ms']):
        return False
    return (type(first) is int and type(retest) is int and (retest + 1) % 900000 == 0
            and first + 900000 <= retest <= evidence['last_15m_close_ms']) if evidence['confirmed_retest'] else retest is None


def confirmation_decisions(signal, config):
    m = signal.metadata if isinstance(signal.metadata, dict) else {}
    evidence, flow = m.get('level_confirmation'), m.get('p8_order_flow')
    errors = []
    if m.get('strategy') != 'SQUEEZE_BREAKOUT' or signal.direction not in (Direction.LONG, Direction.SHORT):
        errors.append('WRONG_SOURCE')
    if not _valid_evidence(evidence, m):
        errors.append('INVALID_CONFIRMATION_INPUT')
    score = finite(flow.get('score')) if isinstance(flow, dict) else None
    if (not isinstance(flow, dict) or flow.get('alignment') not in ('aligned', 'mixed', 'against')
            or score is None or not Decimal('0') <= score <= Decimal('1')
            or not isinstance(flow.get('risk_flags'), list) or not isinstance(flow.get('reasons'), list)
            or not all(isinstance(x, str) for x in flow['risk_flags'] + flow['reasons'])
            or not isinstance(m.get('squeeze_retest_confirmed'), bool)):
        errors.append('INVALID_CORRECTED_FLOW')
    if errors:
        return {'allowed': {a: False for a in ARMS}, 'source_quality_failures': errors,
                'rule_version': RULE['name']}
    corrected = replace(signal, metadata={**m, 'order_flow': flow})
    strong = _is_strong_clean_squeeze_release(corrected, alignment=flow['alignment'],
        score=score, risk_flags=set(flow['risk_flags']))
    legacy = (m['squeeze_retest_confirmed'] or strong) and 'structure_break_aligned' in flow['reasons']
    coherent = (evidence['hourly_level_held'] and evidence['latest_15m_level_held']
                and (evidence['confirmed_retest'] or strong))
    return {'allowed': {ARMS[0]: True, ARMS[1]: legacy, ARMS[2]: coherent},
            'source_quality_failures': [], 'legacy_pair_confirmed': legacy,
            'same_level_confirmed': coherent, 'shared_strong_release': strong,
            'level_evidence': evidence, 'rule_version': RULE['name']}


class ConfirmationStrategy(SqueezeBreakoutStrategy):
    def generate(self, symbol, candles_15m, candles_1h, candles_4h, metrics):
        signal = super().generate(symbol, candles_15m, candles_1h, candles_4h, metrics)
        if signal is None:
            return None
        evidence = level_confirmation(signal, candles_1h, candles_15m, self.config, int(time.time()*1000))
        return replace(signal, metadata={**signal.metadata, 'level_confirmation': evidence})


class ConfirmationStore(EarlyStore):
    def source_id(self, signal):
        return super().source_id(signal).replace('SQZ_EARLY:', 'SQZ_CONFIRM:', 1)


class ConfirmationLab(Lab):
    arms = ARMS

    def __init__(self, config, symbols, store, cohort):
        super().__init__(config, symbols, store, cohort)
        self.strategy = ConfirmationStrategy(config.strategy, self.strategy.regime_detector)

    def accepts(self, signal):
        return signal.metadata.get('strategy') == 'SQUEEZE_BREAKOUT'

    def decisions(self, signal):
        return confirmation_decisions(signal, self.gate_config)


if __name__ == '__main__':
    main(lab_class=ConfirmationLab, store_class=ConfirmationStore, experiment=RULE)
