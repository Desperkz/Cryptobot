from __future__ import annotations

from decimal import Decimal

from trading_bot.config import MarketFilterConfig
from trading_bot.market_filters import MarketEntryFilter
from trading_bot.models import Direction, Signal, TradingStyle


def filter_config() -> MarketFilterConfig:
    return MarketFilterConfig(
        btc_4h_drop_filter_enabled=True,
        btc_4h_max_drop_pct=Decimal("-0.03"),
        block_longs_when_btc_weak=True,
        block_all_when_btc_weak=False,
        utc_session_filter_enabled=True,
        avoid_utc_hours={2, 3, 4, 5, 6},
        use_self_learning_filters=True,
    )


def signal(direction: Direction, hour: str = "12") -> Signal:
    return Signal(
        symbol="ETHUSDT",
        direction=direction,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        confidence=Decimal("0.7"),
        reason="test",
        metadata={"hour_utc": hour, "atr_pct": "2.4", "rsi": "60"},
    )


def squeeze_signal(
    confidence: Decimal = Decimal("0.84"),
    hour: str = "0",
    state: str = "release",
) -> Signal:
    item = signal(Direction.LONG, hour=hour)
    return Signal(
        symbol="ZECUSDT",
        direction=item.direction,
        style=item.style,
        entry_price=item.entry_price,
        stop_loss=item.stop_loss,
        take_profit=item.take_profit,
        confidence=confidence,
        reason=item.reason,
        metadata={
            **item.metadata,
            "strategy": "SQUEEZE_BREAKOUT",
            "squeeze_state": state,
        },
    )


def test_btc_drop_blocks_longs_but_not_shorts() -> None:
    entry_filter = MarketEntryFilter(filter_config())

    assert entry_filter.allow_signal(signal(Direction.LONG), Decimal("-0.04")).allowed is False
    assert entry_filter.allow_signal(signal(Direction.SHORT), Decimal("-0.04")).allowed is True


def test_utc_session_filter_blocks_avoid_hours() -> None:
    entry_filter = MarketEntryFilter(filter_config())

    assert entry_filter.allow_signal(signal(Direction.LONG, hour="3"), Decimal("0.01")).allowed is False
    assert entry_filter.allow_signal(signal(Direction.LONG, hour="0"), Decimal("0.01")).allowed is True
    assert entry_filter.allow_signal(signal(Direction.LONG, hour="12"), Decimal("0.01")).allowed is True


def test_high_confidence_squeeze_release_can_override_early_session_filter() -> None:
    config = filter_config()
    config = MarketFilterConfig(
        **{
            **config.__dict__,
            "high_confidence_squeeze_session_override": True,
            "high_confidence_squeeze_min_confidence": Decimal("0.80"),
            "high_confidence_squeeze_allowed_hours": {0, 1, 2},
        }
    )
    entry_filter = MarketEntryFilter(config)

    assert entry_filter.allow_signal(squeeze_signal(), Decimal("0.01")).allowed is True
    assert entry_filter.allow_signal(squeeze_signal(hour="2"), Decimal("0.01")).allowed is True


def test_squeeze_session_override_keeps_blocking_weak_or_non_release_signals() -> None:
    config = filter_config()
    config = MarketFilterConfig(
        **{
            **config.__dict__,
            "high_confidence_squeeze_session_override": True,
            "high_confidence_squeeze_min_confidence": Decimal("0.80"),
            "high_confidence_squeeze_allowed_hours": {0, 1, 2},
        }
    )
    entry_filter = MarketEntryFilter(config)

    assert (
        entry_filter.allow_signal(squeeze_signal(confidence=Decimal("0.79"), hour="2"), Decimal("0.01")).allowed
        is False
    )
    assert entry_filter.allow_signal(squeeze_signal(state="build", hour="2"), Decimal("0.01")).allowed is False
    assert entry_filter.allow_signal(squeeze_signal(hour="3"), Decimal("0.01")).allowed is False


def test_self_learning_blocks_bad_segment() -> None:
    entry_filter = MarketEntryFilter(filter_config())
    thresholds = {"blocked_symbol_hours": ["ETHUSDT@12UTC"]}

    assert entry_filter.allow_signal(signal(Direction.LONG, hour="12"), Decimal("0.01"), thresholds).allowed is False
