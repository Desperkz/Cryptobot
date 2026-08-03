from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced by load_config in minimal test envs
    yaml = None

from trading_bot.models import TradingMode, to_decimal


class ConfigError(RuntimeError):
    pass


STRATEGY_RUNTIME_MODES = {"live", "paper", "shadow", "disabled"}
STRATEGY_EXECUTION_MODES = {"live", "paper"}


def _strategy_name(name: str) -> str:
    return str(name).strip().upper()


@dataclass(frozen=True)
class ExchangeConfig:
    mainnet_base_url: str
    testnet_base_url: str
    websocket_mainnet_url: str
    websocket_testnet_url: str
    recv_window_ms: int = 5000
    request_timeout_sec: int = 15
    max_retries: int = 4


@dataclass(frozen=True)
class SafetyConfig:
    enable_mainnet_live: bool = False
    mainnet_confirmation: str = ""
    required_mainnet_confirmation: str = "I_UNDERSTAND_MAINNET_RISK"
    production_unlock_file: str = "data/production_unlock.json"
    require_backtest_approval_for_mainnet: bool = True
    # ФИКС: раньше mainnet открывался по голому флагу backtest_approved: true
    # в JSON, а CLI-команда `backtest` гоняла naive EMA-смоук-тест, который не
    # имеет отношения к торгуемым стратегиям. Теперь требуется артефакт
    # walk-forward отчёта по РЕАЛЬНО включённым стратегиям.
    require_walkforward_report_for_mainnet: bool = True
    walkforward_report_path: str = "data/walkforward_report.json"
    walkforward_max_report_age_days: int = 45
    min_walkforward_out_of_sample_trades: int = 150
    min_walkforward_profit_factor: Decimal = Decimal("1.20")
    min_walkforward_expectancy_r: Decimal = Decimal("0.10")
    require_paper_approval_for_mainnet: bool = True
    max_mainnet_risk_per_trade_pct: Decimal = Decimal("0.02")
    max_mainnet_concurrent_positions: int = 1
    max_mainnet_leverage: int = 2
    mainnet_allowed_strategies: list[str] = field(default_factory=lambda: ["SQUEEZE_BREAKOUT"])
    emergency_stop_file: str = "data/emergency_stop.flag"
    allow_hedge_mode: bool = False
    require_stop_loss: bool = True
    require_take_profit: bool = True
    block_new_trade_if_any_position: bool = True
    block_new_trade_if_same_symbol: bool = True
    adopt_manual_positions: bool = True
    repair_missing_protection: bool = False
    reconcile_orders_on_start: bool = True
    require_exclusive_live_bot: bool = True
    live_conflicting_service_names: list[str] = field(default_factory=lambda: ["trading-bot", "trading-bot-v2"])
    emergency_cancel_orders_in_live: bool = True
    emergency_close_positions_in_live: bool = False


@dataclass(frozen=True)
class AccountConfig:
    starting_deposit_tenge: Decimal
    starting_deposit_usdt: Decimal | None
    manual_tenge_usdt_rate: Decimal | None
    quote_asset: str = "USDT"

    @property
    def initial_equity_usdt(self) -> Decimal | None:
        if self.starting_deposit_usdt is not None:
            return self.starting_deposit_usdt
        if self.manual_tenge_usdt_rate is not None and self.manual_tenge_usdt_rate > 0:
            return self.starting_deposit_tenge / self.manual_tenge_usdt_rate
        return None


@dataclass(frozen=True)
class TradingConfig:
    timeframes: list[str]
    poll_interval_sec: int
    max_symbols: int
    margin_type: str
    position_mode: str
    order_working_type: str
    min_backtest_trades: int
    min_backtest_profit_factor: Decimal
    min_backtest_max_drawdown_pct: Decimal


@dataclass(frozen=True)
class UniverseConfig:
    market_cap_top_n: int
    fetch_extra_top_n: int
    quote_asset: str
    excluded_assets: set[str]
    min_24h_quote_volume_usdt: Decimal
    max_spread_bps: Decimal
    min_order_book_top_liquidity_usdt: Decimal
    cache_ttl_sec: int
    min_symbol_quality_score: Decimal = Decimal("70")
    quality_spread_weight: Decimal = Decimal("0.35")
    quality_liquidity_weight: Decimal = Decimal("0.40")
    quality_volume_weight: Decimal = Decimal("0.25")


@dataclass(frozen=True)
class StrategyConfig:
    ema_fast: int
    ema_mid: int
    ema_slow: int
    rsi_period: int
    atr_period: int
    volume_lookback: int
    min_volume_ratio: Decimal
    min_atr_pct: Decimal
    max_atr_pct: Decimal
    stop_atr_multiplier: dict[str, Decimal]
    take_profit_rr: dict[str, Decimal]
    use_funding_filter: bool
    max_abs_funding_rate: Decimal
    enabled_strategies: list[str] = field(default_factory=lambda: ["TREND_FOLLOWING"])
    strategy_modes: dict[str, str] = field(default_factory=dict)
    shadow_order_flow_hard_gate: bool = False
    # Режим OF-измерения. В "measure" только отдельный SQZ bucket может
    # обойти слабый mixed-flow; retest, структура, relative strength и hostile
    # flow всегда остаются жёсткими. Глобального bypass здесь нет.
    order_flow_entry_gate_mode: str = "strict"
    order_flow_hostile_score_floor: Decimal = Decimal("0.70")
    order_flow_mixed_score_floor: Decimal = Decimal("0.45")
    # Совместимый конфигурационный список для аудита. Новый measurement bucket
    # не принимает сигнал ни с одним risk flag.
    order_flow_always_hard_flags: list[str] = field(default_factory=lambda: ["liquidation_cascade"])
    squeeze_order_flow_measurement_enabled: bool = False
    squeeze_order_flow_measurement_min_score: Decimal = Decimal("0.30")
    squeeze_order_flow_measurement_risk_cap_pct: Decimal = Decimal("0.005")
    # Parallel counterfactual SQZ cohorts. They remain shadow-only and relax
    # exactly one non-critical gate per virtual trade.
    squeeze_gate_cohort_shadow_enabled: bool = False
    squeeze_gate_cohort_shadow_risk_cap_pct: Decimal = Decimal("0.005")
    # Узкий paper-эксперимент: только SQZ с нейтральной relative strength
    # после остальных чистых подтверждений. Это не ослабляет hostile-flow,
    # retest или structure-break safety gates.
    squeeze_controlled_paper_enabled: bool = False
    squeeze_controlled_paper_min_order_flow_score: Decimal = Decimal("0.65")
    squeeze_controlled_paper_risk_cap_pct: Decimal = Decimal("0.005")
    squeeze_dynamic_neutral_shadow_enabled: bool = False
    squeeze_dynamic_neutral_shadow_min_order_flow_score: Decimal = Decimal("0.65")
    squeeze_dynamic_neutral_shadow_risk_cap_pct: Decimal = Decimal("0.0025")
    # A fresh, isolated virtual cohort for candidates that were historically
    # diagnostic-only. It cannot change paper/live admission or promotion.
    shadow_revalidation_enabled: bool = False
    shadow_revalidation_cohort: str = ""
    shadow_revalidation_risk_cap_pct: Decimal = Decimal("0.0025")
    shadow_revalidation_strategies: list[str] = field(default_factory=list)
    # Independent virtual cohorts that relax exactly one context gate. These
    # never change paper/live admission and keep the source exit profile.
    shadow_gate_counterfactual_enabled: bool = False
    shadow_gate_counterfactual_cohort: str = ""
    shadow_gate_counterfactual_risk_cap_pct: Decimal = Decimal("0.0025")
    shadow_gate_counterfactual_gates: list[str] = field(default_factory=list)
    mean_reversion_deviation_atr: Decimal = Decimal("2.0")
    mean_reversion_rsi_oversold: Decimal = Decimal("28")
    mean_reversion_rsi_overbought: Decimal = Decimal("72")
    mean_reversion_stop_atr_multiplier: Decimal = Decimal("1.0")
    mean_reversion_take_profit_rr: Decimal = Decimal("1.4")
    mean_reversion_min_volume_ratio: Decimal = Decimal("1.05")
    mean_reversion_min_edge_score: Decimal = Decimal("0.40")
    mean_reversion_min_confluence: int = 4
    mean_reversion_require_divergence: bool = True
    mean_reversion_require_edge_confirmation: bool = True
    mean_reversion_btc_direction_gate_enabled: bool = True
    mean_reversion_btc_direction_gate_pct: Decimal = Decimal("0.012")
    mean_reversion_order_flow_gate_enabled: bool = True
    mean_reversion_min_order_flow_score: Decimal = Decimal("0.25")
    mean_reversion_min_net_reward_risk: Decimal = Decimal("1.15")
    mean_reversion_min_expected_net_r: Decimal = Decimal("0.05")
    mean_reversion_expected_winrate_floor: Decimal = Decimal("0.48")
    trend_pullback_min_volume_ratio: Decimal = Decimal("1.10")
    trend_pullback_min_trend_strength: Decimal = Decimal("0.35")
    trend_pullback_min_depth_atr: Decimal = Decimal("0.25")
    trend_pullback_max_depth_atr: Decimal = Decimal("2.20")
    trend_pullback_stop_atr_multiplier: Decimal = Decimal("1.20")
    trend_pullback_take_profit_rr: Decimal = Decimal("1.60")
    trend_pullback_min_confluence: int = 4
    trend_pullback_min_edge_score: Decimal = Decimal("0.25")
    liquidity_sweep_lookback: int = 18
    liquidity_sweep_stop_atr_multiplier: Decimal = Decimal("1.10")
    liquidity_sweep_take_profit_rr: Decimal = Decimal("1.55")
    liquidity_sweep_min_edge_score: Decimal = Decimal("0.65")
    liquidity_sweep_min_reclaim_atr: Decimal = Decimal("0.90")
    liquidity_sweep_follow_through_min_body_atr: Decimal = Decimal("0.25")
    vwap_reversion_lookback: int = 96
    vwap_reversion_deviation_atr: Decimal = Decimal("1.60")
    vwap_reversion_max_deviation_atr: Decimal = Decimal("6.00")
    vwap_reversion_min_volume_ratio: Decimal = Decimal("1.20")
    vwap_reversion_watch_deviation_atr: Decimal = Decimal("1.80")
    vwap_reversion_watch_max_deviation_atr: Decimal = Decimal("8.00")
    vwap_reversion_watch_min_volume_ratio: Decimal = Decimal("1.05")
    vwap_reversion_min_progress_atr: Decimal = Decimal("0.80")
    vwap_reversion_watch_min_progress_atr: Decimal = Decimal("1.10")
    vwap_reversion_reversal_min_body_atr: Decimal = Decimal("0.20")
    vwap_reversion_stop_atr_multiplier: Decimal = Decimal("1.00")
    vwap_reversion_take_profit_rr: Decimal = Decimal("1.15")
    momentum_continuation_min_volume_ratio: Decimal = Decimal("1.40")
    momentum_continuation_stop_atr_multiplier: Decimal = Decimal("1.30")
    momentum_continuation_take_profit_rr: Decimal = Decimal("1.60")
    momentum_continuation_min_edge_score: Decimal = Decimal("0.20")
    range_grid_lookback: int = 48
    range_grid_stop_atr_multiplier: Decimal = Decimal("1.00")
    range_grid_take_profit_rr: Decimal = Decimal("1.40")
    range_grid_entry_zone_pct: Decimal = Decimal("0.10")
    range_grid_rsi_long_max: Decimal = Decimal("35")
    range_grid_rsi_short_min: Decimal = Decimal("65")
    funding_carry_filter_enabled: bool = True
    funding_carry_penalty_threshold: Decimal = Decimal("0.0005")
    funding_carry_block_threshold: Decimal = Decimal("0.0012")
    squeeze_release_lookback_bars: int = 4
    squeeze_release_min_breakout_atr: Decimal = Decimal("0.03")
    squeeze_early_min_breakout_atr: Decimal = Decimal("0.10")
    squeeze_early_min_bars_extra: int = 4
    squeeze_early_min_volume_ratio: Decimal = Decimal("1.50")
    squeeze_max_extension_atr: Decimal = Decimal("2.40")
    squeeze_retest_enabled: bool = True
    squeeze_retest_required_after_release_offset: int = 2
    squeeze_retest_lookback_bars: int = 3
    squeeze_retest_tolerance_atr: Decimal = Decimal("0.35")
    squeeze_retest_min_rejection_body_atr: Decimal = Decimal("0.05")

    def configured_strategies(self) -> list[str]:
        ordered: list[str] = []
        for name in [*self.enabled_strategies, *self.strategy_modes.keys()]:
            normalized = _strategy_name(name)
            if normalized and normalized not in ordered:
                ordered.append(normalized)
        return ordered

    def mode_for_strategy(self, strategy: str) -> str:
        normalized = _strategy_name(strategy)
        configured = {_strategy_name(name): str(mode).strip().lower() for name, mode in self.strategy_modes.items()}
        if normalized in configured:
            return configured[normalized]
        return "paper" if normalized in {_strategy_name(name) for name in self.enabled_strategies} else "disabled"

    def mode_summary(self) -> dict[str, str]:
        return {name: self.mode_for_strategy(name) for name in self.configured_strategies()}

    def execution_strategies(self, trading_mode: TradingMode) -> list[str]:
        allowed_modes = {"live"} if trading_mode == TradingMode.MAINNET_LIVE else STRATEGY_EXECUTION_MODES
        return [
            name
            for name in self.configured_strategies()
            if self.mode_for_strategy(name) in allowed_modes
        ]

    def shadow_strategies(self) -> list[str]:
        return [
            name
            for name in self.configured_strategies()
            if self.mode_for_strategy(name) == "shadow"
        ]


