from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_bot.models import Direction, to_decimal


@dataclass(frozen=True)
class ExecutionAssumptions:
    taker_fee_bps: Decimal = Decimal("4.0")
    base_slippage_bps: Decimal = Decimal("5.0")
    spread_slippage_multiplier: Decimal = Decimal("0.50")
    random_slippage_bps: Decimal = Decimal("1.0")
    funding_bps_per_8h: Decimal = Decimal("1.0")
    funding_rate_per_8h: Decimal | None = None
    pessimistic_intrabar: bool = True
    breakeven_offset_bps: Decimal = Decimal("2.0")
    trailing_callback_rate_pct: Decimal = Decimal("0.4")


@dataclass(frozen=True)
class PartialTargetSpec:
    name: str
    reward_risk: Decimal
    fraction: Decimal
    move_stop_to_breakeven: bool = False
    activate_trailing: bool = False


@dataclass(frozen=True)
class SimulatedExecution:
    reason: str
    exit_index: int
    exit_price: Decimal
    effective_exit_price: Decimal
    gross_pnl: Decimal
    fees: Decimal
    slippage_cost: Decimal
    funding_cost: Decimal
    net_pnl: Decimal
    r_multiple: Decimal
    closed_quantity: Decimal
    remaining_quantity: Decimal
    filled_targets: tuple[str, ...]


DEFAULT_PARTIAL_TARGETS: tuple[PartialTargetSpec, ...] = (
    PartialTargetSpec("TP1", Decimal("0.6"), Decimal("0.50"), move_stop_to_breakeven=True),
    PartialTargetSpec("TP2", Decimal("1.1"), Decimal("0.50"), activate_trailing=True),
)


def estimate_quantity_for_risk(
    entry: Decimal,
    stop_loss: Decimal,
    risk_amount: Decimal,
    assumptions: ExecutionAssumptions | None = None,
    expected_holding_hours: Decimal = Decimal("8"),
) -> Decimal:
    assumptions = assumptions or ExecutionAssumptions()
    stop_distance = abs(entry - stop_loss)
    if entry <= 0 or stop_distance <= 0 or risk_amount <= 0:
        return Decimal("0")
    round_turn_bps = assumptions.taker_fee_bps * Decimal("2") + assumptions.base_slippage_bps
    if assumptions.funding_rate_per_8h is not None:
        funding_bps = abs(assumptions.funding_rate_per_8h) * Decimal("10000") * expected_holding_hours / Decimal("8")
    else:
        funding_bps = assumptions.funding_bps_per_8h * expected_holding_hours / Decimal("8")
    estimated_loss_per_unit = stop_distance + entry * (round_turn_bps + funding_bps) / Decimal("10000")
    return risk_amount / estimated_loss_per_unit if estimated_loss_per_unit > 0 else Decimal("0")


def build_default_partial_targets(
    direction: Direction | str,
    entry: Decimal,
    stop_loss: Decimal,
    quantity: Decimal,
    specs: tuple[PartialTargetSpec, ...] = DEFAULT_PARTIAL_TARGETS,
) -> list[dict[str, Any]]:
    direction_value = _direction_value(direction)
    stop_distance = abs(entry - stop_loss)
    allocated = Decimal("0")
    targets: list[dict[str, Any]] = []
    for index, spec in enumerate(specs):
        if index == len(specs) - 1:
            target_qty = quantity - allocated
        else:
            target_qty = quantity * spec.fraction
            allocated += target_qty
        if target_qty <= 0:
            continue
        price = entry + stop_distance * spec.reward_risk if direction_value == "LONG" else entry - stop_distance * spec.reward_risk
        targets.append(
            {
                "name": spec.name,
                "price": price,
                "quantity": target_qty,
                "move_stop_to_breakeven": spec.move_stop_to_breakeven,
                "activate_trailing": spec.activate_trailing,
            }
        )
    return targets


