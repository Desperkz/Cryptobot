from __future__ import annotations

from decimal import Decimal

from trading_bot.config import PartialTakeProfitConfig, TradeManagementConfig
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
        signal_reward_risk = _signal_reward_risk(signal)
        profile = self._targets_for_signal(signal)
        allocated = Decimal("0")
        targets: list[TakeProfitTarget] = []
        for index, item in enumerate(profile):
            if index == len(profile) - 1:
                target_qty = filters.round_quantity(quantity - allocated)
            else:
                target_qty = filters.round_quantity(quantity * item.fraction)
                allocated += target_qty
            if target_qty <= 0:
                continue
            reward_risk = min(item.reward_risk, signal_reward_risk)
            if signal.direction == Direction.LONG:
                price = entry + (stop_distance * reward_risk)
            else:
                price = entry - (stop_distance * reward_risk)
            targets.append(
                TakeProfitTarget(
                    name=item.name,
                    price=filters.round_price(price),
                    quantity=target_qty,
                    fraction=item.fraction,
                    reward_risk=reward_risk,
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
        for item in self._targets_for_signal(signal):
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

    def _targets_for_signal(self, signal: Signal) -> list[PartialTakeProfitConfig]:
        metadata = signal.metadata or {}
        strategy = str(
            metadata.get("exit_profile_strategy") or metadata.get("strategy") or ""
        ).strip().upper()
        if strategy:
            profile = self.config.strategy_exit_profiles.get(strategy)
            if profile:
                return profile
        return self.config.partial_take_profits


def _signal_reward_risk(signal: Signal) -> Decimal:
    if signal.stop_loss is None or signal.take_profit is None:
        return Decimal("0")
    entry = to_decimal(signal.entry_price)
    stop_distance = abs(entry - to_decimal(signal.stop_loss))
    if stop_distance <= 0:
        return Decimal("0")
    return abs(to_decimal(signal.take_profit) - entry) / stop_distance
