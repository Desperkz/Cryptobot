from __future__ import annotations

from decimal import Decimal

from trading_bot.config import EdgeFilterConfig
from trading_bot.models import Candle, Direction, MarketMetrics
from trading_bot.strategy_engine.order_flow import OrderFlowAnnotator


def cfg() -> EdgeFilterConfig:
    return EdgeFilterConfig(
        enabled=True,
        order_book_imbalance_min=Decimal("0.08"),
        taker_buy_ratio_long_min=Decimal("0.53"),
        taker_buy_ratio_short_max=Decimal("0.47"),
        open_interest_change_min_pct=Decimal("0.10"),
        liquidity_sweep_lookback=12,
        liquidity_sweep_threshold_bps=Decimal("12"),
        absorption_wick_body_ratio=Decimal("1.8"),
        aggressive_flow_delta_min=Decimal("0.08"),
        structure_break_lookback=20,
        liquidation_zone_distance_bps=Decimal("35"),
        liquidation_cluster_filter_enabled=False,
    )


def candle(i: int, open_: str, high: str, low: str, close: str, volume: str = "1000") -> Candle:
    return Candle(
        open_time=i,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        close_time=i + 1,
        quote_volume=Decimal(volume) * Decimal(close),
    )


def base_candles(last: Candle | None = None) -> list[Candle]:
    candles = [
        candle(i, "99.8", "100.30", "98.90", "100.00")
        for i in range(24)
    ]
    candles.append(last or candle(24, "99.80", "100.20", "99.40", "100.00", "1800"))
    return candles


def metrics(**overrides) -> MarketMetrics:
    values = {
        "symbol": "BTCUSDT",
        "quote_volume_24h": Decimal("100000000"),
        "spread_bps": Decimal("2"),
        "top_book_liquidity_usdt": Decimal("5000000"),
        "funding_rate": Decimal("0"),
        "open_interest": Decimal("100000000"),
        "order_book_imbalance": Decimal("0"),
        "taker_buy_ratio": Decimal("0.5"),
        "open_interest_change_pct": Decimal("0"),
        "aggressive_buy_sell_delta": Decimal("0"),
    }
    values.update(overrides)
    return MarketMetrics(**values)


def test_order_flow_annotation_marks_aligned_long_flow() -> None:
    annotation = OrderFlowAnnotator(cfg()).annotate(
        base_candles(),
        Direction.LONG,
        metrics(
            taker_buy_ratio=Decimal("0.61"),
            order_book_imbalance=Decimal("0.16"),
            aggressive_buy_sell_delta=Decimal("0.12"),
            open_interest_change_pct=Decimal("0.42"),
        ),
    )

    assert annotation.flow_bias == Direction.LONG
    assert annotation.alignment == "aligned"
    assert annotation.score >= Decimal("0.80")
    assert annotation.liquidity_side == "upside"
    assert "taker_flow_aligned" in annotation.reasons
    assert "book_imbalance_aligned" in annotation.reasons
    assert "aggressive_delta_aligned" in annotation.reasons
    assert "open_interest_expansion" in annotation.reasons
    assert not annotation.risk_flags


def test_order_flow_annotation_flags_adverse_flow_and_liquidation_cascade() -> None:
    annotation = OrderFlowAnnotator(cfg()).annotate(
        base_candles(),
        Direction.LONG,
        metrics(
            taker_buy_ratio=Decimal("0.42"),
            order_book_imbalance=Decimal("-0.14"),
            aggressive_buy_sell_delta=Decimal("-0.18"),
            open_interest_change_pct=Decimal("-0.35"),
            funding_rate=Decimal("0.0008"),
        ),
    )

    assert annotation.flow_bias == Direction.SHORT
    assert annotation.alignment == "against"
    assert "taker_flow_against" in annotation.risk_flags
    assert "book_imbalance_against" in annotation.risk_flags
    assert "aggressive_delta_against" in annotation.risk_flags
    assert "liquidation_cascade" in annotation.risk_flags
    assert "crowded_long_funding" in annotation.risk_flags


def test_order_flow_annotation_detects_absorption_and_downside_liquidity() -> None:
    near_downside = [
        candle(i, "99.90", "100.30", "99.60", "99.90")
        for i in range(24)
    ]
    last = candle(24, "100.00", "100.60", "99.70", "99.90", "1800")
    annotation = OrderFlowAnnotator(cfg()).annotate(
        [*near_downside, last],
        Direction.SHORT,
        metrics(
            taker_buy_ratio=Decimal("0.39"),
            order_book_imbalance=Decimal("-0.12"),
            aggressive_buy_sell_delta=Decimal("-0.10"),
            open_interest_change_pct=Decimal("0.20"),
        ),
    )

    assert annotation.alignment == "aligned"
    assert annotation.liquidity_side == "downside"
    assert annotation.absorption_direction == Direction.SHORT
    assert "absorption_aligned" in annotation.reasons
    assert "target_liquidity_nearby" in annotation.reasons
