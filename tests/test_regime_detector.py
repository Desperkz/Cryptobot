"""Тесты счётчика вердиктов режимного детектора."""
from __future__ import annotations

from pathlib import Path

from trading_bot.config import load_config
from trading_bot.market_regime_detector import MarketRegimeDetector

ROOT = Path(__file__).parent.parent


def test_detector_accumulates_and_pops_verdict_summary() -> None:
    config = load_config(ROOT / "config.yaml", ROOT / ".env.example")
    detector = MarketRegimeDetector(config.strategy)

    detector.detect([])  # UNKNOWN: недостаточно 4h-данных
    detector.detect([])
    summary = detector.pop_verdict_summary()
    assert summary.get("UNKNOWN") == 2
    # после pop счётчик обнулён
    assert detector.pop_verdict_summary() == {}
