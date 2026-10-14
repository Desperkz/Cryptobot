"""Re-anchor a strategy signal to the executable market price.

Strategies build their plan from the close of the last *closed* candle
(1h for SQZ).  The bot polls every minute and a signal can pass its gates
many minutes after that close.  Previously paper trades were "filled" at
that stale close (+ fixed slippage) and live sizing used it as the entry,
while the real MARKET order filled elsewhere.  Both distort R:

* paper statistics were measured from a price that was no longer available;
* live position size was computed for a stop distance that did not exist.

``rebase_signal_to_market`` keeps the strategy's stop/target *distances*
(they are ATR-derived) and moves them to the current mid price.  When the
market has already moved too far from the signal price the entry is
rejected instead of chasing it.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from trading_bot.models import Direction, Signal, to_decimal


def rebase_signal_to_market(
    signal: Signal,
    market_price: Decimal | None,
    max_drift_r: Decimal,
) -> tuple[Signal | None, str | None]:
    """Return ``(rebased_signal, None)`` or ``(None, rejection_reason)``.

    ``max_drift_r`` is the largest allowed |market - signal entry| expressed
    in units of the original stop distance.  ``0`` disables the drift guard.
    """
    if market_price is None:
        return None, "current market price unavailable; refusing stale-price entry"
    price = to_decimal(market_price)
    if price <= 0:
        return None, f"invalid current market price {price}"
    if signal.stop_loss is None or signal.take_profit is None:
        return None, "signal has no stop-loss/take-profit to rebase"

    entry = to_decimal(signal.entry_price)
    stop = to_decimal(signal.stop_loss)
    take = to_decimal(signal.take_profit)
    stop_distance = abs(entry - stop)
    take_distance = abs(take - entry)
    if stop_distance <= 0 or take_distance <= 0:
        return None, "signal has non-positive stop/target distance"

    drift = price - entry
    if signal.direction == Direction.SHORT:
        drift = -drift  # positive drift = market already moved in trade direction
    drift_r = drift / stop_distance
    if max_drift_r > 0 and abs(drift_r) > max_drift_r:
        side = "ahead of" if drift_r > 0 else "against"
        return None, (
            f"market moved {abs(drift_r):.2f}R {side} the signal price "
            f"(signal {entry}, now {price}); max allowed {max_drift_r}R"
        )

    if signal.direction == Direction.LONG:
        new_stop = price - stop_distance
        new_take = price + take_distance
    elif signal.direction == Direction.SHORT:
        new_stop = price + stop_distance
        new_take = price - take_distance
    else:
        return None, "signal direction is not tradeable"
    if new_stop <= 0 or new_take <= 0:
        return None, "rebased stop/target is not positive"

    metadata = dict(signal.metadata or {})
    metadata["entry_rebase"] = {
        "signal_entry": str(entry),
        "market_price": str(price),
        "drift_r": str(drift_r.quantize(Decimal("0.0001"))),
        "original_stop": str(stop),
        "original_take_profit": str(take),
    }
    return (
        replace(
            signal,
            entry_price=price,
            stop_loss=new_stop,
            take_profit=new_take,
            metadata=metadata,
        ),
        None,
    )
