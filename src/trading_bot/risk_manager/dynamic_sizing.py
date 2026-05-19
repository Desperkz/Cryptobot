from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_bot.config import RiskConfig
from trading_bot.models import Signal, to_decimal


@dataclass(frozen=True)
class DynamicSizingDecision:
    strategy: str
    risk_pct: Decimal
    leverage: int
    base_risk_pct: Decimal
    raw_risk_pct: Decimal
    multiplier: Decimal
    cap_risk_pct: Decimal
    reasons: tuple[str, ...]

    def to_metadata(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "risk_pct": str(self.risk_pct),
            "leverage": self.leverage,
            "base_risk_pct": str(self.base_risk_pct),
            "raw_risk_pct": str(self.raw_risk_pct),
            "multiplier": str(self.multiplier),
            "cap_risk_pct": str(self.cap_risk_pct),
            "reasons": list(self.reasons),
        }


def dynamic_position_sizing(
    *,
    signal: Signal,
    config: RiskConfig,
    base_risk_pct: Decimal,
    strategy_mode: str = "paper",
) -> DynamicSizingDecision:
    """Return a bounded confidence-based risk/leverage decision for one signal.

    This deliberately changes risk per trade, not the stop distance. Leverage is
    only used as a margin-efficiency selector and is still clipped by the global
    risk config, so a high-confidence signal cannot bypass portfolio caps.
    """

    strategy = str((signal.metadata or {}).get("strategy") or "UNKNOWN").upper()
    base_risk_pct = _clip(to_decimal(base_risk_pct), config.kelly_min_risk_pct, config.kelly_max_risk_pct)
    default_leverage = _clip_int(config.default_leverage, 1, config.max_leverage)

    if not config.dynamic_sizing_enabled:
        return DynamicSizingDecision(
            strategy=strategy,
            risk_pct=base_risk_pct,
            leverage=default_leverage,
            base_risk_pct=base_risk_pct,
            raw_risk_pct=base_risk_pct,
            multiplier=Decimal("1"),
            cap_risk_pct=base_risk_pct,
            reasons=("dynamic_sizing_disabled",),
        )

    reasons: list[str] = []
    multiplier = config.dynamic_strategy_risk_multipliers.get(strategy, Decimal("0.50"))
    reasons.append(f"strategy_multiplier={multiplier}")

    confidence_multiplier = _confidence_multiplier(signal.confidence, config)
    multiplier *= confidence_multiplier
    reasons.append(f"confidence_multiplier={confidence_multiplier}")

    order_flow_multiplier, order_flow_reason, order_flow_score, order_flow_alignment = _order_flow_multiplier(
        signal.metadata or {}
    )
    multiplier *= order_flow_multiplier
    reasons.append(order_flow_reason)

    funding_carry = str((signal.metadata or {}).get("funding_carry") or "")
    if funding_carry == "penalized":
        multiplier *= Decimal("0.75")
        reasons.append("funding_penalty=0.75")

    raw_risk_pct = base_risk_pct * multiplier
    cap = min(
        config.dynamic_sizing_max_risk_pct,
        config.dynamic_strategy_max_risk_pct.get(strategy, config.dynamic_sizing_max_risk_pct),
    )
    if str(strategy_mode).lower() == "shadow":
        cap = min(cap, config.dynamic_sizing_shadow_max_risk_pct)
    risk_pct = _clip(raw_risk_pct, config.dynamic_sizing_min_risk_pct, cap)
    if risk_pct != raw_risk_pct:
        reasons.append(f"risk_clipped_to={risk_pct}")

    leverage = default_leverage
    if config.dynamic_leverage_enabled:
        leverage = _dynamic_leverage(
            config=config,
            base_leverage=default_leverage,
            risk_pct=risk_pct,
            base_risk_pct=base_risk_pct,
            confidence=signal.confidence,
            order_flow_score=order_flow_score,
            order_flow_alignment=order_flow_alignment,
        )
        if leverage != default_leverage:
            reasons.append(f"leverage_adjusted_to={leverage}")

    return DynamicSizingDecision(
        strategy=strategy,
        risk_pct=risk_pct,
        leverage=leverage,
        base_risk_pct=base_risk_pct,
        raw_risk_pct=raw_risk_pct,
        multiplier=multiplier,
        cap_risk_pct=cap,
        reasons=tuple(reasons),
    )


def _confidence_multiplier(confidence: Decimal, config: RiskConfig) -> Decimal:
    confidence = to_decimal(confidence)
    if confidence >= config.dynamic_sizing_elite_confidence:
        return Decimal("1.30")
    if confidence >= config.dynamic_sizing_high_confidence:
        return Decimal("1.15")
    if confidence >= Decimal("0.72"):
        return Decimal("1.00")
    if confidence >= Decimal("0.62"):
        return Decimal("0.75")
    return Decimal("0.50")


def _order_flow_multiplier(metadata: dict[str, Any]) -> tuple[Decimal, str, Decimal, str]:
    order_flow = metadata.get("order_flow")
    if not isinstance(order_flow, dict):
        return Decimal("0.90"), "order_flow=missing", Decimal("0"), "missing"

    score = _decimal_or_zero(order_flow.get("score"))
    alignment = str(order_flow.get("alignment") or "unknown")
    risk_flags = {str(flag) for flag in (order_flow.get("risk_flags") or [])}
    severe_flags = {
        "liquidation_cascade",
        "taker_flow_against",
        "aggressive_delta_against",
        "adverse_liquidity_nearby",
        "crowded_long_funding",
        "crowded_short_funding",
    }

    if risk_flags & severe_flags:
        if alignment == "aligned" and score >= Decimal("0.75"):
            return Decimal("0.85"), "order_flow=aligned_but_risky", score, alignment
        return Decimal("0.55"), "order_flow=hostile", score, alignment
    if alignment == "aligned" and score >= Decimal("0.85"):
        return Decimal("1.25"), "order_flow=elite_aligned", score, alignment
    if alignment == "aligned" and score >= Decimal("0.65"):
        return Decimal("1.12"), "order_flow=aligned", score, alignment
    if alignment == "neutral":
        return Decimal("0.90"), "order_flow=neutral", score, alignment
    if alignment == "against":
        return Decimal("0.60"), "order_flow=against", score, alignment
    return Decimal("0.80"), "order_flow=weak_or_unknown", score, alignment


def _dynamic_leverage(
    *,
    config: RiskConfig,
    base_leverage: int,
    risk_pct: Decimal,
    base_risk_pct: Decimal,
    confidence: Decimal,
    order_flow_score: Decimal,
    order_flow_alignment: str,
) -> int:
    lower = max(1, config.dynamic_leverage_min)
    upper = min(config.max_leverage, config.dynamic_leverage_max)
    leverage = base_leverage

    if risk_pct <= base_risk_pct * Decimal("0.70"):
        leverage -= 1
    elif (
        confidence >= config.dynamic_sizing_elite_confidence
        and order_flow_alignment == "aligned"
        and order_flow_score >= Decimal("0.75")
    ):
        leverage += 2
    elif (
        confidence >= config.dynamic_sizing_high_confidence
        and order_flow_alignment == "aligned"
        and order_flow_score >= Decimal("0.65")
    ):
        leverage += 1

    return _clip_int(leverage, lower, upper)


def _decimal_or_zero(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return to_decimal(value)
    except Exception:
        return Decimal("0")


def _clip(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return max(lower, min(value, upper))


def _clip_int(value: int, lower: int, upper: int) -> int:
    return max(lower, min(int(value), int(upper)))
