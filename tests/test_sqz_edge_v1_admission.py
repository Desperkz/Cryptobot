from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from trading_bot.bot import _order_flow_entry_rejection_reason as _legacy_gate
from trading_bot.bot import _production_entry_rejection_reason as _order_flow_entry_rejection_reason
from trading_bot.config import ConfigError, StrategyConfig, load_config
from trading_bot.models import Direction, Signal, TradingStyle

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def edge_config() -> StrategyConfig:
    cfg = load_config(ROOT / "config.yaml").strategy
    return replace(cfg, squeeze_admission_profile="edge_v1")


def sqz(
    *,
    symbol: str = "SOLUSDT",
    alignment: str = "against",
    score: str = "0.20",
    flags: list[str] | None = None,
    rs: str = "neutral",
    regime: str = "TREND_UP",
    squeeze_bars_4h: int = 0,
    retest: bool = False,
    strategy: str = "SQUEEZE_BREAKOUT",
) -> Signal:
    return Signal(
        symbol=symbol,
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        take_profit=Decimal("104"),
        confidence=Decimal("0.70"),
        reason="test",
        metadata={
            "strategy": strategy,
            "regime": regime,
            "squeeze_bars_4h": squeeze_bars_4h,
            "squeeze_retest_confirmed": retest,
            "squeeze_state": "release",
            "squeeze_entry_timing": "release_followthrough",
            "breakout_atr": "0.40",
            "order_flow": {
                "alignment": alignment,
                "score": score,
                "reasons": [],
                "risk_flags": flags or [],
            },
            "relative_strength": {"alignment": rs},
        },
    )


def test_directional_order_flow_no_retest_no_structure_break_is_admitted(edge_config):
    assert _order_flow_entry_rejection_reason(sqz(), edge_config) is None


def test_legacy_profile_still_rejects_same_signal(edge_config):
    legacy = replace(edge_config, squeeze_admission_profile="legacy")
    assert _order_flow_entry_rejection_reason(sqz(), legacy) is not None


@pytest.mark.parametrize(
    "flag",
    ["liquidation_cascade", "structure_break_against", "adverse_liquidity_nearby", "absorption_against"],
)
def test_structural_flags_stay_hard(edge_config, flag):
    rejected = _order_flow_entry_rejection_reason(sqz(flags=[flag]), edge_config)
    assert rejected is not None and rejected[0] == "ORDER_FLOW"


def test_directional_flags_alone_do_not_block(edge_config):
    signal = sqz(flags=["taker_flow_against", "aggressive_delta_against", "book_imbalance_against"])
    assert _order_flow_entry_rejection_reason(signal, edge_config) is None


def test_range_without_4h_compression_is_blocked(edge_config):
    rejected = _order_flow_entry_rejection_reason(sqz(regime="RANGE", squeeze_bars_4h=0), edge_config)
    assert rejected is not None and rejected[0] == "SQZ_CONTEXT"


def test_range_with_4h_compression_is_admitted(edge_config):
    assert _order_flow_entry_rejection_reason(sqz(regime="RANGE", squeeze_bars_4h=3), edge_config) is None


@pytest.mark.parametrize("rs", ["against", "unknown"])
def test_relative_strength_against_or_unknown_blocks_alts(edge_config, rs):
    rejected = _order_flow_entry_rejection_reason(sqz(rs=rs), edge_config)
    assert rejected is not None and rejected[0] == "RELATIVE_STRENGTH"


def test_btc_is_exempt_from_self_benchmark_rs(edge_config):
    assert _order_flow_entry_rejection_reason(sqz(symbol="BTCUSDT", rs="neutral"), edge_config) is None
    # legacy requires aligned, which BTC can never reach against itself
    legacy = replace(edge_config, squeeze_admission_profile="legacy")
    signal = sqz(symbol="BTCUSDT", alignment="aligned", score="0.80", retest=True)
    signal.metadata["order_flow"]["reasons"] = ["structure_break_aligned"]
    rejected = _order_flow_entry_rejection_reason(signal, legacy)
    assert rejected is not None and rejected[0] == "RELATIVE_STRENGTH"


def test_profile_only_changes_core_sqz(edge_config):
    dyn = sqz(strategy="SQUEEZE_BREAKOUT_DYNAMIC")
    assert _order_flow_entry_rejection_reason(dyn, edge_config) is not None


def test_unknown_profile_rejected_by_config_validation(tmp_path):
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    text = text.replace("squeeze_admission_profile: \"edge_v1\"", "squeeze_admission_profile: \"yolo\"")
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path).validate()


def test_research_callers_keep_legacy_semantics(edge_config):
    # frozen cohorts call the legacy gate directly; profile must not leak in
    assert _legacy_gate(sqz(), edge_config) is not None
