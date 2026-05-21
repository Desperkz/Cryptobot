from __future__ import annotations

from collections import defaultdict

from trading_bot.config import AppConfig
from trading_bot.data_provider import BinanceUSDMClient
from trading_bot.models import Direction, ExecutionIssue, to_decimal


class ExecutionReconciler:
    """REST reconciliation for live edge cases.

    It catches the dangerous states that can happen around partial fills,
    reduceOnly rejects, missing SL/TP, and orphaned conditional orders.
    WebSocket user-data-stream events should feed the same checks in a real
    always-on production process.
    """

    def __init__(self, config: AppConfig, binance: BinanceUSDMClient | None) -> None:
        self.config = config
        self.binance = binance

    async def reconcile(self) -> list[ExecutionIssue]:
        if not self.binance:
            return []
        positions = await self.binance.position_risk()
        open_orders = await self.binance.open_orders()
        orders_by_symbol: dict[str, list[dict]] = defaultdict(list)
        for order in open_orders:
            orders_by_symbol[order["symbol"]].append(order)

        issues: list[ExecutionIssue] = []
        active_symbols = set()
        for item in positions:
            amount = to_decimal(item.get("positionAmt", "0"))
            if amount == 0:
                continue
            symbol = item["symbol"]
            active_symbols.add(symbol)
            symbol_orders = orders_by_symbol.get(symbol, [])
            evidence = restart_recovery_evidence(item, symbol_orders)
            if not evidence["has_stop"]:
                issues.append(ExecutionIssue(symbol, "CRITICAL", "Active position has no STOP_MARKET/TRAILING stop."))
            elif not evidence["stop_close_position"]:
                issues.append(ExecutionIssue(symbol, "CRITICAL", "Active position stop is not close-position."))
            if not evidence["has_take_profit"]:
                issues.append(ExecutionIssue(symbol, "HIGH", "Active position has no take-profit order."))
            elif not evidence["all_take_profits_reduce_only"]:
                issues.append(ExecutionIssue(symbol, "HIGH", "Active position has take-profit orders that are not reduce-only."))

        for symbol, orders in orders_by_symbol.items():
            if symbol not in active_symbols and any(_is_reduce_only(order) for order in orders):
                issues.append(ExecutionIssue(symbol, "MEDIUM", "Reduce-only protective orders exist without position."))
        return issues


def restart_recovery_evidence(position: dict, orders: list[dict]) -> dict:
    amount = to_decimal(position.get("positionAmt", "0"))
    direction = Direction.LONG if amount > 0 else Direction.SHORT if amount < 0 else Direction.NONE
    stop_orders = [order for order in orders if _is_stop(order)]
    take_profit_orders = [order for order in orders if _is_take_profit(order)]
    bot_order_ids = [
        str(order.get("clientOrderId") or order.get("origClientOrderId") or "")
        for order in orders
        if str(order.get("clientOrderId") or order.get("origClientOrderId") or "").startswith("bot-")
    ]
    return {
        "source": "BINANCE_RESTART_RECOVERY",
        "symbol": position.get("symbol"),
        "direction": direction.value,
        "positionAmt": str(abs(amount)),
        "entryPrice": str(position.get("entryPrice", "0")),
        "markPrice": str(position.get("markPrice", "0")),
        "openOrderCount": len(orders),
        "stopOrderCount": len(stop_orders),
        "takeProfitOrderCount": len(take_profit_orders),
        "has_stop": bool(stop_orders),
        "has_take_profit": bool(take_profit_orders),
        "stop_close_position": bool(stop_orders) and all(_is_close_position(order) for order in stop_orders),
        "all_take_profits_reduce_only": bool(take_profit_orders)
        and all(_is_reduce_only(order) for order in take_profit_orders),
        "managed_by_bot": bool(bot_order_ids),
        "bot_client_order_ids": bot_order_ids,
        "protected": bool(stop_orders)
        and all(_is_close_position(order) for order in stop_orders)
        and bool(take_profit_orders)
        and all(_is_reduce_only(order) for order in take_profit_orders),
    }


def _has_stop(orders: list[dict]) -> bool:
    return any(_is_stop(order) for order in orders)


def _has_take_profit(orders: list[dict]) -> bool:
    return any(_is_take_profit(order) for order in orders)


def _is_reduce_only(order: dict) -> bool:
    return _truthy(order.get("reduceOnly")) or _truthy(order.get("closePosition"))


def _is_stop(order: dict) -> bool:
    return order.get("type") in {"STOP", "STOP_MARKET", "TRAILING_STOP_MARKET"}


def _is_take_profit(order: dict) -> bool:
    return order.get("type") in {"TAKE_PROFIT", "TAKE_PROFIT_MARKET"}


def _is_close_position(order: dict) -> bool:
    return _truthy(order.get("closePosition"))


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)
