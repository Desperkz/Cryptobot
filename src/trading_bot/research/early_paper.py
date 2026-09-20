"""Prospective, isolated early-SQZ admission experiment. Local paper only."""
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from trading_bot.bot import (_sqz_gate_cohort_shadow_variants,
                             _squeeze_context_gate_rejection,
                             _order_flow_entry_rejection_reason)
from trading_bot.research.mainnet_paper import Lab, Store, main

ARMS = ('EARLY_BASELINE_2R', 'EARLY_STRICT_2R', 'EARLY_RULE_2R')
RULE = {
    'name': 'early-sqz-v1',
    'scope': 'Early SQZ admission only; corrected OF, first hourly source, shared symbol occupancy',
    'source': 'Unchanged SQZ generator and liquidity floors; build/early_breakout only',
    'rule': 'Strict corrected OF gate vector except SQZ_RETEST_OR_STRONG_RELEASE; preserve structural, RS and structure gates',
    'thresholds': 'Reuse frozen source/OF configuration; no fit to historical PnL',
    'source_identity': 'symbol + direction + closed 1h bar',
    'timing': 'First observation only; no retry after rejection or occupancy skip',
    'occupancy': 'All arms wait until every position for the previous symbol source has closed',
    'control': 'Same corrected OF inputs with the original retest gate retained',
    'funding_and_costs': 'Identical to original mainnet paper; single 2R exit',
}


def finite(value):
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def is_early(signal):
    m = signal.metadata
    return (m.get('strategy') == 'SQUEEZE_BREAKOUT' and
            m.get('squeeze_state') == 'build' and m.get('squeeze_entry_timing') == 'early_breakout')


def early_decisions(signal, config):
    """Remove only the impossible retest/release requirement from strict OF gates.

    Keep source-quality bounds explicit and fail closed on malformed input.
    No direction, symbol or threshold was selected by retrospective PnL.
    """
    m = signal.metadata
    flow = m.get('p8_order_flow')
    missing = []
    if not is_early(signal):
        missing.append('NOT_EARLY_SOURCE')
    if not isinstance(flow, dict) or not flow:
        missing.append('MISSING_CORRECTED_FLOW')
        flow = {}
    if flow.get('alignment') not in ('aligned', 'mixed', 'against') or finite(flow.get('score')) is None:
        missing.append('INVALID_CORRECTED_FLOW')
    squeeze_bars = finite(m.get('squeeze_bars'))
    minimum = (8 if m.get('regime') in ('TREND_UP', 'TREND_DOWN') else 4) + config.squeeze_early_min_bars_extra
    breakout, volume = finite(m.get('breakout_atr')), finite(m.get('volume_ratio'))
    if squeeze_bars is None or squeeze_bars < minimum:
        missing.append('EARLY_COMPRESSION')
    if breakout is None or not config.squeeze_early_min_breakout_atr <= breakout <= config.squeeze_max_extension_atr:
        missing.append('EARLY_BREAKOUT_DISTANCE')
    if volume is None or volume < max(config.min_volume_ratio, config.squeeze_early_min_volume_ratio):
        missing.append('EARLY_VOLUME')
    corrected = replace(signal, metadata={**m, 'order_flow':flow})
    # The strict and treatment gates use the SAME corrected observations.
    if 'INVALID_CORRECTED_FLOW' in missing:
        failures, safety, context, strict_rejection = [], [], None, ('ORDER_FLOW', 'Invalid corrected flow')
    else:
        _, failures, safety = _sqz_gate_cohort_shadow_variants(
            corrected, replace(config, squeeze_gate_cohort_shadow_enabled=True))
        context = _squeeze_context_gate_rejection(corrected, config)
        strict_rejection = _order_flow_entry_rejection_reason(corrected, replace(config, order_flow_entry_gate_mode='strict'))
    treatment = [f for f in failures if f != 'SQZ_RETEST_OR_STRONG_RELEASE']
    treatment += safety + missing
    if context:
        treatment.append(context[0])
    return {'allowed': {ARMS[0]:not missing, ARMS[1]:not missing and strict_rejection is None,
                        ARMS[2]:not treatment},
            'current_first_rejection':strict_rejection,
            'current_all_gate_failures':failures, 'current_structural_flags':safety,
            'early_all_gate_failures':sorted(set(treatment)),
            'removed_requirement':'SQZ_RETEST_OR_STRONG_RELEASE',
            'source_quality_failures':missing, 'rule_version':RULE['name']}


class EarlyStore(Store):
    def source_id(self, signal):
        # Hour identity comes from the actual closed 1h frame, not wall-clock rounding.
        stamp = signal.metadata['source_hour_close_time']
        observed = signal.metadata['observed_ms']
        if not isinstance(stamp, int) or stamp >= observed or (stamp + 1) % 3600000:
            raise ValueError('Invalid closed hourly source identity')
        return f"SQZ_EARLY:{signal.symbol}:{signal.direction.value}:{stamp}"

    def symbol_busy(self, signal, policy, arm, source_id):
        # Entries for the same source are paired. Another source cannot exploit
        # the treatment arm closing earlier than its baseline comparator.
        return self.db.execute("""SELECT 1 FROM positions WHERE symbol=? AND source_id!=?
            AND status IN ('PENDING','OPEN')""", (signal.symbol,source_id)).fetchone() is not None


class EarlyLab(Lab):
    arms = ARMS

    def accepts(self, signal):
        return is_early(signal)

    def decisions(self, signal):
        return early_decisions(signal, self.gate_config)


if __name__ == '__main__':
    main(lab_class=EarlyLab, store_class=EarlyStore, experiment=RULE)