def simulate_realistic_trade(
    candles: list[Any],
    entry_index: int,
    direction: Direction | str,
    entry: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    quantity: Decimal,
    risk_amount: Decimal,
    *,
    max_bars: int = 80,
    assumptions: ExecutionAssumptions | None = None,
    partial_targets: list[dict[str, Any]] | None = None,
    spread_bps: Decimal = Decimal("0"),
) -> SimulatedExecution:
    assumptions = assumptions or ExecutionAssumptions()
    direction_value = _direction_value(direction)
    if quantity <= 0:
        return _empty_execution(entry_index, entry)

    targets = partial_targets
    if targets is None:
        targets = build_default_partial_targets(direction_value, entry, stop_loss, quantity)
    if not targets:
        targets = [{"name": "TP", "price": take_profit, "quantity": quantity}]

    remaining_qty = quantity
    realized_gross = Decimal("0")
    realized_fees = Decimal("0")
    realized_slippage = Decimal("0")
    realized_funding = Decimal("0")
    filled_targets: list[str] = []
    current_stop = stop_loss
    trailing_active = False
    last_exit_index = min(entry_index + max_bars, len(candles) - 1)
    last_exit_price = entry
    last_effective_exit = entry
    entry_time = _candle_open_time(candles[entry_index]) if 0 <= entry_index < len(candles) else 0

    end_index = min(entry_index + max_bars, len(candles) - 1)
    for idx in range(entry_index + 1, end_index + 1):
        candle = candles[idx]
        pending = [target for target in targets if str(target.get("name", "TP")) not in filled_targets]
        stop_hit = _stop_hit(direction_value, candle, current_stop)
        target_hit = _first_target_hit(direction_value, candle, pending)

        if assumptions.pessimistic_intrabar and stop_hit and target_hit is not None:
            target_hit = None
        if stop_hit:
            closed = _close_slice(
                direction_value,
                entry,
                current_stop,
                remaining_qty,
                entry_time,
                _candle_close_time(candle),
                assumptions,
                spread_bps,
                idx,
            )
            realized_gross += closed.gross_pnl
            realized_fees += closed.fees
            realized_slippage += closed.slippage_cost
            realized_funding += closed.funding_cost
            last_exit_index = idx
            last_exit_price = current_stop
            last_effective_exit = closed.effective_exit_price
            remaining_qty = Decimal("0")
            return _final_execution(
                "stop_loss",
                last_exit_index,
                last_exit_price,
                last_effective_exit,
                realized_gross,
                realized_fees,
                realized_slippage,
                realized_funding,
                quantity,
                remaining_qty,
                risk_amount,
                filled_targets,
            )

        if target_hit is not None:
            target_name = str(target_hit.get("name", "TP"))
            target_price = to_decimal(target_hit["price"])
            target_qty = min(to_decimal(target_hit.get("quantity", remaining_qty)), remaining_qty)
            closed = _close_slice(
                direction_value,
                entry,
                target_price,
                target_qty,
                entry_time,
                _candle_close_time(candle),
                assumptions,
                spread_bps,
                idx,
            )
            realized_gross += closed.gross_pnl
            realized_fees += closed.fees
            realized_slippage += closed.slippage_cost
            realized_funding += closed.funding_cost
            remaining_qty -= target_qty
            filled_targets.append(target_name)
            last_exit_index = idx
            last_exit_price = target_price
            last_effective_exit = closed.effective_exit_price
            if target_hit.get("move_stop_to_breakeven") and remaining_qty > 0:
                current_stop = _breakeven_price(direction_value, entry, assumptions)
            if target_hit.get("activate_trailing") and remaining_qty > 0:
                trailing_active = True
            if remaining_qty <= 0:
                return _final_execution(
                    "take_profit",
                    last_exit_index,
                    last_exit_price,
                    last_effective_exit,
                    realized_gross,
                    realized_fees,
                    realized_slippage,
                    realized_funding,
                    quantity,
                    Decimal("0"),
                    risk_amount,
                    filled_targets,
                )

        if trailing_active and remaining_qty > 0:
            current_stop = _trailing_stop(direction_value, candle, current_stop, assumptions)

    if remaining_qty > 0:
        last_exit_index = end_index
        candle = candles[last_exit_index]
        exit_price = to_decimal(candle.close)
        closed = _close_slice(
            direction_value,
            entry,
            exit_price,
            remaining_qty,
            entry_time,
            _candle_close_time(candle),
            assumptions,
            spread_bps,
            last_exit_index,
        )
        realized_gross += closed.gross_pnl
        realized_fees += closed.fees
        realized_slippage += closed.slippage_cost
        realized_funding += closed.funding_cost
        last_exit_price = exit_price
        last_effective_exit = closed.effective_exit_price
        remaining_qty = Decimal("0")

    return _final_execution(
        "timeout",
        last_exit_index,
        last_exit_price,
        last_effective_exit,
        realized_gross,
        realized_fees,
        realized_slippage,
        realized_funding,
        quantity,
        remaining_qty,
        risk_amount,
        filled_targets,
    )


def _close_slice(
    direction: str,
    entry: Decimal,
    exit_price: Decimal,
    qty: Decimal,
    entry_time: int,
    exit_time: int,
    assumptions: ExecutionAssumptions,
    spread_bps: Decimal,
    event_index: int,
) -> SimulatedExecution:
    effective_exit = _effective_exit_price(direction, exit_price, assumptions, spread_bps, event_index)
    if direction == "LONG":
        gross = (exit_price - entry) * qty
        gross_after_slippage = (effective_exit - entry) * qty
    else:
        gross = (entry - exit_price) * qty
        gross_after_slippage = (entry - effective_exit) * qty
    slippage_cost = max(gross - gross_after_slippage, Decimal("0"))
    fees = (entry * qty + effective_exit * qty) * assumptions.taker_fee_bps / Decimal("10000")
    funding = _funding_cost(direction, entry, qty, entry_time, exit_time, assumptions)
    net = gross - slippage_cost - fees - funding
    return SimulatedExecution(
        reason="slice",
        exit_index=event_index,
        exit_price=exit_price,
        effective_exit_price=effective_exit,
        gross_pnl=gross,
        fees=fees,
        slippage_cost=slippage_cost,
        funding_cost=funding,
        net_pnl=net,
        r_multiple=Decimal("0"),
        closed_quantity=qty,
        remaining_quantity=Decimal("0"),
        filled_targets=(),
    )