@dataclass(frozen=True)
class MarketFilterConfig:
    btc_4h_drop_filter_enabled: bool
    btc_4h_max_drop_pct: Decimal
    block_longs_when_btc_weak: bool
    block_all_when_btc_weak: bool
    utc_session_filter_enabled: bool
    avoid_utc_hours: set[int]
    use_self_learning_filters: bool
    use_open_interest_filter: bool = False
    oi_drop_cascade_pct: Decimal = Decimal("-5.0")
    oi_rise_longs_blocked_pct: Decimal = Decimal("3.0")
    high_confidence_squeeze_session_override: bool = False
    high_confidence_squeeze_min_confidence: Decimal = Decimal("0.80")
    high_confidence_squeeze_allowed_hours: set[int] = field(default_factory=lambda: {0, 1, 2})
    # Симметричный контртрендовый фильтр: блокируем шорты в растущем рынке
    # (зеркально существующему block_longs_when_btc_weak).
    counter_trend_filter_enabled: bool = True
    btc_4h_min_rise_pct_for_short_block: Decimal = Decimal("0.015")
    block_shorts_when_btc_strong: bool = True
    symbol_4h_trend_filter_enabled: bool = True
    symbol_4h_trend_lookback_bars: int = 12
    symbol_4h_max_rise_pct_for_short: Decimal = Decimal("0.03")
    symbol_4h_max_drop_pct_for_long: Decimal = Decimal("-0.03")


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: Decimal
    aggressive_risk_threshold_pct: Decimal
    max_concurrent_positions: int
    max_leverage: int
    default_leverage: int
    max_daily_loss_pct: Decimal
    cooldown_after_losses: int
    cooldown_minutes: int
    symbol_cooldown_after_loss_minutes: int
    strategy_reentry_cooldown_minutes: int
    strategy_reentry_winning_cooldown_minutes: int
    scale_in_enabled: bool
    max_scale_ins_per_symbol_strategy: int
    scale_in_risk_multiplier: Decimal
    scale_in_min_unrealized_r: Decimal
    scale_in_independent_signal_minutes: int
    trade_cluster_window_minutes: int
    max_portfolio_risk_pct: Decimal
    max_correlated_group_risk_pct: Decimal
    max_margin_usage_pct: Decimal
    min_reward_risk: Decimal
    taker_fee_bps: Decimal
    slippage_bps: Decimal
    funding_buffer_bps: Decimal
    liquidation_buffer_pct: Decimal
    require_liquidation_check_in_live: bool
    max_funding_impact_bps: Decimal = Decimal("8.0")
    funding_impact_holding_hours: Decimal = Decimal("8")
    adaptive_kelly_enabled: bool = True
    kelly_min_sample_trades: int = 50
    kelly_lookback_trades: int = 50
    kelly_fraction: Decimal = Decimal("0.5")
    kelly_min_risk_pct: Decimal = Decimal("0.005")
    kelly_max_risk_pct: Decimal = Decimal("0.03")
    dynamic_sizing_enabled: bool = True
    dynamic_sizing_min_risk_pct: Decimal = Decimal("0.003")
    dynamic_sizing_max_risk_pct: Decimal = Decimal("0.025")
    dynamic_sizing_shadow_max_risk_pct: Decimal = Decimal("0.020")
    dynamic_sizing_high_confidence: Decimal = Decimal("0.82")
    dynamic_sizing_elite_confidence: Decimal = Decimal("0.90")
    dynamic_leverage_enabled: bool = True
    dynamic_leverage_min: int = 1
    dynamic_leverage_max: int = 5
    dynamic_strategy_risk_multipliers: dict[str, Decimal] = field(
        default_factory=lambda: {
            "SQUEEZE_BREAKOUT": Decimal("1.00"),
            "SQUEEZE_BREAKOUT_DYNAMIC": Decimal("0.65"),
            "SQUEEZE_BREAKOUT_DYNAMIC_UPD": Decimal("0.65"),
            "MEAN_REVERSION": Decimal("0.65"),
            "TREND_PULLBACK": Decimal("0.75"),
            "LIQUIDITY_SWEEP_REVERSAL": Decimal("0.60"),
            "VWAP_REVERSION": Decimal("0.45"),
            "VWAP_REVERSION_WATCH": Decimal("0.50"),
            "MOMENTUM_CONTINUATION": Decimal("0.75"),
            "RANGE_GRID": Decimal("0.25"),
            "TREND_FOLLOWING": Decimal("0.60"),
        }
    )
    dynamic_strategy_max_risk_pct: dict[str, Decimal] = field(
        default_factory=lambda: {
            "SQUEEZE_BREAKOUT": Decimal("0.020"),
            "SQUEEZE_BREAKOUT_DYNAMIC": Decimal("0.012"),
            "SQUEEZE_BREAKOUT_DYNAMIC_UPD": Decimal("0.012"),
            "MEAN_REVERSION": Decimal("0.014"),
            "TREND_PULLBACK": Decimal("0.016"),
            "LIQUIDITY_SWEEP_REVERSAL": Decimal("0.012"),
            "VWAP_REVERSION": Decimal("0.010"),
            "VWAP_REVERSION_WATCH": Decimal("0.012"),
            "MOMENTUM_CONTINUATION": Decimal("0.016"),
            "RANGE_GRID": Decimal("0.006"),
            "TREND_FOLLOWING": Decimal("0.006"),
        }
    )
    realtime_correlation_enabled: bool = True
    realtime_correlation_threshold: Decimal = Decimal("0.70")
    realtime_correlation_lookback: int = 48
    block_live_when_correlation_unavailable: bool = True
    correlation_groups: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class PartialTakeProfitConfig:
    name: str
    reward_risk: Decimal
    fraction: Decimal
    move_stop_to_breakeven: bool = False
    activate_trailing: bool = False


