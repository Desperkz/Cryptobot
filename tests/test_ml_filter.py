from __future__ import annotations

import json
from decimal import Decimal

from trading_bot.ml.model import MLSignalFilter
from trading_bot.models import Direction, Signal, TradingStyle


def _signal() -> Signal:
    return Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        confidence=Decimal("0.75"),
        reason="test",
        metadata={"volume_ratio": "2.0", "rsi": "60", "atr_pct": "1.2"},
    )


def test_enabled_ml_fails_open_without_model(tmp_path) -> None:
    model = MLSignalFilter(
        model_path=str(tmp_path / "missing.json"),
        min_confidence=Decimal("0.60"),
        enabled=True,
        retrain_min_trades=500,
    )

    prediction = model.predict(_signal())

    assert prediction.allow_trade is True
    assert "no validated model" in prediction.reason


def test_enabled_ml_stays_shadow_until_minimum_model_rows(tmp_path) -> None:
    model_path = tmp_path / "model.json"
    model_path.write_text(
        json.dumps({"rows": 100, "weights": {"bias": -10.0, "confidence": -10.0}}),
        encoding="utf-8",
    )
    model = MLSignalFilter(
        model_path=str(model_path),
        min_confidence=Decimal("0.60"),
        enabled=True,
        retrain_min_trades=500,
        decision_min_trades=500,
    )

    prediction = model.predict(_signal())

    assert prediction.allow_trade is True
    assert "shadow mode" in prediction.reason


def test_validated_ml_model_scores_shadow_only_unless_enforced(tmp_path) -> None:
    model_path = tmp_path / "model.json"
    model_path.write_text(
        json.dumps({"rows": 600, "weights": {"bias": -10.0, "confidence": -10.0}}),
        encoding="utf-8",
    )
    model = MLSignalFilter(
        model_path=str(model_path),
        min_confidence=Decimal("0.60"),
        enabled=True,
        decision_min_trades=500,
        enforce_decisions=False,
    )

    prediction = model.predict(_signal())

    assert prediction.allow_trade is True
    assert prediction.confidence < Decimal("0.60")
    assert "shadow-only score" in prediction.reason
    assert "would reject" in prediction.reason


def test_validated_ml_model_can_only_block_when_enforcement_enabled(tmp_path) -> None:
    model_path = tmp_path / "model.json"
    model_path.write_text(
        json.dumps({"rows": 600, "weights": {"bias": -10.0, "confidence": -10.0}}),
        encoding="utf-8",
    )
    model = MLSignalFilter(
        model_path=str(model_path),
        min_confidence=Decimal("0.60"),
        enabled=True,
        decision_min_trades=500,
        enforce_decisions=True,
    )

    prediction = model.predict(_signal())

    assert prediction.allow_trade is False
    assert prediction.confidence < Decimal("0.60")


def test_walk_forward_validation_reports_baseline_and_filtered_metrics(tmp_path) -> None:
    model = MLSignalFilter(
        model_path=str(tmp_path / "model.json"),
        min_confidence=Decimal("0.55"),
        enabled=True,
        retrain_min_trades=5,
        decision_min_trades=5,
    )
    trades = [
        {
            "id": idx,
            "status": "CLOSED",
            "realized_pnl": "1" if idx % 2 else "-1",
            "r_multiple": "1" if idx % 2 else "-1",
            "metadata": json.dumps({"signal_metadata": {"confidence": "0.7", "volume_ratio": str(1 + idx / 10)}}),
        }
        for idx in range(1, 13)
    ]

    report = model.walk_forward_validate_from_trades(trades, min_train_rows=5, test_window=3)

    assert report["validated"] is True
    assert report["baseline"]["trades"] == 7
    assert "ml_filtered" in report
    assert "comparison" in report
    assert report["folds"]