def _final_execution(
    reason: str,
    exit_index: int,
    exit_price: Decimal,
    effective_exit: Decimal,
    gross: Decimal,
    fees: Decimal,
    slippage: Decimal,
    funding: Decimal,
    original_qty: Decimal,
    remaining_qty: Decimal,
    risk_amount: Decimal,
    filled_targets: list[str],
) -> SimulatedExecution:
    net = gross - slippage - fees - funding
    return SimulatedExecution(
        reason=reason,
        exit_index=exit_index,
        exit_price=exit_price,
        effective_exit_price=effective_exit,
        gross_pnl=gross,
        fees=fees,
        slippage_cost=slippage,
        funding_cost=funding,
        net_pnl=net,
        r_multiple=net / risk_amount if risk_amount > 0 else Decimal("0"),
        closed_quantity=original_qty - remaining_qty,
        remaining_quantity=remaining_qty,
        filled_targets=tuple(filled_targets),
    )


def _effective_exit_price(
    direction: str,
    price: Decimal,
    assumptions: ExecutionAssumptions,
    spread_bps: Decimal,
    event_index: int,
) -> Decimal:
    deterministic_extra = _deterministic_slippage_bps(event_index, assumptions.random_slippage_bps)
    slip_bps = assumptions.base_slippage_bps + spread_bps * assumptions.spread_slippage_multiplier + deterministic_extra
    slip = slip_bps / Decimal("10000")
    return price * (Decimal("1") - slip) if direction == "LONG" else price * (Decimal("1") + slip)


def _deterministic_slippage_bps(event_index: int, max_bps: Decimal) -> Decimal:
    if max_bps <= 0:
        return Decimal("0")
    basis = int(max_bps * Decimal("100"))
    if basis <= 0:
        return Decimal("0")
    return Decimal(str((event_index * 1103515245 + 12345) % (basis + 1))) / Decimal("100")


def _funding_cost(
    direction: str,
    entry: Decimal,
    qty: Decimal,
    entry_time: int,
    exit_time: int,
    assumptions: ExecutionAssumptions,
) -> Decimal:
    held_hours = Decimal(str(max(exit_time - entry_time, 0))) / Decimal("3600000")
    if held_hours <= 0:
        return Decimal("0")
    notional = entry * qty
    if assumptions.funding_rate_per_8h is not None:
        signed = assumptions.funding_rate_per_8h if direction == "LONG" else -assumptions.funding_rate_per_8h
        return notional * signed * (held_hours / Decimal("8"))
    return notional * assumptions.funding_bps_per_8h / Decimal("10000") * (held_hours / Decimal("8"))


def _breakeven_price(direction: str, entry: Decimal, assumptions: ExecutionAssumptions) -> Decimal:
    offset = entry * assumptions.breakeven_offset_bps / Decimal("10000")
    return entry + offset if direction == "LONG" else entry - offset


def _trailing_stop(direction: str, candle: Any, stop_loss: Decimal, assumptions: ExecutionAssumptions) -> Decimal:
    callback = assumptions.trailing_callback_rate_pct / Decimal("100")
    if direction == "LONG":
        candidate = to_decimal(candle.high) * (Decimal("1") - callback)
        return max(stop_loss, candidate)
    candidate = to_decimal(candle.low) * (Decimal("1") + callback)
    return min(stop_loss, candidate)


def _stop_hit(direction: str, candle: Any, stop_loss: Decimal) -> bool:
    return to_decimal(candle.high) >= stop_loss if direction == "SHORT" else to_decimal(candle.low) <= stop_loss


def _target_hit(direction: str, candle: Any, price: Decimal) -> bool:
    return to_decimal(candle.low) <= price if direction == "SHORT" else to_decimal(candle.high) >= price


def _first_target_hit(direction: str, candle: Any, targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    for target in targets:
        if _target_hit(direction, candle, to_decimal(target["price"])):
            return target
    return None


def _direction_value(direction: Direction | str) -> str:
    return direction.value if hasattr(direction, "value") else str(direction)


def _candle_open_time(candle: Any) -> int:
    return int(getattr(candle, "open_time", 0) or 0)


def _candle_close_time(candle: Any) -> int:
    return int(getattr(candle, "close_time", None) or getattr(candle, "open_time", 0) or 0)


def _empty_execution(index: int, price: Decimal) -> SimulatedExecution:
    return SimulatedExecution(
        reason="invalid_quantity",
        exit_index=index,
        exit_price=price,
        effective_exit_price=price,
        gross_pnl=Decimal("0"),
        fees=Decimal("0"),
        slippage_cost=Decimal("0"),
        funding_cost=Decimal("0"),
        net_pnl=Decimal("0"),
        r_multiple=Decimal("0"),
        closed_quantity=Decimal("0"),
        remaining_quantity=Decimal("0"),
        filled_targets=(),
    )
