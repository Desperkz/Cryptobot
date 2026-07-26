from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading_bot.config import RiskConfig, TradeManagementConfig
from trading_bot.models import Direction, Position, RiskPlan, Signal, SymbolFilters, to_decimal
from trading_bot.trade_manager.exit_plan import ExitPlanBuilder


class RiskError(RuntimeError):
    pass


@dataclass
class RiskState:
    realized_pnl_today: Decimal = Decimal("0")
    losing_streak: int = 0
    cooldown_until: datetime | None = None
    emergency_stop: bool = False
    pnl_date_utc: str = ""  # дата UTC когда обнулился realized_pnl_today (YYYY-MM-DD)


@dataclass(frozen=True)
class FundingImpactEstimate:
    cost_bps: Decimal
    signed_bps: Decimal | None
    source: str


class RiskManager:
    def __init__(
        self,
        config: RiskConfig,
        trade_management: TradeManagementConfig | None = None,
        state: RiskState | None = None,
    ) -> None:
        self.config = config
        self.exit_plan_builder = ExitPlanBuilder(trade_management) if trade_management else None
        self.state = state or RiskState()

    def _reset_daily_pnl_if_needed(self) -> None:
        """Сбрасывает realized_pnl_today если наступил новый UTC день."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.pnl_date_utc != today:
            self.state.realized_pnl_today = Decimal("0")
            self.state.pnl_date_utc = today

    def calculate_plan(
        self,
        signal: Signal,
        equity_usdt: Decimal,
        filters: SymbolFilters,
        leverage: int | None = None,
        active_positions: list[Position] | None = None,
        liquidation_price: Decimal | None = None,
        live_mode: bool = False,
        risk_per_trade_pct: Decimal | None = None,
    ) -> RiskPlan:
        self._reset_daily_pnl_if_needed()
        self._validate_signal(signal)
        active_positions = active_positions or []
        self._validate_trade_allowed(signal.symbol, equity_usdt, active_positions)

        selected_leverage = min(leverage or self.config.default_leverage, self.config.max_leverage)
        if selected_leverage < 1:
            raise RiskError("Leverage must be >= 1.")

        entry = to_decimal(signal.entry_price)
        stop = filters.round_price(to_decimal(signal.stop_loss))
        take_profit = filters.round_price(to_decimal(signal.take_profit))
        if signal.direction == Direction.LONG and not (stop < entry < take_profit):
            raise RiskError("Rounded LONG prices must keep stop_loss < entry < take_profit.")
        if signal.direction == Direction.SHORT and not (take_profit < entry < stop):
            raise RiskError("Rounded SHORT prices must keep take_profit < entry < stop_loss.")
        stop_distance = abs(entry - stop)
        reward_distance = abs(take_profit - entry)
        if stop_distance <= 0:
            raise RiskError("Stop-loss distance must be positive.")

        reward_risk = reward_distance / stop_distance
        if reward_risk < self.config.min_reward_risk:
            raise RiskError(f"Reward/risk {reward_risk:.2f} is below minimum {self.config.min_reward_risk}.")

        used_estimated_liquidation = False
        if live_mode and liquidation_price is None:
            liquidation_price = _estimated_liquidation_price(signal.direction, entry, selected_leverage)
            used_estimated_liquidation = True
        self._validate_liquidation(signal.direction, stop, liquidation_price, live_mode)

        effective_risk_pct = risk_per_trade_pct or self.config.risk_per_trade_pct
        risk_budget = equity_usdt * effective_risk_pct
        funding_impact = _estimate_funding_impact_bps(signal.direction, signal.metadata, self.config)
        if funding_impact.cost_bps > self.config.max_funding_impact_bps:
            raise RiskError(
                "Estimated funding impact "
                f"{funding_impact.cost_bps:.2f} bps exceeds maximum {self.config.max_funding_impact_bps} bps."
            )
        cost_bps = self.config.taker_fee_bps * Decimal("2") + self.config.slippage_bps + funding_impact.cost_bps
        estimated_cost_per_unit = stop_distance + (entry * cost_bps / Decimal("10000"))
        raw_qty = risk_budget / estimated_cost_per_unit

        max_notional = equity_usdt * selected_leverage
        max_qty_by_leverage = max_notional / entry
        margin_used = sum(_position_margin_estimate(position, self.config.default_leverage) for position in active_positions)
        max_allowed_margin = equity_usdt * self.config.max_margin_usage_pct
        available_margin = max_allowed_margin - margin_used
        if available_margin <= 0:
            raise RiskError("New trade blocked: max margin usage would be exceeded.")
        max_qty_by_margin = (available_margin * selected_leverage) / entry
        capped_qty = min(raw_qty, max_qty_by_leverage, max_qty_by_margin)
        quantity = filters.round_quantity(capped_qty)

        if quantity <= 0 or quantity < filters.min_qty:
            raise RiskError(f"Calculated quantity {quantity} is below minimum {filters.min_qty}.")
        if filters.max_qty is not None and quantity > filters.max_qty:
            quantity = filters.round_quantity(filters.max_qty)

        notional = quantity * entry
        if notional < filters.min_notional:
            raise RiskError(f"Calculated notional {notional} is below minimum {filters.min_notional}.")

        initial_margin = notional / selected_leverage
        risk_amount = quantity * estimated_cost_per_unit
        reward_amount = quantity * reward_distance
        if initial_margin > equity_usdt:
            raise RiskError("Initial margin exceeds available equity.")
        self._validate_portfolio_limits(signal.symbol, equity_usdt, active_positions, risk_amount, initial_margin)

        warnings: list[str] = []
        if used_estimated_liquidation:
            warnings.append("Liquidation check used a conservative pre-trade estimate; verify exchange liquidation after fill.")
        if effective_risk_pct >= self.config.aggressive_risk_threshold_pct:
            warnings.append(
                f"Aggressive risk: {effective_risk_pct:.2%} per trade can cause rapid drawdown."
            )
        if raw_qty > max_qty_by_leverage:
            warnings.append("Position size was capped by max leverage.")
        if raw_qty > max_qty_by_margin:
            warnings.append("Position size was capped by max margin usage.")
        if funding_impact.source == "signed_estimate" and funding_impact.signed_bps is not None:
            if funding_impact.signed_bps < 0:
                warnings.append("Estimated funding is favorable for this direction; no funding buffer was charged.")
            elif funding_impact.signed_bps > Decimal("0"):
                warnings.append(f"Estimated adverse funding impact: {funding_impact.cost_bps:.2f} bps.")

        partial_take_profits = ()
        protection = None
        first_target_net_reward_risk = None
        exit_profile_signature = None
        if self.exit_plan_builder:
            rounded_signal = replace(signal, stop_loss=stop, take_profit=take_profit)
            partial_take_profits = self.exit_plan_builder.build_targets(rounded_signal, quantity, filters)
            if not partial_take_profits:
                raise RiskError("Partial take-profit ladder is mandatory but no valid TP targets were built.")
            first_target_net_reward_risk = self._validate_first_target_net_reward(
                rounded_signal,
                partial_take_profits[0],
                stop_distance,
                cost_bps,
            )
            exit_profile_signature = _exit_profile_signature(partial_take_profits)
            protection = self.exit_plan_builder.build_protection(rounded_signal, signal.style, filters)

        return RiskPlan(
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=entry,
            stop_loss=stop,
            take_profit=take_profit,
            quantity=quantity,
            notional=notional,
            initial_margin=initial_margin,
            risk_amount=risk_amount,
            reward_amount=reward_amount,
            risk_pct=risk_amount / equity_usdt if equity_usdt > 0 else Decimal("0"),
            leverage=selected_leverage,
            reward_risk=reward_risk,
            partial_take_profits=partial_take_profits,
            protection=protection,
            signal_metadata={
                **dict(signal.metadata),
                "style": signal.style.value,
                "direction": signal.direction.value,
                "confidence": str(signal.confidence),
                "risk_cost_bps": str(cost_bps),
                "risk_funding_impact_bps": str(funding_impact.cost_bps),
                "risk_funding_impact_source": funding_impact.source,
                "risk_signed_funding_impact_bps": (
                    str(funding_impact.signed_bps) if funding_impact.signed_bps is not None else None
                ),
                "exit_profile_signature": exit_profile_signature,
                "first_target_net_reward_risk": (
                    str(first_target_net_reward_risk) if first_target_net_reward_risk is not None else None
                ),
            },
            warnings=tuple(warnings),
        )

    def record_closed_trade(self, realized_pnl: Decimal) -> None:
        self._reset_daily_pnl_if_needed()
        self.state.realized_pnl_today += realized_pnl
        if realized_pnl < 0:
            self.state.losing_streak += 1
            if self.state.losing_streak >= self.config.cooldown_after_losses:
                self.state.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=self.config.cooldown_minutes)
        else:
            self.state.losing_streak = 0
            self.state.cooldown_until = None

    def emergency_stop(self) -> None:
        self.state.emergency_stop = True

    def _validate_first_target_net_reward(
        self,
        signal: Signal,
        first_target: object,
        stop_distance: Decimal,
        cost_bps: Decimal,
    ) -> Decimal | None:
        if not self.exit_plan_builder or not self.exit_plan_builder.config:
            return None
        if stop_distance <= 0:
            return None
        metadata = signal.metadata or {}
        strategy = str(
            metadata.get("exit_profile_strategy") or metadata.get("strategy") or ""
        ).strip().upper()
        config = self.exit_plan_builder.config
        minimum = config.strategy_min_first_target_net_reward_risk.get(
            strategy,
            config.min_first_target_net_reward_risk,
        )
        if minimum <= 0:
            return None
        entry = to_decimal(signal.entry_price)
        target_price = to_decimal(getattr(first_target, "price"))
        gross_reward = abs(target_price - entry)
        estimated_cost = entry * cost_bps / Decimal("10000")
        net_reward_risk = (gross_reward - estimated_cost) / stop_distance
        if net_reward_risk < minimum:
            raise RiskError(
                "First partial TP is too small after estimated costs: "
                f"net R {net_reward_risk:.2f} below minimum {minimum:.2f}."
            )
        return net_reward_risk

    def _validate_signal(self, signal: Signal) -> None:
        if signal.direction not in {Direction.LONG, Direction.SHORT}:
            raise RiskError("Signal direction must be LONG or SHORT.")
        if signal.stop_loss is None:
            raise RiskError("Stop-loss is mandatory.")
        if signal.take_profit is None:
            raise RiskError("Take-profit is mandatory.")
        entry = to_decimal(signal.entry_price)
        stop = to_decimal(signal.stop_loss)
        take_profit = to_decimal(signal.take_profit)
        if signal.direction == Direction.LONG and not (stop < entry < take_profit):
            raise RiskError("LONG signal must have stop_loss < entry < take_profit.")
        if signal.direction == Direction.SHORT and not (take_profit < entry < stop):
            raise RiskError("SHORT signal must have take_profit < entry < stop_loss.")

    def _validate_trade_allowed(self, symbol: str, equity_usdt: Decimal, active_positions: list[Position]) -> None:
        if self.state.emergency_stop:
            raise RiskError("Emergency stop is active.")
        if equity_usdt <= 0:
            raise RiskError("Equity must be positive.")
        if any(position.symbol == symbol for position in active_positions):
            raise RiskError(f"New trade blocked: active {symbol} position already exists.")
        if len(active_positions) >= self.config.max_concurrent_positions:
            raise RiskError("New trade blocked: max concurrent positions reached.")
        max_daily_loss = equity_usdt * self.config.max_daily_loss_pct
        if self.state.realized_pnl_today <= -max_daily_loss:
            raise RiskError("Daily loss limit reached.")
        if self.state.cooldown_until and datetime.now(timezone.utc) < self.state.cooldown_until:
            raise RiskError(f"Cooldown is active until {self.state.cooldown_until.isoformat()}.")

    def _validate_portfolio_limits(
        self,
        symbol: str,
        equity_usdt: Decimal,
        active_positions: list[Position],
        new_risk_amount: Decimal,
        new_margin: Decimal,
    ) -> None:
        active_risk = sum(_position_risk_estimate(position) for position in active_positions)
        if active_risk + new_risk_amount > equity_usdt * self.config.max_portfolio_risk_pct:
            raise RiskError("New trade blocked: max portfolio risk would be exceeded.")

        margin_used = sum(_position_margin_estimate(position, self.config.default_leverage) for position in active_positions)
        if margin_used + new_margin > equity_usdt * self.config.max_margin_usage_pct:
            raise RiskError("New trade blocked: max margin usage would be exceeded.")

        group = self._correlation_group(symbol)
        if group:
            group_symbols = set(self.config.correlation_groups[group])
            group_risk = sum(
                _position_risk_estimate(position)
                for position in active_positions
                if position.symbol in group_symbols
            )
            if group_risk + new_risk_amount > equity_usdt * self.config.max_correlated_group_risk_pct:
                raise RiskError("New trade blocked: correlated group risk would be exceeded.")

    def _correlation_group(self, symbol: str) -> str | None:
        for group, symbols in self.config.correlation_groups.items():
            if symbol in symbols:
                return group
        return None

    def _validate_liquidation(
        self,
        direction: Direction,
        stop_loss: Decimal,
        liquidation_price: Decimal | None,
        live_mode: bool,
    ) -> None:
        if not live_mode:
            return
        if liquidation_price is None:
            if self.config.require_liquidation_check_in_live:
                raise RiskError("Live mode requires liquidation price check before entry.")
            return
        buffer = self.config.liquidation_buffer_pct
        if direction == Direction.LONG:
            min_safe_stop = liquidation_price * (Decimal("1") + buffer)
            if stop_loss <= min_safe_stop:
                raise RiskError("LONG stop-loss is too close to liquidation price.")
        if direction == Direction.SHORT:
            max_safe_stop = liquidation_price * (Decimal("1") - buffer)
            if stop_loss >= max_safe_stop:
                raise RiskError("SHORT stop-loss is too close to liquidation price.")


def _position_risk_estimate(position: Position) -> Decimal:
    if position.stop_loss is not None:
        return abs(position.entry_price - position.stop_loss) * position.quantity
    return position.notional * Decimal("0.05")


def _position_margin_estimate(position: Position, default_leverage: int) -> Decimal:
    if position.initial_margin is not None and position.initial_margin > 0:
        return position.initial_margin
    leverage = position.leverage or default_leverage
    if leverage <= 0:
        leverage = 1
    return position.notional / Decimal(leverage)


def _exit_profile_signature(targets: tuple[object, ...]) -> str:
    parts = []
    for target in targets:
        flags = []
        if getattr(target, "move_stop_to_breakeven", False):
            flags.append("BE")
        if getattr(target, "activate_trailing", False):
            flags.append("TR")
        suffix = f"/{'+'.join(flags)}" if flags else ""
        parts.append(
            f"{getattr(target, 'name')}:{getattr(target, 'reward_risk')}R@{getattr(target, 'fraction')}{suffix}"
        )
    return "|".join(parts)


def _estimated_liquidation_price(direction: Direction, entry: Decimal, leverage: int) -> Decimal | None:
    if leverage <= 1:
        return None
    leverage_buffer = Decimal("1") / Decimal(leverage)
    if direction == Direction.LONG:
        return entry * (Decimal("1") - leverage_buffer)
    if direction == Direction.SHORT:
        return entry * (Decimal("1") + leverage_buffer)
    return None


def _estimate_funding_impact_bps(
    direction: Direction,
    metadata: dict[str, object] | None,
    config: RiskConfig,
) -> FundingImpactEstimate:
    funding_rate = _metadata_decimal(metadata or {}, "funding_rate")
    if funding_rate is None:
        return FundingImpactEstimate(
            cost_bps=max(Decimal("0"), config.funding_buffer_bps),
            signed_bps=None,
            source="fallback_buffer",
        )

    holding_hours = max(Decimal("0"), config.funding_impact_holding_hours)
    signed_rate = funding_rate if direction == Direction.LONG else -funding_rate
    signed_bps = signed_rate * Decimal("10000") * holding_hours / Decimal("8")
    return FundingImpactEstimate(
        cost_bps=max(Decimal("0"), signed_bps),
        signed_bps=signed_bps,
        source="signed_estimate",
    )


def _metadata_decimal(metadata: dict[str, object], key: str) -> Decimal | None:
    value = metadata.get(key)
    if value in (None, ""):
        return None
    try:
        return to_decimal(value)
    except Exception:
        return None
