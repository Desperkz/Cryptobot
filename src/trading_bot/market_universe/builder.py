from __future__ import annotations

import logging
import time
from decimal import Decimal

from trading_bot.config import UniverseConfig
from trading_bot.data_provider import BinanceUSDMClient, CoinGeckoClient, MarketDataProvider
from trading_bot.models import SymbolFilters, UniverseAsset, to_decimal


logger = logging.getLogger(__name__)


class MarketUniverseBuilder:
    def __init__(
        self,
        config: UniverseConfig,
        binance: BinanceUSDMClient,
        coingecko: CoinGeckoClient,
        market_data: MarketDataProvider,
    ) -> None:
        self.config = config
        self.binance = binance
        self.coingecko = coingecko
        self.market_data = market_data
        self._cache: tuple[float, list[UniverseAsset]] | None = None

    async def build(self, force_refresh: bool = False) -> list[UniverseAsset]:
        if self._cache and not force_refresh:
            created_at, assets = self._cache
            if time.time() - created_at < self.config.cache_ttl_sec:
                return assets

        coins = await self.coingecko.top_markets(per_page=self.config.fetch_extra_top_n)
        exchange_info = await self.binance.exchange_info()
        tradable = {
            item["baseAsset"].upper(): item
            for item in exchange_info.get("symbols", [])
            if item.get("contractType") == "PERPETUAL"
            and item.get("status") == "TRADING"
            and item.get("quoteAsset") == self.config.quote_asset
        }

        selected: list[UniverseAsset] = []
        for coin in coins:
            base = str(coin.get("symbol", "")).upper()
            if not base or base in self.config.excluded_assets:
                continue
            info = tradable.get(base)
            if not info:
                continue
            symbol = info["symbol"]
            if symbol.upper() in self.config.excluded_assets:
                continue
            try:
                metrics = await self.market_data.symbol_metrics(symbol)
            except Exception as exc:
                logger.warning("Skipping %s: metrics fetch failed: %s", symbol, exc)
                continue

            quality_score = self._quality_score(
                metrics.quote_volume_24h,
                metrics.spread_bps,
                metrics.top_book_liquidity_usdt,
            )
            if (
                not self._passes_filters(metrics.quote_volume_24h, metrics.spread_bps, metrics.top_book_liquidity_usdt)
                or quality_score < self.config.min_symbol_quality_score
            ):
                logger.info(
                    "Skipping %s: quality=%s min_quality=%s volume=%s spread_bps=%s book_liq=%s",
                    symbol,
                    quality_score,
                    self.config.min_symbol_quality_score,
                    metrics.quote_volume_24h,
                    metrics.spread_bps,
                    metrics.top_book_liquidity_usdt,
                )
                continue

            selected.append(
                UniverseAsset(
                    symbol=symbol,
                    base_asset=base,
                    quote_asset=self.config.quote_asset,
                    market_cap_rank=int(coin.get("market_cap_rank") or 0),
                    market_cap_usd=to_decimal(coin["market_cap"]) if coin.get("market_cap") is not None else None,
                    filters=SymbolFilters.from_exchange_symbol(info),
                    metrics=metrics,
                )
            )
            if len(selected) >= self.config.market_cap_top_n:
                break

        self._cache = (time.time(), selected)
        return selected

    def _passes_filters(self, quote_volume: Decimal, spread_bps: Decimal, book_liquidity: Decimal) -> bool:
        return (
            quote_volume >= self.config.min_24h_quote_volume_usdt
            and spread_bps <= self.config.max_spread_bps
            and book_liquidity >= self.config.min_order_book_top_liquidity_usdt
        )

    def _quality_score(self, quote_volume: Decimal, spread_bps: Decimal, book_liquidity: Decimal) -> Decimal:
        spread_score = self._inverse_score(spread_bps, self.config.max_spread_bps)
        liquidity_score = self._target_score(book_liquidity, self.config.min_order_book_top_liquidity_usdt)
        volume_score = self._target_score(quote_volume, self.config.min_24h_quote_volume_usdt)

        weighted_total = (
            spread_score * self.config.quality_spread_weight
            + liquidity_score * self.config.quality_liquidity_weight
            + volume_score * self.config.quality_volume_weight
        )
        weight_total = (
            self.config.quality_spread_weight
            + self.config.quality_liquidity_weight
            + self.config.quality_volume_weight
        )
        if weight_total <= 0:
            return Decimal("0")
        return (weighted_total / weight_total).quantize(Decimal("0.01"))

    @staticmethod
    def _target_score(value: Decimal, target: Decimal) -> Decimal:
        if target <= 0:
            return Decimal("100")
        return min(Decimal("100"), max(Decimal("0"), value / target * Decimal("100")))

    @staticmethod
    def _inverse_score(value: Decimal, limit: Decimal) -> Decimal:
        if limit <= 0:
            return Decimal("100")
        return min(Decimal("100"), max(Decimal("0"), (Decimal("1") - value / limit) * Decimal("100")))
