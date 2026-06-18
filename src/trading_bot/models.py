from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from typing import Any


class TradingMode(StrEnum):
    BACKTEST = "BACKTEST"
    DRY_RUN = "DRY_RUN"
    PAPER_TRADING = "PAPER_TRADING"
    TESTNET_LIVE = "TESTNET_LIVE"
    MAINNET_LIVE = "MAINNET_LIVE"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class TradingStyle(StrEnum):
    SCALPING = "SCALPING"
    INTRADAY = "INTRADAY"
    SWING = "SWING"
    NO_TRADE = "NO_TRADE"


class MarketRegime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    MOMENTUM = "MOMENTUM"
    UNKNOWN = "UNKNOWN"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ProtectionStatus(StrEnum):
    PENDING = "PENDING"
    PLACED = "PLACED"
    BREAKEVEN = "BREAKEVEN"
    TRAILING = "TRAILING"
    ERROR = "ERROR"


class UserStreamHealth(StrEnum):
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: int
    quote_volume: Decimal = Decimal("0")

    @classmethod
    def from_binance(cls, raw: list[Any]) -> "Candle":
        return cls(
            open_time=int(raw[0]),
            open=to_decimal(raw[1]),
            high=to_decimal(raw[2]),
            low=to_decimal(raw[3]),
            close=to_decimal(raw[4]),
            volume=to_decimal(raw[5]),
            close_time=int(raw[6]),
            quote_volume=to_decimal(raw[7]) if len(raw) > 7 else Decimal("0"),
        )


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_qty: Decimal | None = None

    @classmethod
    def from_exchange_symbol(cls, info: dict[str, Any]) -> "SymbolFilters":
        filters = {item["filterType"]: item for item in info.get("filters", [])}
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE", {})
        price = filters.get("PRICE_FILTER", {})
        min_notional = filters.get("MIN_NOTIONAL", {})
        return cls(
            symbol=info["symbol"],
            tick_size=to_decimal(price.get("tickSize", "0.00000001")),
            step_size=to_decimal(lot.get("stepSize", "0.00000001")),
            min_qty=to_decimal(lot.get("minQty", "0")),
            min_notional=to_decimal(min_notional.get("notional", "0")),
            max_qty=to_decimal(lot["maxQty"]) if lot.get("maxQty") else None,
        )

    def round_price(self, value: Decimal) -> Decimal:
        return floor_to_step(value, self.tick_size)

    def round_quantity(self, value: Decimal) -> Decimal:
        return floor_to_step(value, self.step_size)


@dataclass(frozen=True)
class MarketMetrics:
    symbol: str
    quote_volume_24h: Decimal
    spread_bps: Decimal
    top_book_liquidity_usdt: Decimal = Decimal("0")
    funding_rate: Decimal | None = None
    open_interest: Decimal | None = None
    order_book_imbalance: Decimal = Decimal("0")
    taker_buy_ratio: Decimal | None = None
    open_interest_change_pct: Decimal | None = None
    aggressive_buy_sell_delta: Decimal = Decimal("0")


@dataclass(frozen=True)
class EdgeSnapshot:
    liquidity_sweep: bool
    sweep_direction: Direction
    absorption: bool
    absorption_direction: Direction
    structure_break: bool
    structure_direction: Direction
    liquidation_zone_nearby: bool
    score: Decimal
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class UniverseAsset:
    symbol: str
    base_asset: str
    quote_asset: str
    market_cap_rank: int
    market_cap_usd: Decimal | None
    filters: SymbolFilters
    metrics: MarketMetrics


@dataclass(frozen=True)
class RegimeSnapshot:
    regime: MarketRegime
    atr_pct: Decimal
    trend_strength: Decimal
    momentum_pct: Decimal
    reason: str


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: Direction
    style: TradingStyle
    entry_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    confidence: Decimal
    reason: str
    timeframe: str = "15m"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_tradeable(self) -> bool:
        return (
            self.direction in {Direction.LONG, Direction.SHORT}
            and self.style != TradingStyle.NO_TRADE
            and self.stop_loss is not None
            and self.take_profit is not None
        )


@dataclass(frozen=True)
class Position:
    symbol: str
    direction: Direction
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal | None = None
    liquidation_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    managed_by_bot: bool = False
    protection_status: ProtectionStatus = ProtectionStatus.PENDING
    unrealized_pnl: Decimal = Decimal("0")
    source: str = "LOCAL"
    leverage: int | None = None
    initial_margin: Decimal | None = None

    @property
    def notional(self) -> Decimal:
        price = self.mark_price or self.entry_price
        return abs(self.quantity * price)


@dataclass(frozen=True)
class TakeProfitTarget:
    name: str
    price: Decimal
    quantity: Decimal
    fraction: Decimal
    reward_risk: Decimal
    move_stop_to_breakeven: bool = False
    activate_trailing: bool = False


@dataclass(frozen=True)
class ProtectionPlan:
    initial_stop: Decimal
    breakeven_price: Decimal
    breakeven_after_target: str | None
    trailing_enabled: bool
    trailing_activation_reward_risk: Decimal
    trailing_callback_rate_pct: Decimal


@dataclass(frozen=True)
class RiskPlan:
    symbol: str
    direction: Direction
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    quantity: Decimal
    notional: Decimal
    initial_margin: Decimal
    risk_amount: Decimal
    reward_amount: Decimal
    risk_pct: Decimal
    leverage: int
    reward_risk: Decimal
    partial_take_profits: tuple[TakeProfitTarget, ...] = ()
    protection: ProtectionPlan | None = None
    signal_metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderResult:
    symbol: str
    mode: TradingMode
    accepted: bool
    message: str
    trade_id: str | None = None
    client_order_ids: dict[str, str] = field(default_factory=dict)
    entry_order: dict[str, Any] | None = None
    stop_order: dict[str, Any] | None = None
    take_profit_order: dict[str, Any] | None = None
    take_profit_orders: tuple[dict[str, Any], ...] = ()
    trailing_order: dict[str, Any] | None = None
    execution_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionIssue:
    symbol: str
    severity: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UserStreamStatus:
    health: UserStreamHealth
    connected: bool
    last_event_at: float | None
    last_order_event_at: float | None
    last_account_event_at: float | None
    reconnects: int
    last_error: str | None = None


@dataclass(frozen=True)
class LearningRecommendation:
    scope: str
    key: str
    metric: str
    value: Decimal
    recommendation: str
    trades: int


@dataclass(frozen=True)
class PerformanceSnapshot:
    symbol: str | None
    trades: int
    winrate: Decimal
    expectancy_r: Decimal
    profit_factor: Decimal
    avg_r_multiple: Decimal
    max_drawdown_pct: Decimal
