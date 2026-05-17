from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.models import Direction, Position
from trading_bot.position_manager import PositionManager


@pytest.mark.asyncio
async def test_local_position_blocks_repeat_entry() -> None:
    manager = PositionManager()

    assert await manager.has_active_position() is False
    manager.set_local_position(Position("BTCUSDT", Direction.LONG, Decimal("0.01"), Decimal("50000")))

    assert await manager.has_active_position() is True
    assert await manager.has_position_for_symbol("BTCUSDT") is True
    assert await manager.has_position_for_symbol("ETHUSDT") is False
    with pytest.raises(RuntimeError, match="Repeat entry is blocked|Active position exists"):
        await manager.ensure_no_active_position()
    with pytest.raises(RuntimeError, match="same symbol"):
        await manager.ensure_symbol_available("BTCUSDT")
    await manager.ensure_symbol_available("ETHUSDT")
