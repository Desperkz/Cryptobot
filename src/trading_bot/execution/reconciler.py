from __future__ import annotations

from collections import defaultdict

from trading_bot.config import AppConfig
from trading_bot.data_provider import BinanceUSDMClient
from trading_bot.models import ExecutionIssue, to_decimal


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
            if not _has_stop(symbol_orders):
                issues.append(ExecutionIssue(symbol, "CRITICAL", "Active position has no STOP_MARKET/TRAILING stop."))
            if not _has_take_profit(symbol_orders):
                issues.append(ExecutionIssue(symbol, "HIGH", "Active position has no take-profit order."))

        for symbol, orders in orders_by_symbol.items():
            if symbol not in active_symbols and any(_is_reduce_only(order) for order in orders):
                issues.append(ExecutionIssue(symbol, "MEDIUM", "Reduce-only protective orders exist without position."))
        return issues


def _has_stop(orders: list[dict]) -> bool:
    return any(order.get("type") in {"STOP_MARKET", "TRAILING_STOP_MARKET"} for order in orders)


def _has_take_profit(orders: list[dict]) -> bool:
    return any(order.get("type") in {"TAKE_PROFIT", "TAKE_PROFIT_MARKET"} for order in orders)


def _is_reduce_only(order: dict) -> bool:
    return bool(order.get("reduceOnly")) or bool(order.get("closePosition"))

