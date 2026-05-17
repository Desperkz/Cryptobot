from __future__ import annotations

from decimal import Decimal

from trading_bot.order_manager.manager import OrderManager, _client_order_id


def test_client_order_id_is_unique_trade_scoped_and_binance_safe_length() -> None:
    client_id = _client_order_id("HBARUSDT", "123456789", "entry")

    assert client_id == "bot-HBARUSDT-123456789-entry"
    assert len(client_id) <= 36


def test_executed_quantity_does_not_treat_orig_qty_as_fill() -> None:
    order = {"status": "NEW", "origQty": "10", "executedQty": "0"}

    assert OrderManager._executed_quantity(order) == Decimal("0")