@dataclass(frozen=True)
class TradeManagementConfig:
    user_stream_required_for_live: bool
    user_stream_stale_after_sec: int
    user_stream_reconnect_backoff_sec: int
    rest_reconciliation_when_stale: bool
    partial_take_profits: list[PartialTakeProfitConfig]
    breakeven_offset_bps: Decimal
    trailing_enabled: bool
    trailing_activation_reward_risk: Decimal
    trailing_callback_rate_pct: dict[str, Decimal]
    trailing_client_side: bool = False
    strategy_exit_profiles: dict[str, list[PartialTakeProfitConfig]] = field(default_factory=dict)
    min_first_target_net_reward_risk: Decimal = Decimal("0.20")
    strategy_min_first_target_net_reward_risk: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class EdgeFilterConfig:
    enabled: bool
    order_book_imbalance_min: Decimal
    taker_buy_ratio_long_min: Decimal
    taker_buy_ratio_short_max: Decimal
    open_interest_change_min_pct: Decimal
    liquidity_sweep_lookback: int = 12
    liquidity_sweep_threshold_bps: Decimal = Decimal("12")
    absorption_wick_body_ratio: Decimal = Decimal("1.8")
    aggressive_flow_delta_min: Decimal = Decimal("0.08")
    structure_break_lookback: int = 20
    liquidation_zone_distance_bps: Decimal = Decimal("35")
    liquidation_cluster_filter_enabled: bool = False


@dataclass(frozen=True)
class MLConfig:
    enabled: bool
    min_prediction_confidence: Decimal
    model_path: str
    training_data_path: str = "data/ml/features.jsonl"
    retrain_min_trades: int = 500
    decision_min_trades: int = 500
    enforce_decisions: bool = False
    validation_report_path: str = "data/ml/walk_forward_report.json"
    require_validation_improvement_for_live: bool = True
    min_validation_trade_coverage: Decimal = Decimal("0.50")
    adaptive_thresholds_enabled: bool = True


@dataclass(frozen=True)
class AnalyticsConfig:
    min_trades_for_adaptation: int
    min_expectancy_r: Decimal
    min_winrate: Decimal
    disable_symbol_after_bad_trades: int
    segment_min_trades: int = 12
    rsi_bucket_size: int = 4
    atr_bucket_size_pct: Decimal = Decimal("0.4")
    bad_segment_expectancy_r: Decimal = Decimal("-0.10")
    # Эпоха статистики: self-learning учитывает только сделки, закрытые после
    # этой даты (ISO, например "2026-07-08"). Ставится в день деплоя изменений
    # логики, чтобы правила не выводились из сделок старой конфигурации.
    stats_epoch: str | None = None


@dataclass(frozen=True)
class DatabaseConfig:
    url: str
    postgres_url: str = ""


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool
    send_daily_report_utc: str
    cohort_report_enabled: bool = True
    cohort_report_interval_sec: int = 900


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    file: str


@dataclass(frozen=True)
class Secrets:
    binance_api_key: str | None = None
    binance_api_secret: str | None = None
    binance_testnet_api_key: str | None = None
    binance_testnet_api_secret: str | None = None
    coingecko_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


