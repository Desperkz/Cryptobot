"""Isolated BTC RS applicability experiment. Never rewrite neutral as aligned."""
from dataclasses import replace
from decimal import Decimal

from trading_bot.bot import _sqz_gate_cohort_shadow_variants, _squeeze_context_gate_rejection
from trading_bot.research.early_paper import EarlyStore, finite
from trading_bot.research.mainnet_paper import Lab, main

ARMS = ('BTC_RS_STRICT_2R', 'BTC_RS_NOT_APPLICABLE_2R')
RULE = {
    'name': 'btc-rs-applicability-v1',
    'scope': 'BTC-only prospective paper comparison of self-benchmark RS applicability',
    'source': 'Unchanged SQZ generator and liquidity floors, BTCUSDT only',
    'control': 'Strict gate vector on corrected order flow; all safety/context gates',
    'treatment': 'Remove only RS_NEUTRAL when finite BTC self-comparison is verified',
    'annotation': 'RS stays neutral in source; applicability=not_applicable in decision only',
    'source_identity': 'BTC + direction + actual closed 1h bar, first observation only',
    'occupancy': 'Shared across arms; no retry after rejection or occupied source',
    'execution': 'Existing next-minute 2R, risk 2 USDT before costs, 24h maximum',
    'limits': 'Not an early-entry fix or a global flow/structure/RS relaxation',
}


def self_benchmark_verified(signal):
    rs = signal.metadata.get('relative_strength')
    if signal.symbol != 'BTCUSDT' or not isinstance(rs, dict) or rs.get('alignment') != 'neutral':
        return False
    own, benchmark, relative = [finite(rs.get(k)) for k in
        ('symbol_change_4h', 'btc_change_4h', 'relative_change_4h')]
    if any(v is None for v in (own, benchmark, relative)):
        return False
    # Both existing arithmetic forms differ by Decimal rounding (~1e-29).
    # This is an equality tolerance, not a market-strength threshold.
    tolerance = Decimal('1e-24')
    return abs(own-benchmark) <= tolerance and abs(relative) <= tolerance and abs(relative-(own-benchmark)) <= tolerance


def btc_rs_decisions(signal, config):
    m = signal.metadata
    flow = m.get('p8_order_flow')
    errors = []
    if signal.symbol != 'BTCUSDT' or m.get('strategy') != 'SQUEEZE_BREAKOUT':
        errors.append('WRONG_SOURCE')
    verified = self_benchmark_verified(signal)
    if not verified:
        errors.append('UNVERIFIED_SELF_BENCHMARK')
    score = finite(flow.get('score')) if isinstance(flow, dict) else None
    if (not isinstance(flow, dict) or flow.get('alignment') not in ('aligned','mixed','against')
            or score is None or not Decimal('0') <= score <= Decimal('1')
            or not isinstance(flow.get('risk_flags'), list) or not isinstance(flow.get('reasons'), list)):
        errors.append('INVALID_CORRECTED_FLOW')
        failures, safety, context = [], [], None
    else:
        corrected = replace(signal, metadata={**m, 'order_flow':flow})
        _, failures, safety = _sqz_gate_cohort_shadow_variants(
            corrected, replace(config, squeeze_gate_cohort_shadow_enabled=True))
        context = _squeeze_context_gate_rejection(corrected, config)
    common = safety + errors + ([context[0]] if context else [])
    control = sorted(set(failures + common))
    treatment = sorted(set([x for x in failures if not (verified and x=='RS_NEUTRAL')] + common))
    return {'allowed':{ARMS[0]:not control, ARMS[1]:not treatment},
            'control_all_gate_failures':control, 'treatment_all_gate_failures':treatment,
            'current_all_gate_failures':failures,'current_structural_flags':safety,
            'source_quality_failures':errors,'rule_version':RULE['name'],
            'rs_applicability':'not_applicable_self_benchmark' if verified else 'unverified',
            'removed_requirement':'RS_NEUTRAL' if verified and 'RS_NEUTRAL' in failures else None}


class BTCStore(EarlyStore):
    def source_id(self, signal):
        return super().source_id(signal).replace('SQZ_EARLY:', 'SQZ_BTC_RS:', 1)


class BTCLab(Lab):
    arms = ARMS

    def __init__(self, config, symbols, store, cohort):
        if symbols != ['BTCUSDT']:
            raise ValueError('BTC RS experiment requires the singleton BTCUSDT universe')
        super().__init__(config, symbols, store, cohort)

    def accepts(self, signal):
        return signal.symbol == 'BTCUSDT' and signal.metadata.get('strategy') == 'SQUEEZE_BREAKOUT'

    def decisions(self, signal):
        return btc_rs_decisions(signal, self.gate_config)


if __name__ == '__main__':
    main(lab_class=BTCLab, store_class=BTCStore, experiment=RULE)
