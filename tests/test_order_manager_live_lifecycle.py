from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from trading_bot.config import load_config
from trading_bot.data_provider import BinanceAPIError
from trading_bot.models import Direction, RiskPlan, TakeProfitTarget, TradingMode
from trading_bot.order_manager.manager import OrderManager, _client_order_id
from trading_bot.position_manager import PositionManager


def test_client_order_id_is_unique_trade_scoped_and_binance_safe_length() -> None:
    client_id = _client_order_id("HBARUSDT", "123456789", "entry")

    assert client_id == "bot-HBARUSDT-123456789-entry"
    assert len(client_id) <= 36


def test_executed_quantity_does_not_treat_orig_qty_as_fill() -> None:
    order = {"status": "NEW", "origQty": "10", "executedQty": "0"}

    assert OrderManager._executed_quantity(order) == Decimal("0")


@pytest.mark.asyncio
async def test_paper_entry_fill_applies_adverse_slippage_and_records_metadata() -> None:
    config = replace(load_config(), mode=TradingMode.PAPER_TRADING)
    positions = PositionManager()
    manager = OrderManager(config, None, positions)

    result = await manager.execute(_risk_plan())
    active = await positions.active_positions()

    assert result.entry_order is not None
    assert result.entry_order["simulatedFillModel"] == "paper_entry_slippage_v1"
    assert result.execution_metadata["simulated"] is True
    assert result.execution_metadata["plannedEntryPrice"] == "100"
    assert result.execution_metadata["averageFillPrice"] == "100.05"
    assert result.execution_metadata["entrySlippageCost"] == "0.05"
    assert result.execution_metadata["entryFee"] == "0.04002"
    assert active[0].entry_price == Decimal("100.05")


@pytest.mark.asyncio
async def test_live_partial_fill_scales_take_profit_ladder_and_records_execution_metadata() -> None:
    config = replace(load_config(), mode=TradingMode.TESTNET_LIVE)
    binance = FakeBinance(entry_status="FILLED", executed_qty="0.5", cumulative_quote_qty="50")
    manager = OrderManager(config, binance, PositionManager())

    result = await manager.execute(_risk_plan())

    tp_orders = [order for order in binance.new_orders if order["type"] == "TAKE_PROFIT_MARKET"]
    assert [order["quantity"] for order in tp_orders] == ["0.2", "0.175", "0.125"]
    assert sum(Decimal(order["quantity"]) for order in tp_orders) == Decimal("0.5")
    assert result.execution_metadata["executedQty"] == "0.5"
    assert result.execution_metadata["cumulativeQuoteQty"] == "50"
    assert result.execution_metadata["averageFillPrice"] == "100"
    assert result.execution_metadata["partialFill"] is True
    assert result.execution_metadata["protection_verified"] is True


@pytest.mark.asyncio
async def test_failed_post_fill_protection_verification_defensively_closes_executed_quantity() -> None:
    config = replace(load_config(), mode=TradingMode.TESTNET_LIVE)
    binance = FakeBinance(entry_status="FILLED", executed_qty="0.5", stop_status="CANCELED")
    manager = OrderManager(config, binance, PositionManager())

    with pytest.raises(RuntimeError, match="Protective order is not active"):
        await manager.execute(_risk_plan())

    cleanup_orders = [order for order in binance.new_orders if order.get("newClientOrderId", "").endswith("cleanup")]
    assert cleanup_orders
    assert cleanup_orders[-1]["type"] == "MARKET"
    assert cleanup_orders[-1]["quantity"] == "0.5"
    assert cleanup_orders[-1]["reduceOnly"] == "true"
    assert binance.cancelled_symbols == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_live_noop_margin_type_error_is_skipped_before_entry() -> None:
    config = replace(load_config(), mode=TradingMode.TESTNET_LIVE)
    binance = FakeBinance(
        margin_type_error=BinanceAPIError(
            "No need to change margin type.",
            400,
            {"code": -4046, "msg": "No need to change margin type."},
        )
    )
    manager = OrderManager(config, binance, PositionManager())

    result = await manager.execute(_risk_plan())

    assert result.accepted is True
    assert any(order["type"] == "MARKET" and not order.get("reduceOnly") for order in binance.new_orders)