@dataclass(frozen=True)
class AppConfig:
    mode: TradingMode
    exchange: ExchangeConfig
    safety: SafetyConfig
    account: AccountConfig
    trading: TradingConfig
    universe: UniverseConfig
    strategy: StrategyConfig
    market_filters: MarketFilterConfig
    risk: RiskConfig
    trade_management: TradeManagementConfig
    edge_filters: EdgeFilterConfig
    ml: MLConfig
    analytics: AnalyticsConfig
    database: DatabaseConfig
    telegram: TelegramConfig
    web: WebConfig
    logging: LoggingConfig
    secrets: Secrets

    @property
    def is_live(self) -> bool:
        return self.mode in {TradingMode.TESTNET_LIVE, TradingMode.MAINNET_LIVE}

    @property
    def is_testnet(self) -> bool:
        return self.mode == TradingMode.TESTNET_LIVE

    @property
    def rest_base_url(self) -> str:
        if self.mode == TradingMode.MAINNET_LIVE:
            return self.exchange.mainnet_base_url
        return self.exchange.testnet_base_url

    @property
    def websocket_base_url(self) -> str:
        if self.mode == TradingMode.MAINNET_LIVE:
            return self.exchange.websocket_mainnet_url
        return self.exchange.websocket_testnet_url

    @property
    def api_key(self) -> str | None:
        if self.mode == TradingMode.MAINNET_LIVE:
            return self.secrets.binance_api_key
        return self.secrets.binance_testnet_api_key

    @property
    def api_secret(self) -> str | None:
        if self.mode == TradingMode.MAINNET_LIVE:
            return self.secrets.binance_api_secret
        return self.secrets.binance_testnet_api_secret

    def validate(self) -> list[str]:
        warnings: list[str] = []
        allow_unproven_high_risk = os.getenv("BOT_ALLOW_UNPROVEN_HIGH_RISK", "0") == "1"
        strategy_modes = self.strategy.mode_summary()
        invalid_strategy_modes = {
            name: mode
            for name, mode in strategy_modes.items()
            if mode not in STRATEGY_RUNTIME_MODES
        }
        if invalid_strategy_modes:
            details = ", ".join(f"{name}={mode}" for name, mode in sorted(invalid_strategy_modes.items()))
            raise ConfigError(
                "strategy.strategy_modes contains invalid values; use live, paper, shadow, or disabled: "
                f"{details}."
            )
        if self.mode == TradingMode.MAINNET_LIVE:
            if not self.safety.enable_mainnet_live:
                raise ConfigError("MAINNET_LIVE is blocked: set safety.enable_mainnet_live=true intentionally.")
            if self.safety.mainnet_confirmation != self.safety.required_mainnet_confirmation:
                raise ConfigError("MAINNET_LIVE is blocked: invalid mainnet confirmation phrase.")
            if not self.secrets.binance_api_key or not self.secrets.binance_api_secret:
                raise ConfigError("MAINNET_LIVE requires BINANCE_API_KEY and BINANCE_API_SECRET.")
            if self.risk.risk_per_trade_pct > self.safety.max_mainnet_risk_per_trade_pct:
                raise ConfigError(
                    "MAINNET_LIVE blocked: risk_per_trade_pct exceeds safety.max_mainnet_risk_per_trade_pct."
                )
            if self.risk.max_concurrent_positions > self.safety.max_mainnet_concurrent_positions:
                raise ConfigError(
                    "MAINNET_LIVE blocked: max_concurrent_positions exceeds safety.max_mainnet_concurrent_positions."
                )
            if self.risk.max_leverage > self.safety.max_mainnet_leverage:
                raise ConfigError("MAINNET_LIVE blocked: max_leverage exceeds safety.max_mainnet_leverage.")
            live_strategies = set(self.strategy.execution_strategies(self.mode))
            disallowed = live_strategies - set(self.safety.mainnet_allowed_strategies)
            if disallowed:
                raise ConfigError(
                    "MAINNET_LIVE blocked: disallowed live strategies enabled: "
                    f"{', '.join(sorted(disallowed))}."
                )
            active_conflicts = active_systemd_services(self.safety.live_conflicting_service_names)
            if self.safety.require_exclusive_live_bot and active_conflicts:
                raise ConfigError(
                    "MAINNET_LIVE blocked: conflicting bot services are active: "
                    f"{', '.join(active_conflicts)}. Stop them before using real money."
                )
            if self.ml.enabled and self.ml.require_validation_improvement_for_live:
                self._validate_ml_live_readiness()
            self._validate_production_unlock()

        if self.mode == TradingMode.TESTNET_LIVE:
            if not self.secrets.binance_testnet_api_key or not self.secrets.binance_testnet_api_secret:
                raise ConfigError("TESTNET_LIVE requires BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET.")
        if self.is_live and self.trade_management.user_stream_required_for_live:
            warnings.append("Live mode requires healthy Binance user data stream; new entries are blocked if stale.")
        required_timeframes = {"15m", "1h", "4h"}
        missing_timeframes = required_timeframes - set(self.trading.timeframes)
        if missing_timeframes:
            raise ConfigError(
                "trading.timeframes must include 15m, 1h, and 4h; "
                f"missing: {', '.join(sorted(missing_timeframes))}."
            )

        if self.risk.max_leverage > 5:
            raise ConfigError("max_leverage must be <= 5 for this safety profile.")
        if self.risk.default_leverage > self.risk.max_leverage:
            raise ConfigError("default_leverage cannot exceed max_leverage.")
        if self.risk.max_concurrent_positions < 1:
            raise ConfigError("max_concurrent_positions must be >= 1.")
        if self.strategy.order_flow_entry_gate_mode not in {"strict", "measure"}:
            raise ConfigError(
                "strategy.order_flow_entry_gate_mode must be strict or measure; global OF bypass is disabled."
            )
        if not (Decimal("0") < self.strategy.order_flow_hostile_score_floor <= Decimal("1")):
            raise ConfigError("strategy.order_flow_hostile_score_floor must be within (0, 1].")
        if not (Decimal("0") < self.strategy.order_flow_mixed_score_floor <= Decimal("1")):
            raise ConfigError("strategy.order_flow_mixed_score_floor must be within (0, 1].")
        if self.strategy.squeeze_order_flow_measurement_enabled:
            if self.strategy.order_flow_entry_gate_mode != "measure":
                raise ConfigError(
                    "squeeze_order_flow_measurement_enabled requires order_flow_entry_gate_mode=measure."
                )
            if self.strategy.mode_for_strategy("SQUEEZE_BREAKOUT_OF_MEASURE") != "paper":
                raise ConfigError(
                    "SQUEEZE_BREAKOUT_OF_MEASURE must be configured as paper while measurement is enabled."
                )
            if not (
                Decimal("0")
                < self.strategy.squeeze_order_flow_measurement_min_score
                < self.strategy.order_flow_mixed_score_floor
            ):
                raise ConfigError(
                    "strategy.squeeze_order_flow_measurement_min_score must be positive and below order_flow_mixed_score_floor."
                )
            if not (
                Decimal("0")
                < self.strategy.squeeze_order_flow_measurement_risk_cap_pct
                <= self.risk.risk_per_trade_pct
            ):
                raise ConfigError(
                    "strategy.squeeze_order_flow_measurement_risk_cap_pct must be positive and no higher than risk_per_trade_pct."
                )
        if self.strategy.squeeze_gate_cohort_shadow_enabled:
            cohort_strategies = {
                "SQZ_STRICT_CONTROL_SHADOW",
                "SQZ_OF_AGAINST_SHADOW",
                "SQZ_OF_HOSTILE_SHADOW",
                "SQZ_OF_ABSORPTION_SHADOW",
                "SQZ_RS_NEUTRAL_SHADOW",
                "SQZ_NO_RETEST_SHADOW",
            }
            incorrectly_configured = sorted(
                strategy
                for strategy in cohort_strategies
                if self.strategy.mode_for_strategy(strategy) != "shadow"
            )
            if incorrectly_configured:
                raise ConfigError(
                    "squeeze_gate_cohort_shadow_enabled requires shadow mode for: "
                    + ", ".join(incorrectly_configured)
                )
            if not (
                Decimal("0")
                < self.strategy.squeeze_gate_cohort_shadow_risk_cap_pct
                <= self.risk.risk_per_trade_pct
            ):
                raise ConfigError(
                    "strategy.squeeze_gate_cohort_shadow_risk_cap_pct must be positive and no higher than risk_per_trade_pct."
                )
        if self.strategy.squeeze_controlled_paper_enabled:
            if not (Decimal("0") < self.strategy.squeeze_controlled_paper_risk_cap_pct <= self.risk.risk_per_trade_pct):
                raise ConfigError(
                    "strategy.squeeze_controlled_paper_risk_cap_pct must be positive and no higher than risk_per_trade_pct."
                )
            if not (Decimal("0") < self.strategy.squeeze_controlled_paper_min_order_flow_score <= Decimal("1")):
                raise ConfigError(
                    "strategy.squeeze_controlled_paper_min_order_flow_score must be within (0, 1]."
                )
        if self.strategy.squeeze_dynamic_neutral_shadow_enabled:
            if not (
                Decimal("0")
                < self.strategy.squeeze_dynamic_neutral_shadow_risk_cap_pct
                <= self.risk.risk_per_trade_pct
            ):
                raise ConfigError(
                    "strategy.squeeze_dynamic_neutral_shadow_risk_cap_pct must be positive and no higher than risk_per_trade_pct."
                )
            if not (
                Decimal("0")
                < self.strategy.squeeze_dynamic_neutral_shadow_min_order_flow_score
                <= Decimal("1")
            ):
                raise ConfigError(
                    "strategy.squeeze_dynamic_neutral_shadow_min_order_flow_score must be within (0, 1]."
                )
        if self.strategy.shadow_revalidation_enabled:
            if not self.strategy.shadow_revalidation_cohort:
                raise ConfigError(
                    "strategy.shadow_revalidation_cohort is required while shadow revalidation is enabled."
                )
            if not self.strategy.shadow_revalidation_strategies:
                raise ConfigError(
                    "strategy.shadow_revalidation_strategies must contain at least one shadow strategy."
                )
            if not (
                Decimal("0")
                < self.strategy.shadow_revalidation_risk_cap_pct
                <= self.risk.risk_per_trade_pct
            ):
                raise ConfigError(
                    "strategy.shadow_revalidation_risk_cap_pct must be positive and no higher than risk_per_trade_pct."
                )
            non_shadow = sorted(
                strategy
                for strategy in self.strategy.shadow_revalidation_strategies
                if self.strategy.mode_for_strategy(strategy) != "shadow"
            )
            if non_shadow:
                raise ConfigError(
                    "strategy.shadow_revalidation_strategies must be configured as shadow: "
                    + ", ".join(non_shadow)
                )
        if self.strategy.shadow_gate_counterfactual_enabled:
            allowed_gates = {
                "RS_NEUTRAL",
                "RS_AGAINST",
                "MISSING_OI",
                "NO_RETEST",
                "NEAR_LIQUIDITY",
            }
            if not self.strategy.shadow_gate_counterfactual_cohort:
                raise ConfigError(
                    "strategy.shadow_gate_counterfactual_cohort is required while counterfactuals are enabled."
                )
            configured_gates = {
                str(gate).strip().upper()
                for gate in self.strategy.shadow_gate_counterfactual_gates
                if str(gate).strip()
            }
            if not configured_gates:
                raise ConfigError(
                    "strategy.shadow_gate_counterfactual_gates must contain at least one gate."
                )
            unknown_gates = sorted(configured_gates - allowed_gates)
            if unknown_gates:
                raise ConfigError(
                    "strategy.shadow_gate_counterfactual_gates contains unsupported gates: "
                    + ", ".join(unknown_gates)
                )
            if not (
                Decimal("0")
                < self.strategy.shadow_gate_counterfactual_risk_cap_pct
                <= self.risk.risk_per_trade_pct
            ):
                raise ConfigError(
                    "strategy.shadow_gate_counterfactual_risk_cap_pct must be positive and no higher than risk_per_trade_pct."
                )
        if (
            self.mode in {TradingMode.PAPER_TRADING, TradingMode.BACKTEST}
            and self.risk.risk_per_trade_pct > Decimal("0.02")
            and not allow_unproven_high_risk
        ):
            raise ConfigError(
                "risk_per_trade_pct above 2% is blocked until sufficient post-cost evidence exists; "
                "set BOT_ALLOW_UNPROVEN_HIGH_RISK=1 only for an explicit reviewed experiment."
            )
        if not (Decimal("0") < self.risk.realtime_correlation_threshold <= Decimal("1")):
            raise ConfigError("risk.realtime_correlation_threshold must be within (0, 1].")
        if self.risk.realtime_correlation_lookback < 10:
            raise ConfigError("risk.realtime_correlation_lookback must be >= 10.")
        tp_fraction = sum(target.fraction for target in self.trade_management.partial_take_profits)
        if tp_fraction != Decimal("1"):
            raise ConfigError("trade_management.partial_take_profits fractions must sum to 1.0.")
        self._validate_exit_profile("trade_management.partial_take_profits", self.trade_management.partial_take_profits)
        for strategy, targets in self.trade_management.strategy_exit_profiles.items():
            profile_fraction = sum(target.fraction for target in targets)
            if profile_fraction != Decimal("1"):
                raise ConfigError(
                    "trade_management.strategy_exit_profiles fractions must sum to 1.0 "
                    f"for {strategy}."
                )
            self._validate_exit_profile(f"trade_management.strategy_exit_profiles.{strategy}", targets)
        if self.risk.risk_per_trade_pct >= self.risk.aggressive_risk_threshold_pct:
            warnings.append(
                f"risk_per_trade_pct={self.risk.risk_per_trade_pct:.2%} is aggressive for futures."
            )
        if self.account.initial_equity_usdt is None and self.mode in {
            TradingMode.BACKTEST,
            TradingMode.PAPER_TRADING,
        }:
            raise ConfigError(
                "Backtest/paper mode needs STARTING_DEPOSIT_USDT or MANUAL_TENGE_USDT_RATE; "
                "the bot will not guess KZT/USDT."
            )
        if self.account.initial_equity_usdt is None and self.mode == TradingMode.DRY_RUN:
            warnings.append(
                "No USDT-equivalent starting equity is configured; set STARTING_DEPOSIT_USDT "
                "or MANUAL_TENGE_USDT_RATE before running trade cycles."
            )
        if self.trading.position_mode != "ONE_WAY" and not self.safety.allow_hedge_mode:
            raise ConfigError("Hedge mode is disabled by safety.allow_hedge_mode=false.")
        return warnings

    @staticmethod
    def _validate_exit_profile(name: str, targets: list[PartialTakeProfitConfig]) -> None:
        if not targets:
            raise ConfigError(f"{name} must define at least one take-profit target.")
        remaining = Decimal("1")
        has_trailing_with_runner = False
        for target in targets:
            if target.fraction <= 0:
                raise ConfigError(f"{name}.{target.name} fraction must be positive.")
            if target.reward_risk <= 0:
                raise ConfigError(f"{name}.{target.name} reward_risk must be positive.")
            remaining -= target.fraction
            if target.activate_trailing and remaining > Decimal("0"):
                has_trailing_with_runner = True
        if any(target.activate_trailing for target in targets) and not has_trailing_with_runner:
            raise ConfigError(
                f"{name} activates trailing only after the final target; leave a runner after the trailing trigger."
            )

    def _validate_production_unlock(self) -> None:
        import json

        path = Path(self.safety.production_unlock_file)
        if not path.exists():
            raise ConfigError(f"MAINNET_LIVE blocked: missing production unlock file {path}.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"MAINNET_LIVE blocked: invalid production unlock JSON: {exc}.") from exc

        if payload.get("confirmation") != self.safety.required_mainnet_confirmation:
            raise ConfigError("MAINNET_LIVE blocked: production unlock confirmation mismatch.")
        if self.safety.require_backtest_approval_for_mainnet and not payload.get("backtest_approved"):
            raise ConfigError("MAINNET_LIVE blocked: backtest approval is missing.")
        if self.safety.require_walkforward_report_for_mainnet:
            self._validate_walkforward_report()
        if self.safety.require_paper_approval_for_mainnet and not payload.get("paper_trading_approved"):
            raise ConfigError("MAINNET_LIVE blocked: paper-trading approval is missing.")
        if not payload.get("human_approved_by"):
            raise ConfigError("MAINNET_LIVE blocked: human_approved_by is required in production unlock.")

    def _validate_walkforward_report(self) -> None:
        """Require walk-forward evidence for the strategies that will actually trade.

        The previous gate accepted a bare ``backtest_approved: true`` flag, while
        the only automated backtest in the CLI was a naive EMA smoke test that
        never touches SQUEEZE_BREAKOUT or MEAN_REVERSION. A safety gate that can
        be satisfied without evidence about the deployed strategies is decoration,
        not safety.
        """
        import json
        from datetime import datetime, timedelta, timezone

        path = Path(self.safety.walkforward_report_path)
        if not path.exists():
            raise ConfigError(
                f"MAINNET_LIVE blocked: missing walk-forward report {path}. "
                "Generate it with the approved non-overlapping production-pipeline walk-forward workflow first."
            )
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"MAINNET_LIVE blocked: invalid walk-forward report JSON: {exc}.") from exc

        generated_at = report.get("generated_at")
        if not generated_at:
            raise ConfigError("MAINNET_LIVE blocked: walk-forward report has no generated_at timestamp.")
        try:
            stamp = datetime.fromisoformat(str(generated_at))
        except ValueError as exc:
            raise ConfigError(f"MAINNET_LIVE blocked: unparsable walk-forward generated_at: {exc}.") from exc
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - stamp
        if age > timedelta(days=self.safety.walkforward_max_report_age_days):
            raise ConfigError(
                f"MAINNET_LIVE blocked: walk-forward report is {age.days} days old "
                f"(limit {self.safety.walkforward_max_report_age_days})."
            )

        covered = {str(name).upper() for name in report.get("strategies") or []}
        # Only strategies that can actually open a mainnet position must be proven.
        required = {
            str(name).upper()
            for name in self.strategy.enabled_strategies
            if str(name).upper() in {str(item).upper() for item in self.safety.mainnet_allowed_strategies}
        }
        missing = required - covered
        if missing:
            raise ConfigError(
                "MAINNET_LIVE blocked: walk-forward report does not cover live strategies: "
                + ", ".join(sorted(missing))
            )

        oos = report.get("out_of_sample") or {}
        trades = int(oos.get("trades") or 0)
        if trades < self.safety.min_walkforward_out_of_sample_trades:
            raise ConfigError(
                f"MAINNET_LIVE blocked: only {trades} out-of-sample walk-forward trades "
                f"(need {self.safety.min_walkforward_out_of_sample_trades})."
            )
        profit_factor = to_decimal(oos.get("profit_factor") or "0")
        if profit_factor < self.safety.min_walkforward_profit_factor:
            raise ConfigError(
                f"MAINNET_LIVE blocked: out-of-sample profit factor {profit_factor} "
                f"< {self.safety.min_walkforward_profit_factor}."
            )
        expectancy = to_decimal(oos.get("expectancy_r") or "0")
        if expectancy < self.safety.min_walkforward_expectancy_r:
            raise ConfigError(
                f"MAINNET_LIVE blocked: out-of-sample expectancy {expectancy}R "
                f"< {self.safety.min_walkforward_expectancy_r}R."
            )
        # An edge that is not statistically separable from zero is not an edge.
        ci_low = _decimal_or_none(oos.get("expectancy_r_ci_low"))
        if ci_low is None:
            raise ConfigError(
                "MAINNET_LIVE blocked: walk-forward report must include expectancy_r_ci_low "
                "(lower bound of the 95% confidence interval)."
            )
        if ci_low <= 0:
            raise ConfigError(
                f"MAINNET_LIVE blocked: 95% CI lower bound for expectancy is {ci_low}R. "
                "The measured edge is not distinguishable from zero."
            )

    def _validate_ml_live_readiness(self) -> None:
        import json

        path = Path(self.ml.validation_report_path)
        if not path.exists():
            raise ConfigError(f"MAINNET_LIVE blocked: missing ML validation report {path}.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"MAINNET_LIVE blocked: invalid ML validation report JSON: {exc}.") from exc
        if not payload.get("validated"):
            raise ConfigError("MAINNET_LIVE blocked: ML validation report is not validated.")
        baseline = payload.get("baseline") or {}
        filtered = payload.get("ml_filtered") or {}
        baseline_trades = int(baseline.get("trades") or 0)
        filtered_trades = int(filtered.get("trades") or 0)
        if baseline_trades <= 0 or filtered_trades <= 0:
            raise ConfigError("MAINNET_LIVE blocked: ML validation report has no comparable trades.")
        coverage = Decimal(filtered_trades) / Decimal(baseline_trades)
        if coverage < self.ml.min_validation_trade_coverage:
            raise ConfigError("MAINNET_LIVE blocked: ML validation trade coverage is too low.")
        if to_decimal(filtered.get("total_r", "0")) < to_decimal(baseline.get("total_r", "0")):
            raise ConfigError("MAINNET_LIVE blocked: ML-filtered total R is worse than baseline.")
        if to_decimal(filtered.get("avg_r", "0")) < to_decimal(baseline.get("avg_r", "0")):
            raise ConfigError("MAINNET_LIVE blocked: ML-filtered average R is worse than baseline.")


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def active_systemd_services(service_names: list[str]) -> list[str]:
    """Return active systemd services from a candidate list.

    This gate is intentionally best-effort outside Linux/systemd so local config
    tooling still works, but it is strict on the VPS where systemctl exists.
    """
    if os.getenv("BOT_SKIP_EXCLUSIVE_LIVE_CHECK") == "1":
        return []
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return []
    active: list[str] = []
    for service in service_names:
        name = str(service).strip()
        if not name:
            continue
        try:
            result = subprocess.run(
                [systemctl, "is-active", name],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.stdout.strip() == "active":
            active.append(name)
    return active


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return to_decimal(value)


def _dec_map(data: dict[str, Any]) -> dict[str, Decimal]:
    return {str(key): to_decimal(value) for key, value in data.items()}


def _safety_config(raw: dict[str, Any]) -> SafetyConfig:
    data = dict(raw)
    for key in (
        "max_mainnet_risk_per_trade_pct",
        "min_walkforward_profit_factor",
        "min_walkforward_expectancy_r",
    ):
        if key in data:
            data[key] = to_decimal(data[key])
    return SafetyConfig(**data)


def load_config(config_path: str | Path = "config.yaml", env_path: str | Path = ".env") -> AppConfig:
    if yaml is None:
        raise ConfigError("PyYAML is required to load config.yaml. Install project dependencies first.")
    _load_env_file(Path(env_path))
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))

    secrets = Secrets(
        binance_api_key=os.getenv("BINANCE_API_KEY") or None,
        binance_api_secret=os.getenv("BINANCE_API_SECRET") or None,
        binance_testnet_api_key=os.getenv("BINANCE_TESTNET_API_KEY") or None,
        binance_testnet_api_secret=os.getenv("BINANCE_TESTNET_API_SECRET") or None,
        coingecko_api_key=os.getenv("COINGECKO_API_KEY") or None,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
    )

    account_raw = raw["account"]
    starting_deposit_usdt = os.getenv("STARTING_DEPOSIT_USDT") or account_raw.get("starting_deposit_usdt")
    manual_tenge_usdt_rate = os.getenv("MANUAL_TENGE_USDT_RATE") or account_raw.get("manual_tenge_usdt_rate")

    return AppConfig(
        mode=TradingMode(raw["mode"]),
        exchange=ExchangeConfig(**raw["exchange"]),
        safety=_safety_config(raw["safety"]),
        account=AccountConfig(
            starting_deposit_tenge=to_decimal(account_raw["starting_deposit_tenge"]),
            starting_deposit_usdt=_decimal_or_none(starting_deposit_usdt),
            manual_tenge_usdt_rate=_decimal_or_none(manual_tenge_usdt_rate),
            quote_asset=account_raw.get("quote_asset", "USDT"),
        ),
        trading=TradingConfig(
            timeframes=list(raw["trading"]["timeframes"]),
            poll_interval_sec=int(raw["trading"]["poll_interval_sec"]),
            max_symbols=int(raw["trading"]["max_symbols"]),
            margin_type=str(raw["trading"]["margin_type"]),
            position_mode=str(raw["trading"]["position_mode"]),
            order_working_type=str(raw["trading"]["order_working_type"]),
            min_backtest_trades=int(raw["trading"]["min_backtest_trades"]),
            min_backtest_profit_factor=to_decimal(raw["trading"]["min_backtest_profit_factor"]),
            min_backtest_max_drawdown_pct=to_decimal(raw["trading"]["min_backtest_max_drawdown_pct"]),
        ),
        universe=UniverseConfig(
            market_cap_top_n=int(raw["universe"]["market_cap_top_n"]),
            fetch_extra_top_n=int(raw["universe"]["fetch_extra_top_n"]),
            quote_asset=str(raw["universe"]["quote_asset"]),
            excluded_assets={asset.upper() for asset in raw["universe"]["excluded_assets"]},
            min_24h_quote_volume_usdt=to_decimal(raw["universe"]["min_24h_quote_volume_usdt"]),
            max_spread_bps=to_decimal(raw["universe"]["max_spread_bps"]),
            min_order_book_top_liquidity_usdt=to_decimal(raw["universe"]["min_order_book_top_liquidity_usdt"]),
            cache_ttl_sec=int(raw["universe"]["cache_ttl_sec"]),
            min_symbol_quality_score=to_decimal(raw["universe"].get("min_symbol_quality_score", "70")),
            quality_spread_weight=to_decimal(raw["universe"].get("quality_spread_weight", "0.35")),
            quality_liquidity_weight=to_decimal(raw["universe"].get("quality_liquidity_weight", "0.40")),
            quality_volume_weight=to_decimal(raw["universe"].get("quality_volume_weight", "0.25")),
        ),
        strategy=StrategyConfig(
            ema_fast=int(raw["strategy"]["ema_fast"]),
            ema_mid=int(raw["strategy"]["ema_mid"]),
            ema_slow=int(raw["strategy"]["ema_slow"]),
            rsi_period=int(raw["strategy"]["rsi_period"]),
            atr_period=int(raw["strategy"]["atr_period"]),
            volume_lookback=int(raw["strategy"]["volume_lookback"]),
            min_volume_ratio=to_decimal(raw["strategy"]["min_volume_ratio"]),
            min_atr_pct=to_decimal(raw["strategy"]["min_atr_pct"]),
            max_atr_pct=to_decimal(raw["strategy"]["max_atr_pct"]),
            stop_atr_multiplier=_dec_map(raw["strategy"]["stop_atr_multiplier"]),
            take_profit_rr=_dec_map(raw["strategy"]["take_profit_rr"]),
            use_funding_filter=bool(raw["strategy"]["use_funding_filter"]),
            max_abs_funding_rate=to_decimal(raw["strategy"]["max_abs_funding_rate"]),
            enabled_strategies=list(raw["strategy"].get("enabled_strategies", ["TREND_FOLLOWING"])),
            strategy_modes={
                _strategy_name(name): str(mode).strip().lower()
                for name, mode in raw["strategy"].get("strategy_modes", {}).items()
            },
            shadow_order_flow_hard_gate=bool(raw["strategy"].get("shadow_order_flow_hard_gate", False)),
            order_flow_entry_gate_mode=str(
                raw["strategy"].get("order_flow_entry_gate_mode", "strict")
            ).strip().lower(),
            order_flow_hostile_score_floor=to_decimal(
                raw["strategy"].get("order_flow_hostile_score_floor", "0.70")
            ),
            order_flow_mixed_score_floor=to_decimal(
                raw["strategy"].get("order_flow_mixed_score_floor", "0.45")
            ),
            order_flow_always_hard_flags=[
                str(flag) for flag in raw["strategy"].get("order_flow_always_hard_flags", ["liquidation_cascade"])
            ],
            squeeze_order_flow_measurement_enabled=bool(
                raw["strategy"].get("squeeze_order_flow_measurement_enabled", False)
            ),
            squeeze_order_flow_measurement_min_score=to_decimal(
                raw["strategy"].get("squeeze_order_flow_measurement_min_score", "0.30")
            ),
            squeeze_order_flow_measurement_risk_cap_pct=to_decimal(
                raw["strategy"].get("squeeze_order_flow_measurement_risk_cap_pct", "0.005")
            ),
            squeeze_gate_cohort_shadow_enabled=bool(
                raw["strategy"].get("squeeze_gate_cohort_shadow_enabled", False)
            ),
            squeeze_gate_cohort_shadow_risk_cap_pct=to_decimal(
                raw["strategy"].get("squeeze_gate_cohort_shadow_risk_cap_pct", "0.005")
            ),
            squeeze_controlled_paper_enabled=bool(
                raw["strategy"].get("squeeze_controlled_paper_enabled", False)
            ),
            squeeze_controlled_paper_min_order_flow_score=to_decimal(
                raw["strategy"].get("squeeze_controlled_paper_min_order_flow_score", "0.65")
            ),
            squeeze_controlled_paper_risk_cap_pct=to_decimal(
                raw["strategy"].get("squeeze_controlled_paper_risk_cap_pct", "0.005")
            ),
            squeeze_dynamic_neutral_shadow_enabled=bool(
                raw["strategy"].get("squeeze_dynamic_neutral_shadow_enabled", False)
            ),
            squeeze_dynamic_neutral_shadow_min_order_flow_score=to_decimal(
                raw["strategy"].get("squeeze_dynamic_neutral_shadow_min_order_flow_score", "0.65")
            ),
            squeeze_dynamic_neutral_shadow_risk_cap_pct=to_decimal(
                raw["strategy"].get("squeeze_dynamic_neutral_shadow_risk_cap_pct", "0.0025")
            ),
            shadow_revalidation_enabled=bool(raw["strategy"].get("shadow_revalidation_enabled", False)),
            shadow_revalidation_cohort=str(raw["strategy"].get("shadow_revalidation_cohort", "")).strip(),
            shadow_revalidation_risk_cap_pct=to_decimal(
                raw["strategy"].get("shadow_revalidation_risk_cap_pct", "0.0025")
            ),
            shadow_revalidation_strategies=[
                _strategy_name(name)
                for name in raw["strategy"].get("shadow_revalidation_strategies", [])
                if _strategy_name(name)
            ],
            shadow_gate_counterfactual_enabled=bool(
                raw["strategy"].get("shadow_gate_counterfactual_enabled", False)
            ),
            shadow_gate_counterfactual_cohort=str(
                raw["strategy"].get("shadow_gate_counterfactual_cohort", "")
            ).strip(),
            shadow_gate_counterfactual_risk_cap_pct=to_decimal(
                raw["strategy"].get("shadow_gate_counterfactual_risk_cap_pct", "0.0025")
            ),
            shadow_gate_counterfactual_gates=[
                str(gate).strip().upper()
                for gate in raw["strategy"].get("shadow_gate_counterfactual_gates", [])
                if str(gate).strip()
            ],
            mean_reversion_deviation_atr=to_decimal(raw["strategy"].get("mean_reversion_deviation_atr", "2.0")),
            mean_reversion_rsi_oversold=to_decimal(raw["strategy"].get("mean_reversion_rsi_oversold", "28")),
            mean_reversion_rsi_overbought=to_decimal(raw["strategy"].get("mean_reversion_rsi_overbought", "72")),
            mean_reversion_stop_atr_multiplier=to_decimal(
                raw["strategy"].get("mean_reversion_stop_atr_multiplier", "1.0")
            ),
            mean_reversion_take_profit_rr=to_decimal(raw["strategy"].get("mean_reversion_take_profit_rr", "1.4")),
            mean_reversion_min_volume_ratio=to_decimal(
                raw["strategy"].get("mean_reversion_min_volume_ratio", "1.05")
            ),
            mean_reversion_min_edge_score=to_decimal(raw["strategy"].get("mean_reversion_min_edge_score", "0.40")),
            mean_reversion_min_confluence=int(raw["strategy"].get("mean_reversion_min_confluence", 4)),
            mean_reversion_require_divergence=bool(
                raw["strategy"].get("mean_reversion_require_divergence", True)
            ),
            mean_reversion_require_edge_confirmation=bool(
                raw["strategy"].get("mean_reversion_require_edge_confirmation", True)
            ),
            mean_reversion_btc_direction_gate_enabled=bool(
                raw["strategy"].get("mean_reversion_btc_direction_gate_enabled", True)
            ),
            mean_reversion_btc_direction_gate_pct=to_decimal(
                raw["strategy"].get("mean_reversion_btc_direction_gate_pct", "0.012")
            ),
            mean_reversion_order_flow_gate_enabled=bool(
                raw["strategy"].get("mean_reversion_order_flow_gate_enabled", True)
            ),
            mean_reversion_min_order_flow_score=to_decimal(
                raw["strategy"].get("mean_reversion_min_order_flow_score", "0.25")
            ),
            mean_reversion_min_net_reward_risk=to_decimal(
                raw["strategy"].get("mean_reversion_min_net_reward_risk", "1.15")
            ),
            mean_reversion_min_expected_net_r=to_decimal(
                raw["strategy"].get("mean_reversion_min_expected_net_r", "0.05")
            ),
            mean_reversion_expected_winrate_floor=to_decimal(
                raw["strategy"].get("mean_reversion_expected_winrate_floor", "0.48")
            ),
            trend_pullback_min_volume_ratio=to_decimal(
                raw["strategy"].get("trend_pullback_min_volume_ratio", "1.10")
            ),
            trend_pullback_min_trend_strength=to_decimal(
                raw["strategy"].get("trend_pullback_min_trend_strength", "0.35")
            ),
            trend_pullback_min_depth_atr=to_decimal(raw["strategy"].get("trend_pullback_min_depth_atr", "0.25")),
            trend_pullback_max_depth_atr=to_decimal(raw["strategy"].get("trend_pullback_max_depth_atr", "2.20")),
            trend_pullback_stop_atr_multiplier=to_decimal(
                raw["strategy"].get("trend_pullback_stop_atr_multiplier", "1.20")
            ),
            trend_pullback_take_profit_rr=to_decimal(raw["strategy"].get("trend_pullback_take_profit_rr", "1.60")),
            trend_pullback_min_confluence=int(raw["strategy"].get("trend_pullback_min_confluence", 4)),
            trend_pullback_min_edge_score=to_decimal(raw["strategy"].get("trend_pullback_min_edge_score", "0.25")),
            liquidity_sweep_lookback=int(raw["strategy"].get("liquidity_sweep_lookback", 18)),
            liquidity_sweep_stop_atr_multiplier=to_decimal(
                raw["strategy"].get("liquidity_sweep_stop_atr_multiplier", "1.10")
            ),
            liquidity_sweep_take_profit_rr=to_decimal(
                raw["strategy"].get("liquidity_sweep_take_profit_rr", "1.20")
            ),
            liquidity_sweep_min_edge_score=to_decimal(raw["strategy"].get("liquidity_sweep_min_edge_score", "0.30")),
            liquidity_sweep_min_reclaim_atr=to_decimal(
                raw["strategy"].get("liquidity_sweep_min_reclaim_atr", "0.70")
            ),
            liquidity_sweep_follow_through_min_body_atr=to_decimal(
                raw["strategy"].get("liquidity_sweep_follow_through_min_body_atr", "0.18")
            ),
            vwap_reversion_lookback=int(raw["strategy"].get("vwap_reversion_lookback", 96)),
            vwap_reversion_deviation_atr=to_decimal(raw["strategy"].get("vwap_reversion_deviation_atr", "1.60")),
            vwap_reversion_max_deviation_atr=to_decimal(
                raw["strategy"].get("vwap_reversion_max_deviation_atr", "6.00")
            ),
            vwap_reversion_min_volume_ratio=to_decimal(
                raw["strategy"].get("vwap_reversion_min_volume_ratio", "1.20")
            ),
            vwap_reversion_watch_deviation_atr=to_decimal(
                raw["strategy"].get("vwap_reversion_watch_deviation_atr", "1.80")
            ),
            vwap_reversion_watch_max_deviation_atr=to_decimal(
                raw["strategy"].get("vwap_reversion_watch_max_deviation_atr", "8.00")
            ),
            vwap_reversion_watch_min_volume_ratio=to_decimal(
                raw["strategy"].get("vwap_reversion_watch_min_volume_ratio", "1.05")
            ),
            vwap_reversion_min_progress_atr=to_decimal(
                raw["strategy"].get("vwap_reversion_min_progress_atr", "0.50")
            ),
            vwap_reversion_watch_min_progress_atr=to_decimal(
                raw["strategy"].get("vwap_reversion_watch_min_progress_atr", "0.75")
            ),
            vwap_reversion_reversal_min_body_atr=to_decimal(
                raw["strategy"].get("vwap_reversion_reversal_min_body_atr", "0.12")
            ),
            vwap_reversion_stop_atr_multiplier=to_decimal(
                raw["strategy"].get("vwap_reversion_stop_atr_multiplier", "1.00")
            ),
            vwap_reversion_take_profit_rr=to_decimal(raw["strategy"].get("vwap_reversion_take_profit_rr", "1.15")),
            momentum_continuation_min_volume_ratio=to_decimal(
                raw["strategy"].get("momentum_continuation_min_volume_ratio", "1.40")
            ),
            momentum_continuation_stop_atr_multiplier=to_decimal(
                raw["strategy"].get("momentum_continuation_stop_atr_multiplier", "1.30")
            ),
            momentum_continuation_take_profit_rr=to_decimal(
                raw["strategy"].get("momentum_continuation_take_profit_rr", "1.60")
            ),
            momentum_continuation_min_edge_score=to_decimal(
                raw["strategy"].get("momentum_continuation_min_edge_score", "0.20")
            ),
            range_grid_lookback=int(raw["strategy"].get("range_grid_lookback", 48)),
            range_grid_stop_atr_multiplier=to_decimal(raw["strategy"].get("range_grid_stop_atr_multiplier", "1.00")),
            range_grid_take_profit_rr=to_decimal(raw["strategy"].get("range_grid_take_profit_rr", "0.80")),
            range_grid_entry_zone_pct=to_decimal(raw["strategy"].get("range_grid_entry_zone_pct", "0.15")),
            range_grid_rsi_long_max=to_decimal(raw["strategy"].get("range_grid_rsi_long_max", "40")),
            range_grid_rsi_short_min=to_decimal(raw["strategy"].get("range_grid_rsi_short_min", "60")),
            funding_carry_filter_enabled=bool(raw["strategy"].get("funding_carry_filter_enabled", True)),
            funding_carry_penalty_threshold=to_decimal(
                raw["strategy"].get("funding_carry_penalty_threshold", "0.0005")
            ),
            funding_carry_block_threshold=to_decimal(
                raw["strategy"].get("funding_carry_block_threshold", "0.0012")
            ),
            squeeze_release_lookback_bars=int(raw["strategy"].get("squeeze_release_lookback_bars", 4)),
            squeeze_release_min_breakout_atr=to_decimal(
                raw["strategy"].get("squeeze_release_min_breakout_atr", "0.03")
            ),
            squeeze_early_min_breakout_atr=to_decimal(
                raw["strategy"].get("squeeze_early_min_breakout_atr", "0.10")
            ),
            squeeze_early_min_bars_extra=int(raw["strategy"].get("squeeze_early_min_bars_extra", 4)),
            squeeze_early_min_volume_ratio=to_decimal(
                raw["strategy"].get("squeeze_early_min_volume_ratio", "1.50")
            ),
            squeeze_max_extension_atr=to_decimal(raw["strategy"].get("squeeze_max_extension_atr", "2.40")),
            squeeze_retest_enabled=bool(raw["strategy"].get("squeeze_retest_enabled", True)),
            squeeze_retest_required_after_release_offset=int(
                raw["strategy"].get("squeeze_retest_required_after_release_offset", 2)
            ),
            squeeze_retest_lookback_bars=int(raw["strategy"].get("squeeze_retest_lookback_bars", 3)),
            squeeze_retest_tolerance_atr=to_decimal(raw["strategy"].get("squeeze_retest_tolerance_atr", "0.35")),
            squeeze_retest_min_rejection_body_atr=to_decimal(
                raw["strategy"].get("squeeze_retest_min_rejection_body_atr", "0.05")
            ),
        ),
        market_filters=MarketFilterConfig(
            btc_4h_drop_filter_enabled=bool(raw.get("market_filters", {}).get("btc_4h_drop_filter_enabled", True)),
            btc_4h_max_drop_pct=to_decimal(raw.get("market_filters", {}).get("btc_4h_max_drop_pct", "-0.03")),
            block_longs_when_btc_weak=bool(raw.get("market_filters", {}).get("block_longs_when_btc_weak", True)),
            block_all_when_btc_weak=bool(raw.get("market_filters", {}).get("block_all_when_btc_weak", False)),
            utc_session_filter_enabled=bool(raw.get("market_filters", {}).get("utc_session_filter_enabled", True)),
            avoid_utc_hours={int(hour) for hour in raw.get("market_filters", {}).get("avoid_utc_hours", [])},
            use_self_learning_filters=bool(raw.get("market_filters", {}).get("use_self_learning_filters", True)),
            use_open_interest_filter=bool(raw.get("market_filters", {}).get("use_open_interest_filter", False)),
            oi_drop_cascade_pct=to_decimal(raw.get("market_filters", {}).get("oi_drop_cascade_pct", "-5.0")),
            oi_rise_longs_blocked_pct=to_decimal(raw.get("market_filters", {}).get("oi_rise_longs_blocked_pct", "3.0")),
            high_confidence_squeeze_session_override=bool(
                raw.get("market_filters", {}).get("high_confidence_squeeze_session_override", False)
            ),
            high_confidence_squeeze_min_confidence=to_decimal(
                raw.get("market_filters", {}).get("high_confidence_squeeze_min_confidence", "0.80")
            ),
            high_confidence_squeeze_allowed_hours={
                int(hour)
                for hour in raw.get("market_filters", {}).get(
                    "high_confidence_squeeze_allowed_hours",
                    [0, 1],
                )
            },
            counter_trend_filter_enabled=bool(
                raw.get("market_filters", {}).get("counter_trend_filter_enabled", True)
            ),
            btc_4h_min_rise_pct_for_short_block=to_decimal(
                raw.get("market_filters", {}).get("btc_4h_min_rise_pct_for_short_block", "0.015")
            ),
            block_shorts_when_btc_strong=bool(
                raw.get("market_filters", {}).get("block_shorts_when_btc_strong", True)
            ),
            symbol_4h_trend_filter_enabled=bool(
                raw.get("market_filters", {}).get("symbol_4h_trend_filter_enabled", True)
            ),
            symbol_4h_trend_lookback_bars=int(
                raw.get("market_filters", {}).get("symbol_4h_trend_lookback_bars", 12)
            ),
            symbol_4h_max_rise_pct_for_short=to_decimal(
                raw.get("market_filters", {}).get("symbol_4h_max_rise_pct_for_short", "0.03")
            ),
            symbol_4h_max_drop_pct_for_long=to_decimal(
                raw.get("market_filters", {}).get("symbol_4h_max_drop_pct_for_long", "-0.03")
            ),
        ),
        risk=RiskConfig(
            risk_per_trade_pct=to_decimal(raw["risk"]["risk_per_trade_pct"]),
            aggressive_risk_threshold_pct=to_decimal(raw["risk"]["aggressive_risk_threshold_pct"]),
            max_concurrent_positions=int(raw["risk"].get("max_concurrent_positions", 1)),
            max_leverage=int(raw["risk"]["max_leverage"]),
            default_leverage=int(raw["risk"]["default_leverage"]),
            max_daily_loss_pct=to_decimal(raw["risk"]["max_daily_loss_pct"]),
            cooldown_after_losses=int(raw["risk"]["cooldown_after_losses"]),
            cooldown_minutes=int(raw["risk"]["cooldown_minutes"]),
            symbol_cooldown_after_loss_minutes=int(raw["risk"].get("symbol_cooldown_after_loss_minutes", 120)),
            strategy_reentry_cooldown_minutes=int(raw["risk"].get("strategy_reentry_cooldown_minutes", 45)),
            strategy_reentry_winning_cooldown_minutes=int(
                raw["risk"].get("strategy_reentry_winning_cooldown_minutes", 90)
            ),
            scale_in_enabled=bool(raw["risk"].get("scale_in_enabled", False)),
            max_scale_ins_per_symbol_strategy=int(raw["risk"].get("max_scale_ins_per_symbol_strategy", 2)),
            scale_in_risk_multiplier=to_decimal(raw["risk"].get("scale_in_risk_multiplier", "0.50")),
            scale_in_min_unrealized_r=to_decimal(raw["risk"].get("scale_in_min_unrealized_r", "0.25")),
            scale_in_independent_signal_minutes=int(raw["risk"].get("scale_in_independent_signal_minutes", 60)),
            trade_cluster_window_minutes=int(raw["risk"].get("trade_cluster_window_minutes", 60)),
            max_portfolio_risk_pct=to_decimal(raw["risk"]["max_portfolio_risk_pct"]),
            max_correlated_group_risk_pct=to_decimal(raw["risk"]["max_correlated_group_risk_pct"]),
            max_margin_usage_pct=to_decimal(raw["risk"].get("max_margin_usage_pct", "1")),
            min_reward_risk=to_decimal(raw["risk"]["min_reward_risk"]),
            taker_fee_bps=to_decimal(raw["risk"]["taker_fee_bps"]),
            slippage_bps=to_decimal(raw["risk"]["slippage_bps"]),
            funding_buffer_bps=to_decimal(raw["risk"]["funding_buffer_bps"]),
            max_funding_impact_bps=to_decimal(raw["risk"].get("max_funding_impact_bps", "8.0")),
            funding_impact_holding_hours=to_decimal(raw["risk"].get("funding_impact_holding_hours", "8")),
            liquidation_buffer_pct=to_decimal(raw["risk"]["liquidation_buffer_pct"]),
            require_liquidation_check_in_live=bool(raw["risk"]["require_liquidation_check_in_live"]),
            adaptive_kelly_enabled=bool(raw["risk"].get("adaptive_kelly_enabled", True)),
            kelly_lookback_trades=int(raw["risk"].get("kelly_lookback_trades", 50)),
            kelly_min_sample_trades=int(raw["risk"].get("kelly_min_sample_trades", 50)),
            kelly_fraction=to_decimal(raw["risk"].get("kelly_fraction", "0.5")),
            kelly_min_risk_pct=to_decimal(raw["risk"].get("kelly_min_risk_pct", "0.005")),
            kelly_max_risk_pct=to_decimal(raw["risk"].get("kelly_max_risk_pct", "0.03")),
            dynamic_sizing_enabled=bool(raw["risk"].get("dynamic_sizing_enabled", True)),
            dynamic_sizing_min_risk_pct=to_decimal(raw["risk"].get("dynamic_sizing_min_risk_pct", "0.003")),
            dynamic_sizing_max_risk_pct=to_decimal(raw["risk"].get("dynamic_sizing_max_risk_pct", "0.025")),
            dynamic_sizing_shadow_max_risk_pct=to_decimal(
                raw["risk"].get("dynamic_sizing_shadow_max_risk_pct", "0.020")
            ),
            dynamic_sizing_high_confidence=to_decimal(
                raw["risk"].get("dynamic_sizing_high_confidence", "0.82")
            ),
            dynamic_sizing_elite_confidence=to_decimal(
                raw["risk"].get("dynamic_sizing_elite_confidence", "0.90")
            ),
            dynamic_leverage_enabled=bool(raw["risk"].get("dynamic_leverage_enabled", True)),
            dynamic_leverage_min=int(raw["risk"].get("dynamic_leverage_min", 1)),
            dynamic_leverage_max=int(raw["risk"].get("dynamic_leverage_max", raw["risk"]["max_leverage"])),
            dynamic_strategy_risk_multipliers=_dec_map(
                raw["risk"].get("dynamic_strategy_risk_multipliers", {})
            )
            or RiskConfig.__dataclass_fields__["dynamic_strategy_risk_multipliers"].default_factory(),
            dynamic_strategy_max_risk_pct=_dec_map(raw["risk"].get("dynamic_strategy_max_risk_pct", {}))
            or RiskConfig.__dataclass_fields__["dynamic_strategy_max_risk_pct"].default_factory(),
            realtime_correlation_enabled=bool(raw["risk"].get("realtime_correlation_enabled", True)),
            realtime_correlation_threshold=to_decimal(raw["risk"].get("realtime_correlation_threshold", "0.70")),
            realtime_correlation_lookback=int(raw["risk"].get("realtime_correlation_lookback", 48)),
            block_live_when_correlation_unavailable=bool(
                raw["risk"].get("block_live_when_correlation_unavailable", True)
            ),
            correlation_groups={k: list(v) for k, v in raw["risk"].get("correlation_groups", {}).items()},
        ),
        trade_management=TradeManagementConfig(
            user_stream_required_for_live=bool(raw["trade_management"].get("user_stream_required_for_live", True)),
            user_stream_stale_after_sec=int(raw["trade_management"].get("user_stream_stale_after_sec", 20)),
            user_stream_reconnect_backoff_sec=int(raw["trade_management"].get("user_stream_reconnect_backoff_sec", 5)),
            rest_reconciliation_when_stale=bool(raw["trade_management"].get("rest_reconciliation_when_stale", True)),
            partial_take_profits=[
                PartialTakeProfitConfig(
                    name=str(item["name"]),
                    reward_risk=to_decimal(item["reward_risk"]),
                    fraction=to_decimal(item["fraction"]),
                    move_stop_to_breakeven=bool(item.get("move_stop_to_breakeven", False)),
                    activate_trailing=bool(item.get("activate_trailing", False)),
                )
                for item in raw["trade_management"]["partial_take_profits"]
            ],
            breakeven_offset_bps=to_decimal(raw["trade_management"]["breakeven_offset_bps"]),
            trailing_enabled=bool(raw["trade_management"]["trailing_enabled"]),
            trailing_activation_reward_risk=to_decimal(raw["trade_management"]["trailing_activation_reward_risk"]),
            trailing_callback_rate_pct=_dec_map(raw["trade_management"]["trailing_callback_rate_pct"]),
            trailing_client_side=bool(raw["trade_management"].get("trailing_client_side", False)),
            min_first_target_net_reward_risk=to_decimal(
                raw["trade_management"].get("min_first_target_net_reward_risk", "0.20")
            ),
            strategy_min_first_target_net_reward_risk={
                _strategy_name(strategy): value
                for strategy, value in _dec_map(
                    raw["trade_management"].get("strategy_min_first_target_net_reward_risk", {})
                ).items()
            },
            strategy_exit_profiles={
                _strategy_name(strategy): [
                    PartialTakeProfitConfig(
                        name=str(item["name"]),
                        reward_risk=to_decimal(item["reward_risk"]),
                        fraction=to_decimal(item["fraction"]),
                        move_stop_to_breakeven=bool(item.get("move_stop_to_breakeven", False)),
                        activate_trailing=bool(item.get("activate_trailing", False)),
                    )
                    for item in items
                ]
                for strategy, items in raw["trade_management"].get("strategy_exit_profiles", {}).items()
            },
        ),
        edge_filters=EdgeFilterConfig(
            enabled=bool(raw["edge_filters"]["enabled"]),
            order_book_imbalance_min=to_decimal(raw["edge_filters"]["order_book_imbalance_min"]),
            taker_buy_ratio_long_min=to_decimal(raw["edge_filters"]["taker_buy_ratio_long_min"]),
            taker_buy_ratio_short_max=to_decimal(raw["edge_filters"]["taker_buy_ratio_short_max"]),
            open_interest_change_min_pct=to_decimal(raw["edge_filters"]["open_interest_change_min_pct"]),
            liquidity_sweep_lookback=int(raw["edge_filters"].get("liquidity_sweep_lookback", 12)),
            liquidity_sweep_threshold_bps=to_decimal(raw["edge_filters"].get("liquidity_sweep_threshold_bps", "12")),
            absorption_wick_body_ratio=to_decimal(raw["edge_filters"].get("absorption_wick_body_ratio", "1.8")),
            aggressive_flow_delta_min=to_decimal(raw["edge_filters"].get("aggressive_flow_delta_min", "0.08")),
            structure_break_lookback=int(raw["edge_filters"].get("structure_break_lookback", 20)),
            liquidation_zone_distance_bps=to_decimal(raw["edge_filters"].get("liquidation_zone_distance_bps", "35")),
            liquidation_cluster_filter_enabled=bool(raw["edge_filters"].get("liquidation_cluster_filter_enabled", False)),
        ),
        ml=MLConfig(
            enabled=bool(raw["ml"]["enabled"]),
            min_prediction_confidence=to_decimal(raw["ml"]["min_prediction_confidence"]),
            model_path=str(raw["ml"]["model_path"]),
            training_data_path=str(raw["ml"].get("training_data_path", "data/ml/features.jsonl")),
            retrain_min_trades=int(raw["ml"].get("retrain_min_trades", 500)),
            decision_min_trades=int(raw["ml"].get("decision_min_trades", 500)),
            enforce_decisions=bool(raw["ml"].get("enforce_decisions", False)),
            validation_report_path=str(raw["ml"].get("validation_report_path", "data/ml/walk_forward_report.json")),
            require_validation_improvement_for_live=bool(
                raw["ml"].get("require_validation_improvement_for_live", True)
            ),
            min_validation_trade_coverage=to_decimal(raw["ml"].get("min_validation_trade_coverage", "0.50")),
            adaptive_thresholds_enabled=bool(raw["ml"].get("adaptive_thresholds_enabled", True)),
        ),
        analytics=AnalyticsConfig(
            min_trades_for_adaptation=int(raw["analytics"]["min_trades_for_adaptation"]),
            min_expectancy_r=to_decimal(raw["analytics"]["min_expectancy_r"]),
            min_winrate=to_decimal(raw["analytics"]["min_winrate"]),
            disable_symbol_after_bad_trades=int(raw["analytics"]["disable_symbol_after_bad_trades"]),
            segment_min_trades=int(raw["analytics"].get("segment_min_trades", 12)),
            rsi_bucket_size=int(raw["analytics"].get("rsi_bucket_size", 4)),
            atr_bucket_size_pct=to_decimal(raw["analytics"].get("atr_bucket_size_pct", "0.4")),
            bad_segment_expectancy_r=to_decimal(raw["analytics"].get("bad_segment_expectancy_r", "-0.10")),
            stats_epoch=(str(raw["analytics"]["stats_epoch"]) if raw["analytics"].get("stats_epoch") else None),
        ),
        database=DatabaseConfig(**raw["database"]),
        telegram=TelegramConfig(**raw["telegram"]),
        web=WebConfig(**raw["web"]),
        logging=LoggingConfig(**raw["logging"]),
        secrets=secrets,
    )
