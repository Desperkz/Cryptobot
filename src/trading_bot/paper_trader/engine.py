from __future__ import annotations

from decimal import Decimal

from trading_bot.models import Direction, Position, RiskPlan
from trading_bot.position_manager import PositionManager


class PaperTrader:
    def __init__(self, starting_balance: Decimal, positions: PositionManager) -> None:
        self.balance = starting_balance
        self.positions = positions

    def open_position(self, plan: RiskPlan) -> Position:
        position = Position(
            symbol=plan.symbol,
            direction=plan.direction,
            quantity=plan.quantity,
            entry_price=plan.entry_price,
            leverage=plan.leverage,
            initial_margin=plan.initial_margin,
            source="PAPER",
        )
        self.positions.set_local_position(position)
        return position

    def mark_to_market(self, symbol: str, price: Decimal) -> Decimal:
        pnl = Decimal("0")
        for position in self.positions.local_positions():
            if position.symbol != symbol:
                continue
            if position.direction == Direction.LONG:
                pnl += (price - position.entry_price) * position.quantity
            else:
                pnl += (position.entry_price - price) * position.quantity
        return pnl

    def close_position(self, symbol: str, price: Decimal) -> Decimal:
        pnl = self.mark_to_market(symbol, price)
        self.balance += pnl
        self.positions.clear_local_position(symbol)
        return pnl
