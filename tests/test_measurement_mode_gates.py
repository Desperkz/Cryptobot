"""Tests for the measurement-mode fixes.

These cover the three changes that make the system's edge measurable at all:
configurable order-flow thresholds, the measure/off gate mode, and the
walk-forward evidence requirement that replaced a bare boolean flag.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from types import SimpleNamespace

from trading_bot.bot import _order_flow_entry_rejection_reason
from trading_bot.config import AppConfig, ConfigError
from trading_bot.models import Direction, Signal, TradingStyle


def _signal(*, score: str, alignment: str, risk_flags: list[str] | None = None) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        take_profit=Decimal("104"),
        confidence=Decimal("0.8"),
        reason="test",
        metadata={
            "strategy": "SQUEEZE_BREAKOUT",
            "order_flow": {
                "alignment": alignment,
                "score": score,
                "risk_flags": risk_flags or [],
                "reasons": [],
            },
        },
    )


class _StrategyStub:
    """Minimal stand-in exposing only the fields the gate reads."""

    def __init__(self, hostile: str, mixed: str) -> None:
        self.order_flow_hostile_score_floor = Decimal(hostile)
        self.order_flow_mixed_score_floor = Decimal(mixed)


def test_mixed_score_floor_comes_from_config() -> None:
    """The 0.45 threshold used to be hardcoded and therefore untestable."""
    signal = _signal(score="0.50", alignment="mixed")

    strict = _StrategyStub(hostile="0.70", mixed="0.60")
    lenient = _StrategyStub(hostile="0.70", mixed="0.30")

    rejection = _order_flow_entry_rejection_reason(signal, strict)
    assert rejection is not None and rejection[0] == "ORDER_FLOW"

    # Same signal, lower configured floor -> no order-flow rejection.
    result = _order_flow_entry_rejection_reason(signal, lenient)
    assert result is None or result[0] != "ORDER_FLOW"


def test_hostile_score_floor_comes_from_config() -> None:
    signal = _signal(score="0.65", alignment="aligned", risk_flags=["taker_flow_against"])

    assert _order_flow_entry_rejection_reason(signal, _StrategyStub("0.70", "0.45")) is not None

    relaxed = _order_flow_entry_rejection_reason(signal, _StrategyStub("0.60", "0.45"))
    assert relaxed is None or relaxed[0] != "ORDER_FLOW"


def test_gate_defaults_are_unchanged_without_config() -> None:
    """Callers that pass no config must keep the historical 0.70/0.45 behaviour."""
    assert _order_flow_entry_rejection_reason(_signal(score="0.40", alignment="mixed")) is not None
    borderline = _order_flow_entry_rejection_reason(_signal(score="0.50", alignment="mixed"))
    assert borderline is None or borderline[0] != "ORDER_FLOW"


# --- walk-forward mainnet gate ------------------------------------------


class _SafetyStub:
    def __init__(self, path: str) -> None:
        self.walkforward_report_path = path
        self.walkforward_max_report_age_days = 45
        self.min_walkforward_out_of_sample_trades = 150
        self.min_walkforward_profit_factor = Decimal("1.20")
        self.min_walkforward_expectancy_r = Decimal("0.10")
        self.mainnet_allowed_strategies = ["SQUEEZE_BREAKOUT"]


class _ConfigStub:
    """Binds the real validator to minimal stubs so it is exercised directly."""

    _validate_walkforward_report = AppConfig._validate_walkforward_report

    def __init__(self, path: str, enabled: list[str] | None = None) -> None:
        self.safety = _SafetyStub(path)
        self.strategy = SimpleNamespace(enabled_strategies=enabled or ["SQUEEZE_BREAKOUT"])


def _write(tmp_path, report: dict | None):
    path = tmp_path / "walkforward_report.json"
    if report is not None:
        path.write_text(json.dumps(report), encoding="utf-8")
    return _ConfigStub(str(path))


def _report(**overrides) -> dict:
    base = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategies": ["SQUEEZE_BREAKOUT"],
        "out_of_sample": {
            "trades": 200,
            "profit_factor": "1.35",
            "expectancy_r": "0.18",
            "expectancy_r_ci_low": "0.04",
        },
    }
    base.update(overrides)
    return base


def test_valid_report_passes(tmp_path) -> None:
    _write(tmp_path, _report())._validate_walkforward_report()


def test_missing_report_blocks_mainnet(tmp_path) -> None:
    with pytest.raises(ConfigError, match="missing walk-forward report"):
        _write(tmp_path, None)._validate_walkforward_report()


def test_stale_report_blocks_mainnet(tmp_path) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    with pytest.raises(ConfigError, match="days old"):
        _write(tmp_path, _report(generated_at=old))._validate_walkforward_report()


def test_uncovered_strategy_blocks_mainnet(tmp_path) -> None:
    """The report must cover the strategies that can actually open a position."""
    report = _report(strategies=["TREND_PULLBACK"])
    with pytest.raises(ConfigError, match="does not cover live strategies"):
        _write(tmp_path, report)._validate_walkforward_report()


def test_thin_sample_blocks_mainnet(tmp_path) -> None:
    report = _report()
    report["out_of_sample"]["trades"] = 40
    with pytest.raises(ConfigError, match="out-of-sample walk-forward trades"):
        _write(tmp_path, report)._validate_walkforward_report()


def test_weak_profit_factor_blocks_mainnet(tmp_path) -> None:
    report = _report()
    report["out_of_sample"]["profit_factor"] = "1.05"
    with pytest.raises(ConfigError, match="profit factor"):
        _write(tmp_path, report)._validate_walkforward_report()


def test_edge_indistinguishable_from_zero_blocks_mainnet(tmp_path) -> None:
    """A positive point estimate is not enough; the CI must exclude zero."""
    report = _report()
    report["out_of_sample"]["expectancy_r_ci_low"] = "-0.02"
    with pytest.raises(ConfigError, match="not distinguishable from zero"):
        _write(tmp_path, report)._validate_walkforward_report()


def test_missing_confidence_interval_blocks_mainnet(tmp_path) -> None:
    report = _report()
    del report["out_of_sample"]["expectancy_r_ci_low"]
    with pytest.raises(ConfigError, match="expectancy_r_ci_low"):
        _write(tmp_path, report)._validate_walkforward_report()
