from __future__ import annotations

from decimal import Decimal

from trading_bot.config import TradeManagementConfig
from trading_bot.models import Direction, ProtectionPlan, Signal, SymbolFilters, TakeProfitTarget, TradingStyle, to_decimal


class ExitPlanBuilder:
    def __init__(self, config: TradeManagementConfig) -> None:
        self.config = config

    def build_targets(
        self,
        signal: Signal,
        quantity: Decimal,
        filters: SymbolFilters,
    ) -> tuple[TakeProfitTarget, ...]:
        if signal.stop_loss is None:
            raise ValueError("Cannot build partial exits without stop-loss.")
        entry = to_decimal(signal.entry_price)
        stop = to_decimal(signal.stop_loss)
        stop_distance = abs(entry - stop)
        allocated = Decimal("0")
        targets: list[TakeProfitTarget] = []
        for index, item in enumerate(self.config.partial_take_profits):
            if index == len(self.config.partial_take_profits) - 1:
                target_qty = filters.round_quantity(quantity - allocated)
            else:
                target_qty = filters.round_quantity(quantity * item.fraction)
                allocated += target_qty
            if target_qty <= 0:
                continue
            if signal.direction == Direction.LONG:
                price = entry + (stop_distance * item.reward_risk)
            else:
                price = entry - (stop_distance * item.reward_risk)
            targets.append(
                TakeProfitTarget(
                    name=item.name,
                    price=filters.round_price(price),
                    quantity=target_qty,
                    fraction=item.fraction,
                    reward_risk=item.reward_risk,
                    move_stop_to_breakeven=item.move_stop_to_breakeven,
                    activate_trailing=item.activate_trailing,
                )
            )
        return tuple(targets)

    def build_protection(
        self,
        signal: Signal,
        style: TradingStyle,
        filters: SymbolFilters | None = None,
    ) -> ProtectionPlan:
        if signal.stop_loss is None:
            raise ValueError("Cannot build protection without stop-loss.")
        entry = to_decimal(signal.entry_price)
        offset = entry * self.config.breakeven_offset_bps / Decimal("10000")
        if signal.direction == Direction.LONG:
            breakeven = entry + offset
        else:
            breakeven = entry - offset
        if filters:
            breakeven = filters.round_price(breakeven)

        breakeven_after = None
        for item in self.config.partial_take_profits:
            if item.move_stop_to_breakeven:
                breakeven_after = item.name
                break

        callback = self.config.trailing_callback_rate_pct.get(style.value, Decimal("0.6"))
        return ProtectionPlan(
            initial_stop=to_decimal(signal.stop_loss),
            breakeven_price=breakeven,
            breakeven_after_target=breakeven_after,
            trailing_enabled=self.config.trailing_enabled,
            trailing_activation_reward_risk=self.config.trailing_activation_reward_risk,
            trailing_callback_rate_pct=callback,
        )
