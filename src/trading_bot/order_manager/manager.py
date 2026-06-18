from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

from trading_bot.config import AppConfig
from trading_bot.data_provider import BinanceAPIError, BinanceUSDMClient
from trading_bot.models import Direction, OrderResult, OrderSide, Position, RiskPlan, TakeProfitTarget, TradingMode
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
            entry_order: dict[str, Any] | None = None
            execution_metadata: dict[str, Any] = {}
            local_entry_price = plan.entry_price
            if self.config.mode == TradingMode.PAPER_TRADING:
                entry_order = simulated_local_entry_order(
                    symbol=plan.symbol,
                    direction=plan.direction,
                    quantity=plan.quantity,
                    planned_entry_price=plan.entry_price,
                    taker_fee_bps=self.config.risk.taker_fee_bps,
                    slippage_bps=self.config.risk.slippage_bps,
                )
                execution_metadata = self._execution_metadata(entry_order, plan.quantity)
                local_entry_price = Decimal(str(execution_metadata["averageFillPrice"]))
            position = Position(
                symbol=plan.symbol,
                direction=plan.direction,
                quantity=plan.quantity,
                entry_price=local_entry_price,
                stop_loss=plan.stop_loss,
                take_profit=plan.take_profit,
                managed_by_bot=True,
                source=self.config.mode.value,
                leverage=plan.leverage,
                initial_margin=plan.initial_margin,
            )
            if self.config.mode == TradingMode.PAPER_TRADING:
                self.positions.set_local_position(position)
            return OrderResult(
                symbol=plan.symbol,
                mode=self.config.mode,
                accepted=True,
                message=f"{self.config.mode.value}: no exchange order sent; partial exits simulated locally.",
                entry_order=entry_order,
                take_profit_orders=tuple({"name": tp.name, "price": str(tp.price), "quantity": str(tp.quantity)} for tp in plan.partial_take_profits),
                execution_metadata=execution_metadata,
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

        entry_order_attempted = False
        try:
            await self._apply_live_position_settings(plan.symbol, plan.leverage)

            entry_order_attempted = True
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
            execution_metadata = self._execution_metadata(entry_order, plan.quantity)

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
            stop_state = await self._verify_protective_order(plan.symbol, stop_order, require_close_position=True)

            take_profit_orders: list[dict] = []
            take_profit_states: list[dict] = []
            target_quantities = self._take_profit_quantities_for_fill(plan, executed_qty)
            for target, tp_qty in target_quantities:
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
                take_profit_states.append(
                    await self._verify_protective_order(plan.symbol, order, require_reduce_only=True)
                )
            self._verify_live_protection_stack(plan.symbol, executed_qty, stop_state, take_profit_states)
            execution_metadata["protection_verified"] = True
            execution_metadata["verified_stop_order"] = stop_state
            execution_metadata["verified_take_profit_orders"] = take_profit_states
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
                execution_metadata=execution_metadata,
            )
        except Exception:
            executed_qty = locals().get("executed_qty")
            if entry_order_attempted or executed_qty is not None:
                logger.exception("Order placement failed for %s; attempting defensive cancel/close.", plan.symbol)
                await self._defensive_cleanup(
                    plan,
                    entry_client_order_id if entry_order_attempted else None,
                    executed_qty=executed_qty,
                )
            else:
                logger.exception("Live order setup failed for %s before entry submission.", plan.symbol)
            raise

    async def _apply_live_position_settings(self, symbol: str, leverage: int) -> None:
        if not self.binance:
            raise RuntimeError("Live execution requires Binance client.")
        try:
            await self.binance.change_leverage(symbol, leverage)
        except BinanceAPIError as exc:
            if _is_noop_position_setting_error(exc):
                logger.info("Leverage change already satisfied for %s: %s", symbol, exc)
            else:
                raise

        try:
            await self.binance.change_margin_type(symbol, self.config.trading.margin_type)
        except BinanceAPIError as exc:
            if _is_noop_position_setting_error(exc):
                logger.info("Margin type change already satisfied for %s: %s", symbol, exc)
            else:
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

    async def _defensive_cleanup(
        self,
        plan: RiskPlan,
        entry_client_order_id: str | None = None,
        executed_qty: Decimal | None = None,
    ) -> None:
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
            if active or (executed_qty is not None and executed_qty > 0):
                position = active[0] if active else None
                direction = position.direction if position else plan.direction
                close_side = OrderSide.SELL if direction == Direction.LONG else OrderSide.BUY
                await self.binance.new_order(
                    symbol=plan.symbol,
                    side=close_side.value,
                    type="MARKET",
                    quantity=format_decimal(position.quantity if active else executed_qty),
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

    async def _verify_protective_order(
        self,
        symbol: str,
        order: dict,
        *,
        require_reduce_only: bool = False,
        require_close_position: bool = False,
    ) -> dict:
        if not self.binance:
            return order
        order_id = order.get("orderId")
        client_order_id = order.get("clientOrderId")
        if not order_id and not client_order_id:
            raise RuntimeError("Protective order response lacks order id.")
        state = await self.binance.query_order(symbol, order_id=order_id, orig_client_order_id=client_order_id)
        if state.get("status") not in {"NEW", "PARTIALLY_FILLED"}:
            raise RuntimeError(f"Protective order is not active: {state}")
        if require_reduce_only and not (_truthy(state.get("reduceOnly")) or _truthy(order.get("reduceOnly"))):
            raise RuntimeError(f"Protective take-profit is not reduce-only: {state}")
        if require_close_position and not (_truthy(state.get("closePosition")) or _truthy(order.get("closePosition"))):
            raise RuntimeError(f"Protective stop is not close-position: {state}")
        return state

    @staticmethod
    def _verify_live_protection_stack(
        symbol: str,
        executed_qty: Decimal,
        stop_state: dict,
        take_profit_states: list[dict],
    ) -> None:
        if not take_profit_states:
            raise RuntimeError(f"{symbol} protective TP ladder is empty after entry fill.")
        total_tp_qty = sum((_order_quantity(order) or Decimal("0")) for order in take_profit_states)
        if total_tp_qty <= 0:
            raise RuntimeError(f"{symbol} protective TP ladder has zero active quantity.")
        if total_tp_qty > executed_qty * Decimal("1.001"):
            raise RuntimeError(
                f"{symbol} protective TP quantity {total_tp_qty} exceeds executed entry quantity {executed_qty}."
            )
        if stop_state.get("status") not in {"NEW", "PARTIALLY_FILLED"}:
            raise RuntimeError(f"{symbol} protective stop is not active after verification: {stop_state}")

    @staticmethod
    def _take_profit_quantities_for_fill(
        plan: RiskPlan, executed_qty: Decimal
    ) -> list[tuple[TakeProfitTarget, Decimal]]:
        targets = list(plan.partial_take_profits)
        if executed_qty <= 0 or not targets:
            return []
        quantities: list[tuple[object, Decimal]] = []
        remaining = executed_qty
        for index, target in enumerate(targets):
            if remaining <= 0:
                break
            if index == len(targets) - 1:
                qty = remaining
            else:
                qty = min(executed_qty * target.fraction, remaining)
            quantities.append((target, qty))
            remaining -= qty
        return quantities

    @staticmethod
    def _executed_quantity(order: dict) -> Decimal | None:
        from trading_bot.models import to_decimal

        for key in ("executedQty", "cumQty"):
            if order.get(key) not in (None, ""):
                return to_decimal(order[key])
        return None

    @staticmethod
    def _execution_metadata(order: dict, planned_qty: Decimal) -> dict:
        executed_qty = OrderManager._executed_quantity(order) or Decimal("0")
        cumulative_quote_qty = _cumulative_quote_quantity(order) or Decimal("0")
        average_fill_price: Decimal | None = None
        if order.get("avgPrice") not in (None, "", "0"):
            from trading_bot.models import to_decimal

            average_fill_price = to_decimal(order["avgPrice"])
        elif executed_qty > 0 and cumulative_quote_qty > 0:
            average_fill_price = cumulative_quote_qty / executed_qty
        return {
            "status": order.get("status"),
            "executedQty": format_decimal(executed_qty),
            "cumulativeQuoteQty": format_decimal(cumulative_quote_qty),
            "averageFillPrice": format_decimal(average_fill_price) if average_fill_price is not None else None,
            "plannedQty": format_decimal(planned_qty),
            "partialFill": executed_qty > 0 and executed_qty < planned_qty,
            "simulated": bool(order.get("simulated")),
            "simulatedFillModel": order.get("simulatedFillModel"),
            "plannedEntryPrice": order.get("plannedEntryPrice"),
            "effectiveEntryPrice": order.get("effectiveEntryPrice"),
            "entrySlippageBps": order.get("entrySlippageBps"),
            "entrySlippageCost": order.get("entrySlippageCost"),
            "entryTakerFeeBps": order.get("entryTakerFeeBps"),
            "entryFee": order.get("entryFee"),
        }


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)


