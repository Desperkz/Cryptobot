from __future__ import annotations

import logging
import time
from decimal import Decimal

from trading_bot.config import AppConfig
from trading_bot.data_provider import BinanceAPIError, BinanceUSDMClient
from trading_bot.models import Direction, OrderResult, OrderSide, Position, RiskPlan, TradingMode
from trading_bot.order_manager.formatting import format_decimal
from trading_bot.position_manager import PositionManager


logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, config: AppConfig, binance: BinanceUSDMClient | None, positions: PositionManager) -> None:
        self.config = config
        self.binance = binance
        self.positions = positions

    async def execute(self, plan: RiskPlan) -> OrderResult:
        await self.positions.ensure_symbol_available(plan.symbol)

        if self.config.mode in {TradingMode.DRY_RUN, TradingMode.BACKTEST, TradingMode.PAPER_TRADING}:
            position = Position(
                symbol=plan.symbol,
                direction=plan.direction,
                quantity=plan.quantity,
                entry_price=plan.entry_price,
                stop_loss=plan.stop_loss,
                take_profit=plan.take_profit,
                managed_by_bot=True,
                source=self.config.mode.value,
            )
            if self.config.mode == TradingMode.PAPER_TRADING:
                self.positions.set_local_position(position)
            return OrderResult(
                symbol=plan.symbol,
                mode=self.config.mode,
                accepted=True,
                message=f"{self.config.mode.value}: no exchange order sent; partial exits simulated locally.",
                take_profit_orders=tuple({"name": tp.name, "price": str(tp.price), "quantity": str(tp.quantity)} for tp in plan.partial_take_profits),
            )

        if self.config.mode not in {TradingMode.TESTNET_LIVE, TradingMode.MAINNET_LIVE}:
            raise RuntimeError(f"Unsupported execution mode: {self.config.mode}.")
        if not self.binance:
            raise RuntimeError("Live execution requires Binance client.")

        entry_side = OrderSide.BUY if plan.direction == Direction.LONG else OrderSide.SELL
        exit_side = OrderSide.SELL if plan.direction == Direction.LONG else OrderSide.BUY
        qty = format_decimal(plan.quantity)
        trade_id = _trade_id()
        entry_client_order_id = _client_order_id(plan.symbol, trade_id, "entry")
        stop_client_order_id = _client_order_id(plan.symbol, trade_id, "sl0")
        client_order_ids = {
            "entry": entry_client_order_id,
            "stop": stop_client_order_id,
        }

        try:
            await self.binance.change_leverage(plan.symbol, plan.leverage)
            try:
                await self.binance.change_margin_type(plan.symbol, self.config.trading.margin_type)
            except Exception as exc:
                logger.info("Margin type change skipped for %s: %s", plan.symbol, exc)

            entry_order = await self._new_order_with_recovery(
                client_order_id=entry_client_order_id,
                symbol=plan.symbol,
                side=entry_side.value,
                type="MARKET",
                quantity=qty,
                newOrderRespType="RESULT",
            )
            executed_qty = self._executed_quantity(entry_order)
            if executed_qty is None and entry_order.get("status") == "FILLED" and entry_order.get("origQty"):
                from trading_bot.models import to_decimal

                executed_qty = to_decimal(entry_order["origQty"])
            executed_qty = executed_qty or Decimal("0")
            if executed_qty <= 0:
                raise RuntimeError("Entry order returned zero executed quantity; no protective orders placed.")

            # Close-all stop protects the whole remaining one-way position as TP legs reduce it.
            stop_order = await self._new_order_with_recovery(
                client_order_id=stop_client_order_id,
                symbol=plan.symbol,
                side=exit_side.value,
                type="STOP_MARKET",
                stopPrice=format_decimal(plan.stop_loss),
                closePosition="true",
                workingType=self.config.trading.order_working_type,
            )
            await self._verify_protective_order(plan.symbol, stop_order)

            take_profit_orders: list[dict] = []
            for target in plan.partial_take_profits:
                tp_qty = min(target.quantity, executed_qty)
                if tp_qty <= 0:
                    continue
                target_client_order_id = _client_order_id(plan.symbol, trade_id, target.name.lower())
                client_order_ids[target.name] = target_client_order_id
                order = await self._new_order_with_recovery(
                    client_order_id=target_client_order_id,
                    symbol=plan.symbol,
                    side=exit_side.value,
                    type="TAKE_PROFIT_MARKET",
                    stopPrice=format_decimal(target.price),
                    quantity=format_decimal(tp_qty),
                    reduceOnly="true",
                    workingType=self.config.trading.order_working_type,
                )
                take_profit_orders.append(order)
            return OrderResult(
                symbol=plan.symbol,
                mode=self.config.mode,
                accepted=True,
                message=f"{self.config.mode.value} order, close-all SL, and partial TP ladder placed.",
                trade_id=trade_id,
                client_order_ids=client_order_ids,
                entry_order=entry_order,
                stop_order=stop_order,
                take_profit_order=take_profit_orders[-1] if take_profit_orders else None,
                take_profit_orders=tuple(take_profit_orders),
            )
        except Exception:
            logger.exception("Order placement failed for %s; attempting defensive cancel/close.", plan.symbol)
            await self._defensive_cleanup(plan, entry_client_order_id)
            raise

    async def close_position(self, position: Position, reason: str = "manual") -> OrderResult:
        if self.config.mode in {TradingMode.DRY_RUN, TradingMode.BACKTEST, TradingMode.PAPER_TRADING}:
            self.positions.clear_local_position(position.symbol)
            return OrderResult(
                symbol=position.symbol,
                mode=self.config.mode,
                accepted=True,
                message=f"{self.config.mode.value}: local position closed ({reason}); no exchange order sent.",
            )

        if self.config.mode not in {TradingMode.TESTNET_LIVE, TradingMode.MAINNET_LIVE}:
            raise RuntimeError(f"Unsupported execution mode: {self.config.mode}.")
        if not self.binance:
            raise RuntimeError("Live close requires Binance client.")

        close_side = OrderSide.SELL if position.direction == Direction.LONG else OrderSide.BUY
        try:
            await self.binance.cancel_all_orders(position.symbol)
        except Exception:
            logger.exception("Failed to cancel open orders before closing %s", position.symbol)

        order = await self._new_order_with_recovery(
            client_order_id=_client_order_id(position.symbol, _trade_id(), "close"),
            symbol=position.symbol,
            side=close_side.value,
            type="MARKET",
            quantity=format_decimal(position.quantity),
            reduceOnly="true",
        )
        self.positions.clear_local_position(position.symbol)
        return OrderResult(
            symbol=position.symbol,
            mode=self.config.mode,
            accepted=True,
            message=f"{self.config.mode.value}: position close submitted ({reason}).",
            entry_order=order,
        )

    async def _defensive_cleanup(self, plan: RiskPlan, entry_client_order_id: str | None = None) -> None:
        if not self.binance:
            return
        if entry_client_order_id:
            try:
                entry_state = await self.binance.query_order(plan.symbol, orig_client_order_id=entry_client_order_id)
                logger.warning(
                    "%s entry order state after placement failure: %s",
                    plan.symbol,
                    entry_state.get("status"),
                )
            except Exception:
                logger.exception("Could not query entry order %s after placement failure.", entry_client_order_id)
        try:
            await self.binance.cancel_all_orders(plan.symbol)
        except Exception:
            logger.exception("Failed to cancel open orders for %s", plan.symbol)
        try:
            active = [p for p in await self.positions.active_positions() if p.symbol == plan.symbol]
            if active:
                position = active[0]
                close_side = OrderSide.SELL if position.direction == Direction.LONG else OrderSide.BUY
                await self.binance.new_order(
                    symbol=plan.symbol,
                    side=close_side.value,
                    type="MARKET",
                    quantity=format_decimal(position.quantity),
                    reduceOnly="true",
                    newClientOrderId=_client_order_id(plan.symbol, _trade_id(), "cleanup"),
                )
        except Exception:
            logger.exception("Failed to defensively close %s", plan.symbol)

    async def _new_order_with_recovery(self, client_order_id: str, **params) -> dict:
        if not self.binance:
            raise RuntimeError("Live order recovery requires Binance client.")
        symbol = params["symbol"]
        try:
            return await self.binance.new_order(**params, newClientOrderId=client_order_id)
        except BinanceAPIError as exc:
            try:
                state = await self.binance.query_order(symbol, orig_client_order_id=client_order_id)
            except Exception:
                logger.exception("Could not recover order state for %s after Binance error.", client_order_id)
                raise exc
            logger.warning(
                "Recovered order state after Binance error for %s: status=%s",
                client_order_id,
                state.get("status"),
            )
            return state

    async def _verify_protective_order(self, symbol: str, order: dict) -> None:
        if not self.binance:
            return
        order_id = order.get("orderId")
        client_order_id = order.get("clientOrderId")
        if not order_id and not client_order_id:
            raise RuntimeError("Protective order response lacks order id.")
        state = await self.binance.query_order(symbol, order_id=order_id, orig_client_order_id=client_order_id)
        if state.get("status") not in {"NEW", "PARTIALLY_FILLED"}:
            raise RuntimeError(f"Protective order is not active: {state}")

    @staticmethod
    def _executed_quantity(order: dict) -> Decimal | None:
        from trading_bot.models import to_decimal

        for key in ("executedQty", "cumQty"):
            if order.get(key) not in (None, ""):
                return to_decimal(order[key])
        return None


def _trade_id() -> str:
    timestamp = int(time.time() * 1000) % 1_000_000_000
    return str(timestamp)


def _client_order_id(symbol: str, trade_id: str, suffix: str) -> str:
    return f"bot-{symbol[:12]}-{trade_id}-{suffix}"[:36]
