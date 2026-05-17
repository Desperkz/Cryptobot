from __future__ import annotations

import logging
import time
from decimal import Decimal

from trading_bot.config import AppConfig
from trading_bot.data_provider import BinanceUSDMClient
from trading_bot.models import Direction, OrderSide, Position, ProtectionStatus, RiskPlan, TakeProfitTarget
from trading_bot.order_manager.formatting import format_decimal


logger = logging.getLogger(__name__)


class ProtectionManager:
    """Manage post-entry protection: breakeven and trailing.

    In live mode Binance conditional orders are reconciled through REST; a full
    production deployment should pair this with user-data-stream events.
    """

    def __init__(self, config: AppConfig, binance: BinanceUSDMClient | None = None) -> None:
        self.config = config
        self.binance = binance

    def should_move_to_breakeven(self, plan: RiskPlan, filled_target: TakeProfitTarget) -> bool:
        return bool(plan.protection and filled_target.move_stop_to_breakeven)

    async def move_stop_to_breakeven(
        self,
        position: Position,
        plan: RiskPlan,
        client_order_id: str | None = None,
    ) -> dict | None:
        if not self.binance or not plan.protection:
            return None
        await self._cancel_stop_orders(position.symbol)
        side = OrderSide.SELL if position.direction == Direction.LONG else OrderSide.BUY
        return await self.binance.new_order(
            symbol=position.symbol,
            side=side.value,
            type="STOP_MARKET",
            stopPrice=format_decimal(plan.protection.breakeven_price),
            closePosition="true",
            workingType=self.config.trading.order_working_type,
            newClientOrderId=client_order_id or _fallback_client_order_id(position.symbol, "slbe"),
        )

    async def activate_trailing_stop(
        self,
        position: Position,
        plan: RiskPlan,
        remaining_quantity: Decimal,
        client_order_id: str | None = None,
    ) -> dict | None:
        if not self.binance or not plan.protection or not plan.protection.trailing_enabled:
            return None
        if remaining_quantity <= 0:
            return None
        await self._cancel_stop_orders(position.symbol)
        side = OrderSide.SELL if position.direction == Direction.LONG else OrderSide.BUY
        return await self.binance.new_order(
            symbol=position.symbol,
            side=side.value,
            type="TRAILING_STOP_MARKET",
            quantity=format_decimal(remaining_quantity),
            reduceOnly="true",
            callbackRate=format_decimal(plan.protection.trailing_callback_rate_pct),
            workingType=self.config.trading.order_working_type,
            newClientOrderId=client_order_id or _fallback_client_order_id(position.symbol, "trail"),
        )

    def local_trailing_stop_price(
        self,
        position: Position,
        highest_price: Decimal,
        lowest_price: Decimal,
        callback_rate_pct: Decimal,
    ) -> Decimal:
        rate = callback_rate_pct / Decimal("100")
        if position.direction == Direction.LONG:
            return highest_price * (Decimal("1") - rate)
        return lowest_price * (Decimal("1") + rate)

    async def _cancel_stop_orders(self, symbol: str) -> None:
        if not self.binance:
            return
        try:
            orders = await self.binance.open_orders(symbol)
        except Exception:
            logger.exception("Could not fetch open orders before stop replacement for %s", symbol)
            raise
        for order in orders:
            if order.get("type") in {"STOP_MARKET", "TRAILING_STOP_MARKET"}:
                await self.binance.cancel_order(symbol=symbol, order_id=int(order["orderId"]))


def protection_status_from_orders(orders: list[dict]) -> ProtectionStatus:
    if any(order.get("type") == "TRAILING_STOP_MARKET" for order in orders):
        return ProtectionStatus.TRAILING
    if any(order.get("type") == "STOP_MARKET" for order in orders):
        return ProtectionStatus.PLACED
    return ProtectionStatus.PENDING


def _fallback_client_order_id(symbol: str, suffix: str) -> str:
    timestamp = int(time.time() * 1000) % 1_000_000_000
    return f"bot-{symbol[:12]}-{timestamp}-{suffix}"[:36]