def _is_noop_position_setting_error(exc: BinanceAPIError) -> bool:
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    code = payload.get("code")
    message = str(payload.get("msg") or exc).lower()
    return code == -4046 or "no need to change" in message


def _order_quantity(order: dict) -> Decimal | None:
    from trading_bot.models import to_decimal

    for key in ("origQty", "quantity", "executedQty", "cumQty"):
        if order.get(key) not in (None, ""):
            return to_decimal(order[key])
    return None


def _cumulative_quote_quantity(order: dict) -> Decimal | None:
    from trading_bot.models import to_decimal

    for key in ("cumulativeQuoteQty", "cumQuote", "cumQuoteQty"):
        if order.get(key) not in (None, ""):
            return to_decimal(order[key])
    return None


def simulated_local_entry_order(
    *,
    symbol: str,
    direction: Direction,
    quantity: Decimal,
    planned_entry_price: Decimal,
    taker_fee_bps: Decimal,
    slippage_bps: Decimal,
) -> dict[str, Any]:
    effective_entry = simulated_entry_price(direction, planned_entry_price, slippage_bps)
    notional = effective_entry * quantity
    if direction == Direction.LONG:
        slippage_cost = (effective_entry - planned_entry_price) * quantity
        side = OrderSide.BUY.value
    else:
        slippage_cost = (planned_entry_price - effective_entry) * quantity
        side = OrderSide.SELL.value
    entry_fee = notional * taker_fee_bps / Decimal("10000")
    return {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "status": "FILLED",
        "origQty": format_decimal(quantity),
        "executedQty": format_decimal(quantity),
        "cumulativeQuoteQty": format_decimal(notional),
        "avgPrice": format_decimal(effective_entry),
        "simulated": True,
        "simulatedFillModel": "paper_entry_slippage_v1",
        "plannedEntryPrice": format_decimal(planned_entry_price),
        "effectiveEntryPrice": format_decimal(effective_entry),
        "entrySlippageBps": format_decimal(slippage_bps),
        "entrySlippageCost": format_decimal(max(slippage_cost, Decimal("0"))),
        "entryTakerFeeBps": format_decimal(taker_fee_bps),
        "entryFee": format_decimal(entry_fee),
    }


def simulated_entry_price(direction: Direction, planned_entry_price: Decimal, slippage_bps: Decimal) -> Decimal:
    slippage = slippage_bps / Decimal("10000")
    if direction == Direction.LONG:
        return planned_entry_price * (Decimal("1") + slippage)
    if direction == Direction.SHORT:
        return planned_entry_price * (Decimal("1") - slippage)
    return planned_entry_price


def _trade_id() -> str:
    timestamp = int(time.time() * 1000) % 1_000_000_000
    return str(timestamp)


def _client_order_id(symbol: str, trade_id: str, suffix: str) -> str:
    return f"bot-{symbol[:12]}-{trade_id}-{suffix}"[:36]
