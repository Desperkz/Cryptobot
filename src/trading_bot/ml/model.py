from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_bot.models import Signal


@dataclass(frozen=True)
class MLPrediction:
    allow_trade: bool
    confidence: Decimal
    reason: str


class MLSignalFilter:
    """Small deterministic ML hook.

    This intentionally does not train online in live mode. A production ML
    pipeline should train offline, validate out-of-sample, then write a versioned
    model artifact consumed here.
    """

    def __init__(
        self,
        model_path: str,
        min_confidence: Decimal,
        enabled: bool = False,
        training_data_path: str = "data/ml/features.jsonl",
        retrain_min_trades: int = 200,
        decision_min_trades: int = 500,
        enforce_decisions: bool = False,
    ) -> None:
        self.model_path = Path(model_path)
        self.training_data_path = Path(training_data_path)
        self.min_confidence = min_confidence
        self.enabled = enabled
        self.retrain_min_trades = retrain_min_trades
        self.decision_min_trades = decision_min_trades
        self.enforce_decisions = enforce_decisions
        self.weights, self.model_rows = self._load_model()
        self.model_loaded = bool(self.weights)

    def predict(self, signal: Signal) -> MLPrediction:
        if not self.enabled:
            return MLPrediction(True, Decimal("1"), "ML disabled")
        if not self.model_loaded:
            return MLPrediction(True, Decimal("1"), "ML enabled but no validated model is loaded")
        if self.model_rows < self.decision_min_trades:
            return MLPrediction(
                True,
                Decimal("1"),
                f"ML shadow mode: model rows {self.model_rows} < required {self.decision_min_trades}",
            )
        features = _features(signal)
        confidence = Decimal(str(_score_features(self.weights, features)))
        if not self.enforce_decisions:
            verdict = "would allow" if confidence >= self.min_confidence else "would reject"
            return MLPrediction(
                True,
                confidence,
                f"ML shadow-only score: {verdict}; enforcement disabled",
            )
        return MLPrediction(confidence >= self.min_confidence, confidence, "offline model score")

    def record_training_example(self, signal: Signal, realized_r: Decimal) -> None:
        self.training_data_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "features": _features(signal),
            "label": 1 if realized_r > 0 else 0,
            "realized_r": str(realized_r),
            "symbol": signal.symbol,
            "direction": signal.direction.value,
        }
        with self.training_data_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def retrain_from_history(self) -> dict[str, Any]:
        rows = self._load_training_rows()
        return self._train_rows(rows)

    def retrain_from_trades(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        rows = _training_rows_from_trades(trades)
        return self._train_rows(rows)

    def walk_forward_validate_from_trades(
        self,
        trades: list[dict[str, Any]],
        *,
        min_train_rows: int | None = None,
        test_window: int = 50,
    ) -> dict[str, Any]:
        rows = _training_rows_from_trades(trades)
        min_train = min_train_rows or self.retrain_min_trades
        if len(rows) <= min_train:
            return {
                "validated": False,
                "reason": "not enough closed trades for walk-forward validation",
                "rows": len(rows),
                "required_rows": min_train + 1,
            }

        folds: list[dict[str, Any]] = []
        confusion = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        baseline = _empty_performance()
        filtered = _empty_performance()
        window = max(1, int(test_window))
        idx = min_train
        while idx < len(rows):
            train_rows = rows[:idx]
            test_rows = rows[idx : idx + window]
            weights = _train_logistic(train_rows)
            fold_baseline = _empty_performance()
            fold_filtered = _empty_performance()
            allowed = 0
            for row in test_rows:
                confidence = Decimal(str(_score_features(weights, row["features"])))
                allow_trade = confidence >= self.min_confidence
                actual_win = bool(row["label"])
                _add_performance(fold_baseline, row)
                _add_performance(baseline, row)
                if allow_trade:
                    allowed += 1
                    _add_performance(fold_filtered, row)
                    _add_performance(filtered, row)
                    if actual_win:
                        confusion["tp"] += 1
                    else:
                        confusion["fp"] += 1
                elif actual_win:
                    confusion["fn"] += 1
                else:
                    confusion["tn"] += 1
            folds.append(
                {
                    "fold": len(folds) + 1,
                    "train_rows": len(train_rows),
                    "test_rows": len(test_rows),
                    "allowed": allowed,
                    "rejected": len(test_rows) - allowed,
                    "baseline_total_r": str(fold_baseline["total_r"]),
                    "filtered_total_r": str(fold_filtered["total_r"]),
                }
            )
            idx += window

        total_predictions = sum(confusion.values())
        baseline_report = _finalize_performance(baseline)
        filtered_report = _finalize_performance(filtered)
        return {
            "validated": True,
            "rows": len(rows),
            "min_train_rows": min_train,
            "test_window": window,
            "threshold": str(self.min_confidence),
            "folds": folds,
            "confusion": confusion,
            "accuracy": _ratio(confusion["tp"] + confusion["tn"], total_predictions),
            "precision": _ratio(confusion["tp"], confusion["tp"] + confusion["fp"]),
            "recall": _ratio(confusion["tp"], confusion["tp"] + confusion["fn"]),
            "baseline": baseline_report,
            "ml_filtered": filtered_report,
            "comparison": _compare_performance(baseline_report, filtered_report),
        }

    def _train_rows(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if len(rows) < self.retrain_min_trades:
            return {"trained": False, "reason": "not enough training rows", "rows": len(rows)}
        weights = _train_logistic(rows)
        payload = {
            "model_type": "online_logistic_regression",
            "rows": len(rows),
            "weights": weights,
            "feature_importance": _feature_importance(weights),
        }
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        self.model_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self.weights = weights
        self.model_rows = len(rows)
        self.model_loaded = True
        return {"trained": True, "rows": len(rows), "feature_importance": payload["feature_importance"]}

    def feature_importance(self) -> dict[str, float]:
        return _feature_importance(self.weights)

    def features_for_signal(self, signal: Signal) -> dict[str, float]:
        return _features(signal)

    def _load_model(self) -> tuple[dict[str, Any], int]:
        if not self.model_path.exists():
            return {}, 0
        payload = json.loads(self.model_path.read_text(encoding="utf-8"))
        return payload.get("weights", {}), int(payload.get("rows", 0) or 0)

    def _load_training_rows(self) -> list[dict[str, Any]]:
        if not self.training_data_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.training_data_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rows.append(json.loads(line))
        return rows


def _features(signal: Signal) -> dict[str, float]:
    metadata = signal.metadata
    feature_names = [
        "confidence",
        "volume_ratio",
        "spread_bps",
        "atr_pct",
        "rsi",
        "edge_score",
        "aggressive_delta",
        "open_interest_change_pct",
    ]
    features: dict[str, float] = {"confidence": _clip(float(signal.confidence), 0.0, 1.0)}
    for name in feature_names[1:]:
        raw = metadata.get(name)
        if raw in (None, "", "None"):
            continue
        try:
            features[name] = _normalize_feature(name, float(raw))
        except (TypeError, ValueError):
            continue
    return features


def _features_from_trade(trade: dict[str, Any]) -> dict[str, float]:
    raw_metadata = trade.get("metadata") or {}
    if isinstance(raw_metadata, str):
        try:
            raw_metadata = json.loads(raw_metadata)
        except json.JSONDecodeError:
            raw_metadata = {}
    metadata = raw_metadata.get("signal_metadata", raw_metadata)
    features: dict[str, float] = {}
    confidence = metadata.get("confidence")
    if confidence not in (None, "", "None"):
        try:
            features["confidence"] = _clip(float(confidence), 0.0, 1.0)
        except (TypeError, ValueError):
            pass
    for name in [
        "volume_ratio",
        "spread_bps",
        "atr_pct",
        "rsi",
        "edge_score",
        "aggressive_delta",
        "open_interest_change_pct",
    ]:
        raw = metadata.get(name)
        if raw in (None, "", "None"):
            continue
        try:
            features[name] = _normalize_feature(name, float(raw))
        except (TypeError, ValueError):
            continue
    return features


def _training_rows_from_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade in sorted(trades, key=_trade_sort_key):
        if str(trade.get("status", "")).upper() != "CLOSED":
            continue
        features = _features_from_trade(trade)
        if not features:
            continue
        realized_pnl = _decimal_field(trade, "realized_pnl")
        rows.append(
            {
                "features": features,
                "label": 1 if realized_pnl > 0 else 0,
                "realized_pnl": realized_pnl,
                "realized_r": _decimal_field(trade, "r_multiple"),
            }
        )
    return rows


def _trade_sort_key(trade: dict[str, Any]) -> int:
    try:
        return int(trade.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _decimal_field(row: dict[str, Any], key: str) -> Decimal:
    return Decimal(str(row.get(key, "0") or "0"))


def _score_features(weights: dict[str, Any], features: dict[str, float]) -> float:
    z = float(weights.get("bias", 0.0))
    for key, value in features.items():
        z += float(weights.get(key, 0.0)) * value
    return 1 / (1 + math.exp(-max(min(z, 35), -35)))


def _empty_performance() -> dict[str, Any]:
    return {"trades": 0, "wins": 0, "total_pnl": Decimal("0"), "total_r": Decimal("0")}


def _add_performance(performance: dict[str, Any], row: dict[str, Any]) -> None:
    performance["trades"] += 1
    performance["wins"] += int(row["label"])
    performance["total_pnl"] += row["realized_pnl"]
    performance["total_r"] += row["realized_r"]


def _finalize_performance(performance: dict[str, Any]) -> dict[str, Any]:
    trades = int(performance["trades"])
    total_r = performance["total_r"]
    return {
        "trades": trades,
        "wins": int(performance["wins"]),
        "winrate": _ratio(performance["wins"], trades),
        "total_pnl": str(performance["total_pnl"]),
        "total_r": str(total_r),
        "avg_r": str(total_r / Decimal(trades)) if trades else "0",
    }


def _compare_performance(baseline: dict[str, Any], filtered: dict[str, Any]) -> dict[str, Any]:
    baseline_total_r = Decimal(str(baseline["total_r"]))
    filtered_total_r = Decimal(str(filtered["total_r"]))
    baseline_avg_r = Decimal(str(baseline["avg_r"]))
    filtered_avg_r = Decimal(str(filtered["avg_r"]))
    baseline_trades = int(baseline["trades"])
    filtered_trades = int(filtered["trades"])
    coverage = _ratio(filtered_trades, baseline_trades)
    return {
        "total_r_delta": str(filtered_total_r - baseline_total_r),
        "avg_r_delta": str(filtered_avg_r - baseline_avg_r),
        "trade_coverage": coverage,
        "ml_improved_baseline": (
            filtered_trades > 0
            and filtered_total_r >= baseline_total_r
            and filtered_avg_r >= baseline_avg_r
        ),
    }


def _ratio(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def _train_logistic(rows: list[dict[str, Any]], learning_rate: float = 0.03, epochs: int = 120) -> dict[str, float]:
    weights: dict[str, float] = {"bias": 0.0}
    for row in rows:
        for key in row.get("features", {}):
            weights.setdefault(key, 0.0)

    for _ in range(epochs):
        for row in rows:
            features = row.get("features", {})
            y = float(row.get("label", 0))
            z = weights["bias"] + sum(weights.get(key, 0.0) * float(value) for key, value in features.items())
            pred = 1 / (1 + math.exp(-max(min(z, 35), -35)))
            error = y - pred
            weights["bias"] += learning_rate * error
            for key, value in features.items():
                weights[key] = weights.get(key, 0.0) + learning_rate * error * float(value)
    return weights


def _feature_importance(weights: dict[str, Any]) -> dict[str, float]:
    return {
        key: abs(float(value))
        for key, value in sorted(weights.items(), key=lambda item: abs(float(item[1])), reverse=True)
        if key != "bias"
    }


def _normalize_feature(name: str, value: float) -> float:
    if name == "volume_ratio":
        return _clip(value / 5.0, 0.0, 3.0)
    if name == "spread_bps":
        return _clip(value / 20.0, 0.0, 5.0)
    if name == "atr_pct":
        return _clip(value / 10.0, 0.0, 2.0)
    if name == "rsi":
        return _clip((value - 50.0) / 50.0, -1.0, 1.0)
    if name == "open_interest_change_pct":
        return _clip(value / 10.0, -3.0, 3.0)
    if name in {"edge_score", "aggressive_delta"}:
        return _clip(value, -1.0, 1.0)
    return value


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))
