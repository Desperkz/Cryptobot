from __future__ import annotations

import time
from decimal import Decimal

from trading_bot.data_provider.binance_usdm import BinanceUSDMClient
from trading_bot.models import Candle, MarketMetrics, to_decimal


class MarketDataProvider:
    def __init__(self, binance: BinanceUSDMClient) -> None:
        self.binance = binance

    async def candles(self, symbol: str, interval: str, limit: int = 500) -> list[Candle]:
        raw = await self.binance.klines(symbol=symbol, interval=interval, limit=limit)
        candles = [Candle.from_binance(row) for row in raw]
        now_ms = int(time.time() * 1000)
        if candles and candles[-1].close_time > now_ms:
            candles.pop()
        return candles

    async def symbol_metrics(self, symbol: str) -> MarketMetrics:
        book = await self.binance.book_ticker(symbol)
        ticker = await self.binance.ticker_24h(symbol)
        depth = await self.binance.depth(symbol, limit=5)

        bid = to_decimal(book["bidPrice"])
        ask = to_decimal(book["askPrice"])
        mid = (bid + ask) / Decimal("2") if bid and ask else Decimal("0")
        spread_bps = ((ask - bid) / mid * Decimal("10000")) if mid > 0 else Decimal("999999")

        top_book_liquidity = Decimal("0")
        bid_notional = Decimal("0")
        ask_notional = Decimal("0")
        for price, qty in depth.get("bids", [])[:5]:
            value = to_decimal(price) * to_decimal(qty)
            bid_notional += value
            top_book_liquidity += value
        for price, qty in depth.get("asks", [])[:5]:
            value = to_decimal(price) * to_decimal(qty)
            ask_notional += value
            top_book_liquidity += value
        book_total = bid_notional + ask_notional
        order_book_imbalance = (bid_notional - ask_notional) / book_total if book_total > 0 else Decimal("0")

        funding_rate: Decimal | None = None
        funding = await self.binance.funding_rate(symbol, limit=1)
        if funding:
            funding_rate = to_decimal(funding[-1]["fundingRate"])

        open_interest: Decimal | None = None
        oi = await self.binance.open_interest(symbol)
        if oi.get("openInterest") is not None:
            open_interest = to_decimal(oi["openInterest"])

        taker_buy_ratio = await self._taker_buy_ratio(symbol)
        aggressive_delta = Decimal("0")
        if taker_buy_ratio is not None:
            aggressive_delta = (taker_buy_ratio - Decimal("0.5")) * Decimal("2")
        open_interest_change_pct = await self._open_interest_change_pct(symbol)

        return MarketMetrics(
            symbol=symbol,
            quote_volume_24h=to_decimal(ticker.get("quoteVolume", "0")),
            spread_bps=spread_bps,
            top_book_liquidity_usdt=top_book_liquidity,
            funding_rate=funding_rate,
            open_interest=open_interest,
            order_book_imbalance=order_book_imbalance,
            taker_buy_ratio=taker_buy_ratio,
            open_interest_change_pct=open_interest_change_pct,
            aggressive_buy_sell_delta=aggressive_delta,
        )

    async def _taker_buy_ratio(self, symbol: str) -> Decimal | None:
        try:
            trades = await self.binance.agg_trades(symbol, limit=500)
        except Exception:
            return None
        buy_qty = Decimal("0")
        sell_qty = Decimal("0")
        for trade in trades:
            qty = to_decimal(trade.get("q", "0"))
            if trade.get("m") is True:
                sell_qty += qty
            else:
                buy_qty += qty
        total = buy_qty + sell_qty
        return buy_qty / total if total > 0 else None

    async def _open_interest_change_pct(self, symbol: str) -> Decimal | None:
        try:
            history = await self.binance.open_interest_hist(symbol, period="15m", limit=2)
        except Exception:
            return None
        if len(history) < 2:
            return None
        previous = to_decimal(history[0].get("sumOpenInterest", "0"))
        current = to_decimal(history[-1].get("sumOpenInterest", "0"))
        if previous <= 0:
            return None
        return (current - previous) / previous * Decimal("100")
