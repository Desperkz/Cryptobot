from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from trading_bot.data_provider import BinanceUSDMClient
from trading_bot.models import Direction, Position, to_decimal


class PositionManager:
    def __init__(self, binance: BinanceUSDMClient | None = None) -> None:
        self.binance = binance
        self._local_positions: dict[str, Position] = {}

    def set_local_position(self, position: Position) -> None:
        self._local_positions[position.symbol] = position

    def clear_local_position(self, symbol: str) -> None:
        self._local_positions.pop(symbol, None)

    def local_positions(self) -> list[Position]:
        return list(self._local_positions.values())

    async def active_positions(self) -> list[Position]:
        remote = await self._remote_positions()
        merged = {position.symbol: position for position in self._local_positions.values()}
        merged.update({position.symbol: position for position in remote})
        return [position for position in merged.values() if abs(position.quantity) > 0]

    async def has_active_position(self) -> bool:
        return bool(await self.active_positions())

    async def has_position_for_symbol(self, symbol: str) -> bool:
        return any(position.symbol == symbol for position in await self.active_positions())

    async def ensure_no_active_position(self) -> None:
        if await self.has_active_position():
            raise RuntimeError("Active position exists; repeat entry is blocked.")

    async def ensure_symbol_available(self, symbol: str) -> None:
        if await self.has_position_for_symbol(symbol):
            raise RuntimeError(f"Active {symbol} position exists; repeat entry for same symbol is blocked.")

    async def adopt_remote_positions(self) -> list[Position]:
        adopted = []
        for position in await self._remote_positions():
            if position.symbol not in self._local_positions:
                adopted_position = Position(
                    symbol=position.symbol,
                    direction=position.direction,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    mark_price=position.mark_price,
                    liquidation_price=position.liquidation_price,
                    stop_loss=position.stop_loss,
                    take_profit=position.take_profit,
                    managed_by_bot=False,
                    unrealized_pnl=position.unrealized_pnl,
                    source="MANUAL_OR_EXTERNAL",
                    leverage=position.leverage,
                    initial_margin=position.initial_margin,
                )
                self._local_positions[position.symbol] = adopted_position
                adopted.append(adopted_position)
        return adopted

    async def _remote_positions(self) -> list[Position]:
        if not self.binance:
            return []
        raw_positions = await self.binance.position_risk()
        return list(_parse_positions(raw_positions))


def _parse_positions(raw_positions: Iterable[dict]) -> Iterable[Position]:
    for item in raw_positions:
        amount = to_decimal(item.get("positionAmt", "0"))
        if amount == 0:
            continue
        direction = Direction.LONG if amount > 0 else Direction.SHORT
        liquidation = item.get("liquidationPrice")
        yield Position(
            symbol=item["symbol"],
            direction=direction,
            quantity=abs(amount),
            entry_price=to_decimal(item.get("entryPrice", "0")),
            mark_price=to_decimal(item.get("markPrice", "0")),
            liquidation_price=to_decimal(liquidation) if liquidation not in (None, "", "0") else None,
            unrealized_pnl=to_decimal(item.get("unRealizedProfit", "0")),
            source="BINANCE",
            leverage=_optional_int(item.get("leverage")),
            initial_margin=_optional_decimal(item.get("positionInitialMargin")),
        )


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, "", "None", "0"):
        return None
    try:
        parsed = to_decimal(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _optional_int(value: object) -> int | None:
    if value in (None, "", "None", "0"):
        return None
    try:
        parsed = int(str(value))
    except Exception:
        return None
    return parsed if parsed > 0 else None