@pytest.mark.asyncio
async def test_live_position_setting_error_blocks_before_market_entry() -> None:
    config = replace(load_config(), mode=TradingMode.TESTNET_LIVE)
    binance = FakeBinance(
        margin_type_error=BinanceAPIError(
            "Cannot change margin type if there exists position.",
            400,
            {"code": -4048, "msg": "Cannot change margin type if there exists position."},
        )
    )
    manager = OrderManager(config, binance, PositionManager())

    with pytest.raises(BinanceAPIError, match="exists position"):
        await manager.execute(_risk_plan())

    assert binance.new_orders == []
    assert binance.cancelled_symbols == []


def _risk_plan() -> RiskPlan:
    return RiskPlan(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        quantity=Decimal("1"),
        notional=Decimal("100"),
        initial_margin=Decimal("50"),
        risk_amount=Decimal("5"),
        reward_amount=Decimal("10"),
        risk_pct=Decimal("0.01"),
        leverage=2,
        reward_risk=Decimal("2"),
        partial_take_profits=(
            TakeProfitTarget("TP1", Decimal("105"), Decimal("0.4"), Decimal("0.4"), Decimal("1")),
            TakeProfitTarget("TP2", Decimal("108"), Decimal("0.35"), Decimal("0.35"), Decimal("1.6")),
            TakeProfitTarget("TP3", Decimal("110"), Decimal("0.25"), Decimal("0.25"), Decimal("2")),
        ),
    )


class FakeBinance:
    def __init__(
        self,
        *,
        entry_status: str = "FILLED",
        executed_qty: str = "1",
        cumulative_quote_qty: str = "100",
        stop_status: str = "NEW",
        leverage_error: BinanceAPIError | None = None,
        margin_type_error: BinanceAPIError | None = None,
    ) -> None:
        self.entry_status = entry_status
        self.executed_qty = executed_qty
        self.cumulative_quote_qty = cumulative_quote_qty
        self.stop_status = stop_status
        self.leverage_error = leverage_error
        self.margin_type_error = margin_type_error
        self.new_orders: list[dict] = []
        self.cancelled_symbols: list[str] = []

    async def change_leverage(self, symbol: str, leverage: int) -> dict:
        if self.leverage_error:
            raise self.leverage_error
        return {"symbol": symbol, "leverage": leverage}

    async def change_margin_type(self, symbol: str, margin_type: str) -> dict:
        if self.margin_type_error:
            raise self.margin_type_error
        return {"symbol": symbol, "marginType": margin_type}

    async def new_order(self, **params) -> dict:
        self.new_orders.append(params)
        order = dict(params)
        order["clientOrderId"] = params.get("newClientOrderId")
        order["orderId"] = len(self.new_orders)
        if params["type"] == "MARKET" and not params.get("reduceOnly"):
            order.update(
                {
                    "status": self.entry_status,
                    "origQty": params["quantity"],
                    "executedQty": self.executed_qty,
                    "cumulativeQuoteQty": self.cumulative_quote_qty,
                }
            )
        else:
            order.update({"status": "NEW", "origQty": params.get("quantity", "0"), "executedQty": "0"})
        return order

    async def query_order(self, symbol: str, order_id=None, orig_client_order_id=None) -> dict:
        order = next(
            item
            for item in self.new_orders
            if item.get("newClientOrderId") == orig_client_order_id or item.get("orderId") == order_id
        )
        state = dict(order)
        state["clientOrderId"] = order.get("newClientOrderId")
        state["status"] = self.stop_status if order["type"] == "STOP_MARKET" else "NEW"
        state["origQty"] = order.get("quantity", "0")
        return state

    async def cancel_all_orders(self, symbol: str) -> dict:
        self.cancelled_symbols.append(symbol)
        return {"symbol": symbol}

    async def position_risk(self) -> list[dict]:
        return []
