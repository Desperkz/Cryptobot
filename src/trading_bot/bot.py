from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from collections.abc import Collection
from typing import Any

from trading_bot.config import AppConfig, StrategyConfig
from trading_bot.data_provider import BinanceUSDMClient, CoinGeckoClient, MarketDataProvider
from trading_bot.database import Database
from trading_bot.execution import ExecutionReconciler
from trading_bot.execution.reconciler import restart_recovery_evidence
from trading_bot.analytics import SQZCohortMilestoneReporter, SelfLearningEngine
from trading_bot.market_regime_detector import MarketRegimeDetector
from trading_bot.market_universe import MarketUniverseBuilder
from trading_bot.market_filters import MarketEntryFilter
from trading_bot.ml import MLSignalFilter
from trading_bot.models import Candle, Direction, Position, Signal, SymbolFilters, TradingMode, to_decimal
from trading_bot.operational import IncidentAlerter, SystemdNotifier, start_watchdog_thread
from trading_bot.order_manager import OrderManager, simulated_local_entry_order
from trading_bot.position_manager import PositionManager
from trading_bot.risk_manager import (
    DynamicSizingDecision,
    KellyRiskSizer,
    CorrelationFilter,
    RiskError,
    RiskManager,
    dynamic_position_sizing,
)
from trading_bot.strategy_engine.edge import EdgeAnalyzer
from trading_bot.strategy_engine.order_flow import OrderFlowAnnotation, OrderFlowAnnotator
from trading_bot.strategy_engine.relative_strength import RelativeStrengthAnnotation, annotate_relative_strength
from trading_bot.strategy_engine.candidate_strategies import (
    LiquiditySweepReversalStrategy,
    MomentumContinuationStrategy,
    RangeGridStrategy,
    VwapReversionStrategy,
)
from trading_bot.strategy_engine.mean_reversion import MeanReversionStrategy
from trading_bot.strategy_engine.multi_timeframe import MultiTimeframeStrategy
from trading_bot.strategy_engine.router import StrategyRouter
from trading_bot.strategy_engine.squeeze_breakout import SqueezeBreakoutStrategy
from trading_bot.strategy_engine.trend_pullback import TrendPullbackStrategy
from trading_bot.style_selector import StyleSelector
from trading_bot.telegram_notifier import TelegramNotifier
from trading_bot.trade_manager.protection import ProtectionManager
from trading_bot.trade_manager.supervisor import TradeSupervisor
from trading_bot.disaster_mode import DisasterDetector, DisasterConfig, DisasterLevel


logger = logging.getLogger(__name__)


def _disaster_config_from_app_config(config: AppConfig) -> DisasterConfig:
    return DisasterConfig(
        api_max_consecutive_failures=3,
        ws_stale_after_sec=30.0,
        max_spread_bps_disaster=50.0,
        max_consecutive_losses=5,
        max_daily_loss_pct=float(config.risk.max_daily_loss_pct),
        recovery_cooldown_sec=300.0,
    )


def _asset_disaster_skip_reason(
    symbol: str,
    metrics: Any,
    price_move_15m_pct: float | None,
    config: DisasterConfig,
) -> str | None:
    if metrics is not None:
        spread_bps = getattr(metrics, "spread_bps", None)
        if spread_bps is not None and float(spread_bps) > config.max_spread_bps_disaster:
            return f"asset spread anomaly: {float(spread_bps):.1f} bps > {config.max_spread_bps_disaster:.1f}"

        liquidity = getattr(metrics, "top_book_liquidity_usdt", None)
        if liquidity is not None and float(liquidity) < config.min_liquidity_usdt:
            return f"asset liquidity anomaly: ${float(liquidity):.0f} < ${config.min_liquidity_usdt:.0f}"

        funding_rate = getattr(metrics, "funding_rate", None)
        if funding_rate is not None and abs(float(funding_rate)) > config.cascade_funding_rate_threshold:
            return (
                f"asset funding cascade risk: {float(funding_rate):+.4f} "
                f"for {symbol}, skipping symbol only"
            )

    if price_move_15m_pct is not None and abs(price_move_15m_pct) > config.cascade_price_move_pct:
        return (
            f"asset liquidation cascade risk: {price_move_15m_pct:+.1f}% in 15m "
            f"for {symbol}, skipping symbol only"
        )

    return None


class TradingBot:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.db = Database(config.database.url)
        self.binance = BinanceUSDMClient(
            base_url=config.rest_base_url,
            api_key=config.api_key,
            api_secret=config.api_secret,
            recv_window_ms=config.exchange.recv_window_ms,
            timeout_sec=config.exchange.request_timeout_sec,
            max_retries=config.exchange.max_retries,
        )
        self.coingecko = CoinGeckoClient(api_key=config.secrets.coingecko_api_key)
        self.market_data = MarketDataProvider(self.binance)
        self.positions = PositionManager(self.binance if config.is_live else None)
        self.risk = RiskManager(config.risk, config.trade_management)
        self.orders = OrderManager(config, self.binance if config.is_live else None, self.positions)
        self.reconciler = ExecutionReconciler(config, self.binance if config.is_live else None)
        self.protection = ProtectionManager(config, self.binance if config.is_live else None)
        self.supervisor = TradeSupervisor(config, self.binance, self.positions, self.protection)
        self.regime = MarketRegimeDetector(config.strategy)
        self.styles = StyleSelector(config.universe.max_spread_bps, config.universe.min_24h_quote_volume_usdt)
        self.edge_analyzer = EdgeAnalyzer(config.edge_filters)
        self.order_flow = OrderFlowAnnotator(config.edge_filters)
        self._squeeze = SqueezeBreakoutStrategy(config.strategy, self.regime)
        self._trend_pullback = TrendPullbackStrategy(config.strategy, self.regime, self.edge_analyzer)
        self._liquidity_sweep_reversal = LiquiditySweepReversalStrategy(config.strategy, self.edge_analyzer)
        self._vwap_reversion = VwapReversionStrategy(config.strategy)
        self._momentum_continuation = MomentumContinuationStrategy(config.strategy, self.regime, self.edge_analyzer)
        self._range_grid = RangeGridStrategy(config.strategy, self.regime)
        self.strategy = StrategyRouter(
            trend=MultiTimeframeStrategy(config.strategy, self.regime, self.styles, config.edge_filters),
            mean_reversion=MeanReversionStrategy(config.strategy, self.regime, self.edge_analyzer),
            squeeze_breakout=self._squeeze,
            trend_pullback=self._trend_pullback,
            liquidity_sweep_reversal=self._liquidity_sweep_reversal,
            vwap_reversion=self._vwap_reversion,
            momentum_continuation=self._momentum_continuation,
            range_grid=self._range_grid,
            enabled_strategies=config.strategy.execution_strategies(config.mode),
            shadow_strategies=config.strategy.shadow_strategies(),
            config=config.strategy,
        )
        self.entry_filter = MarketEntryFilter(config.market_filters)
        self.corr_filter = CorrelationFilter(
            threshold=float(config.risk.realtime_correlation_threshold),
            lookback=config.risk.realtime_correlation_lookback,
        )
        self.self_learning = SelfLearningEngine(config.analytics)
        self.kelly = KellyRiskSizer(config.risk)
        self.ml_filter = MLSignalFilter(
            config.ml.model_path,
            config.ml.min_prediction_confidence,
            enabled=config.ml.enabled,
            training_data_path=config.ml.training_data_path,
            retrain_min_trades=config.ml.retrain_min_trades,
            decision_min_trades=config.ml.decision_min_trades,
            enforce_decisions=config.ml.enforce_decisions,
        )
        self.universe = MarketUniverseBuilder(config.universe, self.binance, self.coingecko, self.market_data)
        self.telegram = TelegramNotifier(
            token=config.secrets.telegram_bot_token,
            chat_id=config.secrets.telegram_chat_id,
            enabled=config.telegram.enabled,
        )
        self.sqz_cohort_reporter = SQZCohortMilestoneReporter(
            self.db,
            self.telegram,
            stats_epoch=config.analytics.stats_epoch,
            enabled=config.telegram.cohort_report_enabled,
            check_interval_sec=config.telegram.cohort_report_interval_sec,
        )
        self.supervisor.set_warning_callback(self.telegram.risk_warning)
        self.supervisor.set_trade_closed_callback(self._mark_trade_closed)
        self.disaster = DisasterDetector(
            config=_disaster_config_from_app_config(config),
            warn_callback=self.telegram.risk_warning,
            emergency_callback=self._handle_emergency,
        )
        self.incidents = IncidentAlerter(cooldown_sec=300)
        self.systemd = SystemdNotifier()
        self._emergency_actions_applied = False
        self._strategy_diagnostic_seen: dict[tuple[str, str, str], float] = {}
        self._watchdog_stop = False

    async def start(self) -> None:
        warnings = self.config.validate()
        await self.db.connect()
        await self._restore_risk_state()
        await self._sync_paper_positions()
        await self._sync_live_positions_from_exchange(required=True)
        await self.telegram.startup(self.config.mode.value)
        if self.config.is_live and self.config.safety.adopt_manual_positions:
            adopted = await self.positions.adopt_remote_positions()
            for position in adopted:
                await self.telegram.risk_warning(
                    f"Adopted external position {position.symbol}; protection reconciliation is required."
                )
        if self.config.is_live and self.config.safety.reconcile_orders_on_start:
            issues = await self.reconciler.reconcile()
            for issue in issues:
                logger.warning("Execution reconciliation issue: %s", issue)
                await self.telegram.risk_warning(f"{issue.symbol}: {issue.message}")
        if self.config.is_live:
            await self.supervisor.start()
            if self.config.trade_management.user_stream_required_for_live:
                healthy = await self.supervisor.wait_until_healthy(timeout_sec=15)
                if not healthy:
                    raise RuntimeError("Live mode requires a healthy Binance user data stream before trading.")
        for warning in warnings:
            logger.warning(warning)
            await self.telegram.risk_warning(warning)

    async def stop(self) -> None:
        await self.supervisor.stop()
        await self.telegram.shutdown()
        await self.db.close()
        await self.coingecko.close()
        await self.binance.close()
        await self.telegram.close()

    async def _restore_risk_state(self) -> None:
        try:
            saved = await self.db.load_risk_state()
        except Exception:
            logger.exception("Could not restore risk state.")
            return
        if not saved:
            await self._rebuild_risk_state_from_trades()
            return
        cooldown = None
        if saved["cooldown_until"]:
            cooldown = datetime.fromisoformat(saved["cooldown_until"])
            if cooldown < datetime.now(timezone.utc):
                cooldown = None
        self.risk.state.losing_streak = int(saved["losing_streak"])
        self.risk.state.cooldown_until = cooldown
        self.risk.state.realized_pnl_today = Decimal(saved["realized_pnl_today"])
        self.risk.state.pnl_date_utc = (
            saved.get("pnl_date_utc")
            or _date_from_iso_like(saved.get("updated_at"))
            or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )

    async def _rebuild_risk_state_from_trades(self) -> None:
        try:
            trades = await self.db.recent_trades(10_000)
        except Exception:
            logger.exception("Could not rebuild risk state from trade history.")
            return
        closed = [trade for trade in trades if trade.get("status") == "CLOSED"]
        if not closed:
            return

        realized_today = Decimal("0")
        for trade in closed:
            if _trade_closed_today_utc(trade.get("closed_at") or trade.get("created_at")):
                realized_today += _decimal_or_zero(trade.get("realized_pnl"))

        losing_streak = 0
        for trade in closed:
            pnl = _decimal_or_zero(trade.get("realized_pnl"))
            if pnl < 0:
                losing_streak += 1
                continue
            break

        self.risk.state.realized_pnl_today = realized_today
        self.risk.state.losing_streak = losing_streak
        self.risk.state.cooldown_until = None
        self.risk.state.pnl_date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            await self._persist_risk_state()
        except Exception:
            logger.exception("Could not persist rebuilt risk state.")

    async def _persist_risk_state(self) -> None:
        state = self.risk.state
        cooldown = state.cooldown_until.isoformat() if state.cooldown_until else None
        await self.db.save_risk_state(
            state.losing_streak,
            cooldown,
            str(state.realized_pnl_today),
            state.pnl_date_utc or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )

    async def _report_sqz_cohort_milestones(self, recent_trades: list[dict[str, Any]]) -> None:
        try:
            await self.sqz_cohort_reporter.maybe_report(recent_trades)
        except Exception:
            # Research telemetry must never hold up the execution cycle.
            logger.exception("P7-17 SQZ cohort milestone reporting failed.")

    async def run_forever(self) -> None:
        await self.start()
        self._watchdog_stop = False
        watchdog_thread = start_watchdog_thread(
            "trading-bot-v2-1 running",
            interval_sec=20.0,
            stop_when=lambda: self._watchdog_stop,
        )
        if watchdog_thread is None:
            self.systemd.ready()
        try:
            while True:
                self.systemd.watchdog("trading-bot-v2-1 running")
                if self.emergency_stop_active():
                    logger.critical("Emergency stop is active; no new cycles will run.")
                    if self.config.is_live and not self._emergency_actions_applied:
                        await self._apply_live_emergency_stop()
                        self._emergency_actions_applied = True
                    await asyncio.sleep(self.config.trading.poll_interval_sec)
                    continue
                self._emergency_actions_applied = False
                await self.run_cycle()
                await asyncio.sleep(self.config.trading.poll_interval_sec)
        finally:
            self._watchdog_stop = True
            await self.stop()

    async def run_cycle(self) -> None:
        await self._sync_paper_positions()
        await self._sync_live_positions_from_exchange()
        await self._check_operational_incidents()

        # ── Disaster Mode ────────────────────────────────────────────────────
        # A CRITICAL API outage otherwise prevents the normal cycle from ever
        # reaching a successful Binance call, leaving the in-memory counter
        # permanently above the recovery threshold. Probe before re-evaluating
        # the detector; this does not fetch market data or permit an entry.
        if self.disaster.blocks_new_entries and not self.disaster.requires_position_close:
            await self._probe_disaster_recovery()
        disaster_level = await self.disaster.check()
        if self.disaster.requires_position_close:
            await self._close_all_positions("emergency disaster mode")
            return
        if self.disaster.blocks_new_entries:
            logger.warning("Disaster mode ACTIVE (%s): новые входы заблокированы", disaster_level.value)
            return

        # ── Стандартный цикл ─────────────────────────────────────────────────
        if self.config.is_live:
            if self.config.trade_management.user_stream_required_for_live and not self.supervisor.is_healthy():
                await self.incidents.send(
                    "user_stream_stale",
                    "User stream is stale/disconnected; new entries are blocked.",
                    self.telegram.risk_warning,
                )
                self.disaster.record_api_failure()
                await self.supervisor.reconcile_managed_trades()
                return
            self.disaster.record_api_success()
            await self.supervisor.reconcile_managed_trades()

        try:
            assets = await self.universe.build()
            self.disaster.record_api_success()
        except Exception as exc:
            self.disaster.record_api_failure()
            logger.error("Universe build failed: %s", exc)
            await self.telegram.api_error(f"Universe build error: {exc}")
            return

        symbols = [asset.symbol for asset in assets]
        logger.info("Universe: %s", symbols)
        await self.telegram.universe(symbols)

        equity = await self.current_equity_usdt()
        btc_4h_change = await self._btc_4h_change()
        recent_trades = await self.db.recent_trades(10_000)
        await self._report_sqz_cohort_milestones(recent_trades)
        adaptive_thresholds = (
            self.self_learning.adaptive_thresholds(
                _trades_since_stats_epoch(recent_trades, self.config.analytics.stats_epoch)
            )
            if self.config.market_filters.use_self_learning_filters
            else {}
        )
        effective_risk_pct = self.kelly.risk_pct(recent_trades)

        for asset in assets:
            has_active_position = await self.positions.has_position_for_symbol(asset.symbol)

            try:
                candles_15m, candles_1h, candles_4h = await asyncio.gather(
                    self.market_data.candles(asset.symbol, "15m", limit=500),
                    self.market_data.candles(asset.symbol, "1h", limit=500),
                    self.market_data.candles(asset.symbol, "4h", limit=500),
                )
                self.disaster.record_api_success()
            except Exception as exc:
                self.disaster.record_api_failure()
                logger.warning("Skipping %s: candle fetch failed: %s", asset.symbol, exc)
                await self.telegram.api_error(f"{asset.symbol} candle fetch error: {exc}")
                continue

            price_move_15m = None
            if len(candles_15m) >= 2:
                last = float(candles_15m[-1].close)
                prev = float(candles_15m[-2].close)
                if prev > 0:
                    price_move_15m = (last - prev) / prev * 100

            asset_disaster_reason = _asset_disaster_skip_reason(
                asset.symbol,
                asset.metrics,
                price_move_15m,
                self.disaster.config,
            )
            if asset_disaster_reason:
                logger.warning("Asset disaster check: пропуск %s — %s", asset.symbol, asset_disaster_reason)
                continue

            self.corr_filter.update(asset.symbol, candles_1h)
            shadow_signals = self.strategy.generate_shadow(asset.symbol, candles_15m, candles_1h, candles_4h, asset.metrics)
            await self._record_strategy_diagnostics(self.strategy.drain_diagnostics())
            for shadow_signal in shadow_signals:
                shadow_signal = self._annotate_signal_mode(shadow_signal, "shadow", shadow_only=True)
                shadow_signal, annotation = self._annotate_order_flow(shadow_signal, candles_15m, asset.metrics)
                await self._record_order_flow_annotation(shadow_signal, annotation)
                shadow_signal, relative_strength = self._annotate_relative_strength(
                    shadow_signal,
                    candles_4h,
                    btc_4h_change,
                )
                await self._record_relative_strength_annotation(shadow_signal, relative_strength)
                await self._record_shadow_signal(shadow_signal, asset.filters)
            signal = self.strategy.generate(asset.symbol, candles_15m, candles_1h, candles_4h, asset.metrics)
            await self._record_strategy_diagnostics(self.strategy.drain_diagnostics())
            if not signal:
                continue
            signal = self._annotate_signal_mode(
                signal,
                self.config.strategy.mode_for_strategy(signal.metadata.get("strategy", "")),
            )
            signal, annotation = self._annotate_order_flow(signal, candles_15m, asset.metrics)
            await self._record_order_flow_annotation(signal, annotation)
            signal, relative_strength = self._annotate_relative_strength(signal, candles_4h, btc_4h_change)
            await self._record_relative_strength_annotation(signal, relative_strength)
            await self._record_sqz_gate_cohort_shadows(signal, asset.filters)
            if has_active_position:
                logger.info(
                    "Active %s position exists; strict paper entry skipped after shadow diagnostics.",
                    asset.symbol,
                )
                continue
            order_flow_rejection = _order_flow_entry_rejection_reason(
                signal, self.config.strategy
            )
            controlled_paper_signal = _controlled_paper_sqz_override(
                signal,
                order_flow_rejection,
                self.config.strategy,
            )
            if controlled_paper_signal is not None:
                signal = controlled_paper_signal
                order_flow_rejection = None
                logger.info(
                    "Controlled SQZ paper admission: %s %s (neutral relative strength, capped risk)",
                    signal.symbol,
                    signal.direction.value,
                )
            measured_paper_signal = _squeeze_order_flow_measurement_override(
                signal,
                order_flow_rejection,
                self.config.strategy,
            )
            if measured_paper_signal is not None:
                signal = measured_paper_signal
                order_flow_rejection = None
                logger.info(
                    "SQZ OF measurement admission: %s %s (weak mixed flow, capped risk)",
                    signal.symbol,
                    signal.direction.value,
                )
            if order_flow_rejection:
                filter_type, reason = order_flow_rejection
                try:
                    await self.db.insert_filter_rejection(
                        symbol=signal.symbol,
                        direction=signal.direction.value,
                        strategy=_signal_strategy(signal),
                        confidence=str(signal.confidence),
                        filter_type=filter_type,
                        reason=reason,
                    )
                except Exception:
                    pass
                logger.info("Order-flow gate rejected %s: %s", signal.symbol, reason)
                await self._record_ml_feature_snapshot(signal, "REJECTED_ORDER_FLOW_GATE", reason)
                continue
            mr_context_rejection = _mean_reversion_context_rejection_reason(
                signal,
                btc_4h_change,
                annotation,
                self.config.strategy,
            )
            if mr_context_rejection:
                filter_type, reason = mr_context_rejection
                logger.info("MR context gate rejected %s: %s", signal.symbol, reason)
                try:
                    await self.db.insert_filter_rejection(
                        symbol=signal.symbol,
                        direction=signal.direction.value,
                        strategy=_signal_strategy(signal),
                        confidence=str(signal.confidence),
                        filter_type=filter_type,
                        reason=reason,
                    )
                except Exception:
                    pass
                await self._record_ml_feature_snapshot(signal, "REJECTED_MR_CONTEXT_GATE", reason)
                continue
            mr_expectancy_rejection = _mean_reversion_expectancy_rejection_reason(
                signal,
                self.config.strategy,
                self.config.risk,
            )
            if mr_expectancy_rejection:
                filter_type, reason = mr_expectancy_rejection
                logger.info("MR expectancy gate rejected %s: %s", signal.symbol, reason)
                try:
                    await self.db.insert_filter_rejection(
                        symbol=signal.symbol,
                        direction=signal.direction.value,
                        strategy=_signal_strategy(signal),
                        confidence=str(signal.confidence),
                        filter_type=filter_type,
                        reason=reason,
                    )
                except Exception:
                    pass
                await self._record_ml_feature_snapshot(signal, "REJECTED_MR_EXPECTANCY_GATE", reason)
                continue
            oi_chg = asset.metrics.open_interest_change_pct if asset.metrics else None
            symbol_4h_change = _symbol_4h_change_pct(
                candles_4h,
                self.config.market_filters.symbol_4h_trend_lookback_bars,
            )
            filter_decision = self.entry_filter.allow_signal(
                signal,
                btc_4h_change,
                adaptive_thresholds,
                oi_chg,
                symbol_4h_change_pct=symbol_4h_change,
            )
            if filter_decision.allowed:
                active_pos = await self.positions.active_positions()
                corr_reason = await self._refresh_realtime_correlation(signal.symbol, active_pos)
                corr_ok = not corr_reason
                if corr_ok:
                    corr_ok, corr_reason = self.corr_filter.allow_entry(signal.symbol, signal.direction, active_pos)
                if not corr_ok:
                    from trading_bot.market_filters import FilterDecision
                    filter_decision = FilterDecision(False, corr_reason)
            if not filter_decision.allowed:
                logger.info("Entry filter rejected %s: %s", signal.symbol, filter_decision.reason)
                try:
                    _strat = signal.metadata.get("strategy", "UNKNOWN") if signal.metadata else "UNKNOWN"
                    _ftype = "CORRELATION" if "Corr(" in filter_decision.reason else (
                        "COUNTER_TREND" if ("counter-trend" in filter_decision.reason or "short entries blocked" in filter_decision.reason) else (
                        "OI" if "OI " in filter_decision.reason else (
                        "UTC" if "UTC hour" in filter_decision.reason else (
                        "BTC_DROP" if "BTC 4h" in filter_decision.reason else "OTHER"))))
                    await self.db.insert_filter_rejection(
                        symbol=signal.symbol,
                        direction=signal.direction.value,
                        strategy=_strat,
                        confidence=str(signal.confidence),
                        filter_type=_ftype,
                        reason=filter_decision.reason,
                    )
                except Exception:
                    pass
                await self._record_ml_feature_snapshot(signal, "REJECTED_ENTRY_FILTER", filter_decision.reason)
                continue
            prediction = self.ml_filter.predict(signal)
            if self.config.ml.enabled:
                await self._record_ml_feature_snapshot(
                    signal,
                    "ML_SHADOW_SCORE",
                    f"{prediction.reason} (conf={prediction.confidence})",
                )
            if not prediction.allow_trade:
                logger.info("ML rejected %s: %s confidence=%s", signal.symbol, prediction.reason, prediction.confidence)
                try:
                    _strat = signal.metadata.get("strategy", "UNKNOWN") if signal.metadata else "UNKNOWN"
                    await self.db.insert_filter_rejection(
                        symbol=signal.symbol,
                        direction=signal.direction.value,
                        strategy=_strat,
                        confidence=str(signal.confidence),
                        filter_type="ML",
                        reason=f"ML: {prediction.reason} (conf={prediction.confidence})",
                    )
                except Exception:
                    pass
                await self._record_ml_feature_snapshot(
                    signal,
                    "REJECTED_ML",
                    f"{prediction.reason} (conf={prediction.confidence})",
                )
                continue
            cooldown_reason = _symbol_loss_cooldown_reason(
                signal.symbol,
                recent_trades,
                self.config.risk.symbol_cooldown_after_loss_minutes,
            )
            if cooldown_reason:
                logger.info("Symbol cooldown rejected %s: %s", signal.symbol, cooldown_reason)
                try:
                    _strat = signal.metadata.get("strategy", "UNKNOWN") if signal.metadata else "UNKNOWN"
                    await self.db.insert_filter_rejection(
                        symbol=signal.symbol,
                        direction=signal.direction.value,
                        strategy=_strat,
                        confidence=str(signal.confidence),
                        filter_type="RISK",
                        reason=cooldown_reason,
                    )
                except Exception:
                    pass
                await self._record_ml_feature_snapshot(signal, "REJECTED_COOLDOWN", cooldown_reason)
                continue
            reentry_reason = _strategy_reentry_policy_reason(
                signal,
                recent_trades,
                self.config.risk.strategy_reentry_cooldown_minutes,
                self.config.risk.strategy_reentry_winning_cooldown_minutes,
                self.config.risk.scale_in_enabled,
                self.config.risk.max_scale_ins_per_symbol_strategy,
            )
            if reentry_reason:
                logger.info("Strategy re-entry rejected %s: %s", signal.symbol, reentry_reason)
                try:
                    _strat = signal.metadata.get("strategy", "UNKNOWN") if signal.metadata else "UNKNOWN"
                    await self.db.insert_filter_rejection(
                        symbol=signal.symbol,
                        direction=signal.direction.value,
                        strategy=_strat,
                        confidence=str(signal.confidence),
                        filter_type="RISK",
                        reason=reentry_reason,
                    )
                except Exception:
                    pass
                await self._record_ml_feature_snapshot(signal, "REJECTED_REENTRY_POLICY", reentry_reason)
                continue
            await self.db.insert_signal(signal)
            await self.telegram.signal(signal.symbol, signal.direction.value, signal.style.value, signal.reason)
            try:
                controlled_risk_cap = _controlled_paper_risk_cap(signal)
                base_risk_pct = min(effective_risk_pct, controlled_risk_cap) if controlled_risk_cap else effective_risk_pct
                sizing = self._dynamic_sizing_decision(signal, base_risk_pct)
                sizing = _cap_controlled_paper_sizing(sizing, controlled_risk_cap)
                signal = self._annotate_dynamic_sizing(signal, sizing)
                plan = self.risk.calculate_plan(
                    signal=signal,
                    equity_usdt=equity,
                    filters=asset.filters,
                    leverage=sizing.leverage,
                    active_positions=await self.positions.active_positions(),
                    live_mode=self.config.is_live,
                    risk_per_trade_pct=sizing.risk_pct,
                )
                for warning in plan.warnings:
                    if "funding" in warning.lower():
                        await self.incidents.send("funding_pressure", warning, self.telegram.risk_warning)
                    else:
                        await self.telegram.risk_warning(warning)
                result = await self.orders.execute(plan)
                if self.config.is_live and result.accepted:
                    self.supervisor.register_plan(plan, result)
                await self._record_ml_feature_snapshot(
                    signal,
                    "ACCEPTED_TRADE" if result.accepted else "REJECTED_ORDER",
                    result.message,
                )
                stored_plan = _plan_with_paper_entry_fill(plan, result, self.config.mode)
                await self.db.insert_trade(
                    stored_plan,
                    self.config.mode.value,
                    "ACCEPTED",
                    {
                        "message": result.message,
                        "trade_id": result.trade_id,
                        "notional": str(stored_plan.notional),
                        "initial_margin": str(stored_plan.initial_margin),
                        "leverage": str(stored_plan.leverage),
                        "client_order_ids": result.client_order_ids,
                        "entry_order": result.entry_order,
                        "stop_order": result.stop_order,
                        "take_profit_orders": list(result.take_profit_orders),
                        "execution": result.execution_metadata,
                        "dynamic_sizing": sizing.to_metadata(),
                        "signal_metadata": plan.signal_metadata,
                        **_trade_cluster_metadata(
                            signal,
                            recent_trades,
                            self.config.risk.trade_cluster_window_minutes,
                        ),
                        "partial_take_profits": [
                            {
                                "name": target.name,
                                "price": str(target.price),
                                "quantity": str(target.quantity),
                                "fraction": str(target.fraction),
                                "reward_risk": str(target.reward_risk),
                                "move_stop_to_breakeven": target.move_stop_to_breakeven,
                                "activate_trailing": target.activate_trailing,
                            }
                            for target in plan.partial_take_profits
                        ],
                        "protection": (
                            {
                                "initial_stop": str(plan.protection.initial_stop),
                                "breakeven_price": str(plan.protection.breakeven_price),
                                "breakeven_after_target": plan.protection.breakeven_after_target,
                                "trailing_enabled": plan.protection.trailing_enabled,
                                "trailing_activation_reward_risk": str(
                                    plan.protection.trailing_activation_reward_risk
                                ),
                                "trailing_callback_rate_pct": str(plan.protection.trailing_callback_rate_pct),
                            }
                            if plan.protection
                            else None
                        ),
                        "filled_partial_targets": [],
                    },
                )
                await self.telegram.trade_opened(
                    plan.symbol,
                    str(plan.quantity),
                    str(stored_plan.entry_price),
                    str(plan.stop_loss),
                    str(plan.take_profit),
                )
                logger.info("Trade accepted: %s", result.message)
                continue
            except RiskError as exc:
                logger.warning("Risk rejected %s: %s", signal.symbol, exc)
                await self.telegram.risk_warning(str(exc))
                try:
                    _strat = signal.metadata.get("strategy", "UNKNOWN") if signal.metadata else "UNKNOWN"
                    await self.db.insert_filter_rejection(
                        symbol=signal.symbol,
                        direction=signal.direction.value,
                        strategy=_strat,
                        confidence=str(signal.confidence),
                        filter_type="RISK",
                        reason=str(exc),
                    )
                except Exception:
                    pass
                await self._record_ml_feature_snapshot(signal, "REJECTED_RISK", str(exc))
            except Exception as exc:
                logger.exception("Execution error for %s", signal.symbol)
                self.disaster.record_api_failure()
                if self.config.is_live and "protective" in str(exc).lower():
                    await self.incidents.send(
                        "protective_order_rejected",
                        f"{signal.symbol}: protective order incident: {exc}",
                        self.telegram.risk_warning,
                    )
                await self.telegram.api_error(str(exc))

        regime_summary = self.regime.pop_verdict_summary()
        if regime_summary:
            summary_text = ", ".join(
                f"{name}={count}" for name, count in sorted(regime_summary.items(), key=lambda kv: -kv[1])
            )
            logger.info("Regime detector verdicts this cycle: %s", summary_text)

    def _annotate_signal_mode(self, signal: Signal, strategy_mode: str, shadow_only: bool = False) -> Signal:
        strategy = _signal_strategy(signal)
        metadata = {
            **dict(signal.metadata),
            "strategy_mode": strategy_mode,
        }
        strategy_logic_version = _strategy_logic_version(strategy)
        if strategy_logic_version:
            metadata["strategy_logic_version"] = strategy_logic_version
        if shadow_only:
            metadata["shadow_only"] = True
        return replace(signal, metadata=metadata)

    def _annotate_order_flow(
        self,
        signal: Signal,
        candles_15m: list,
        metrics: Any,
    ) -> tuple[Signal, OrderFlowAnnotation]:
        annotation = self.order_flow.annotate(candles_15m, signal.direction, metrics)
        metadata = {
            **dict(signal.metadata or {}),
            "order_flow": annotation.to_metadata(),
        }
        return replace(signal, metadata=metadata), annotation

    def _dynamic_sizing_decision(
        self,
        signal: Signal,
        base_risk_pct: Decimal,
        strategy_mode: str | None = None,
    ) -> DynamicSizingDecision:
        mode = strategy_mode or str((signal.metadata or {}).get("strategy_mode") or "paper")
        return dynamic_position_sizing(
            signal=signal,
            config=self.config.risk,
            base_risk_pct=base_risk_pct,
            strategy_mode=mode,
        )

    def _annotate_dynamic_sizing(self, signal: Signal, sizing: DynamicSizingDecision) -> Signal:
        metadata = {
            **dict(signal.metadata or {}),
            "dynamic_sizing": sizing.to_metadata(),
            "risk_per_trade_pct": str(sizing.risk_pct),
            "leverage": sizing.leverage,
        }
        return replace(signal, metadata=metadata)

    async def _record_shadow_signal(self, signal: Signal, filters: SymbolFilters | None = None) -> None:
        shadow_signal = self._annotate_signal_mode(signal, "shadow", shadow_only=True)
        shadow_signal = _controlled_shadow_revalidation_override(
            shadow_signal,
            self.config.strategy,
        )
        shadow_signal = _controlled_shadow_sqz_dynamic_neutral_override(
            shadow_signal,
            self.config.strategy,
        )
        strict_context_rejection = _shadow_candidate_context_rejection_reason(
            shadow_signal,
            enforce_order_flow=True,
        )
        enforce_order_flow = self.config.strategy.shadow_order_flow_hard_gate
        context_rejection = _shadow_candidate_context_rejection_reason(
            shadow_signal,
            enforce_order_flow=enforce_order_flow,
        )
        shadow_signal = _annotate_shadow_order_flow_gate(
            shadow_signal,
            strict_rejection_reason=strict_context_rejection,
            execution_rejection_reason=context_rejection,
            enforced=enforce_order_flow,
            overridden=bool(strict_context_rejection and context_rejection is None and not enforce_order_flow),
        )
        logger.info(
            "Shadow signal recorded: %s %s strategy=%s confidence=%s",
            shadow_signal.symbol,
            shadow_signal.direction.value,
            shadow_signal.metadata.get("strategy", "UNKNOWN"),
            shadow_signal.confidence,
        )
        try:
            await self.db.insert_signal(shadow_signal)
        except Exception:
            logger.exception("Failed to record shadow signal for %s", shadow_signal.symbol)
        await self._record_ml_feature_snapshot(
            shadow_signal,
            "SHADOW_SIGNAL",
            "candidate strategy shadow-only; no order attempted",
        )
        if not _measurement_shadow_payload(shadow_signal):
            await self._record_shadow_gate_counterfactuals(shadow_signal, filters)
            await self._record_shadow_parallel_lab(shadow_signal, filters)
            await self._record_shadow_conditional_lab(shadow_signal, filters)
        diagnostic_only_reason = _shadow_paper_diagnostic_only_reason(
            shadow_signal,
            self.config.strategy,
        )
        if diagnostic_only_reason:
            logger.info(
                "Shadow paper diagnostic-only for %s %s: %s",
                _signal_strategy(shadow_signal),
                shadow_signal.symbol,
                diagnostic_only_reason,
            )
            await self._record_ml_feature_snapshot(
                shadow_signal,
                "SHADOW_PAPER_REJECTED_CONTEXT",
                diagnostic_only_reason,
            )
            return
        if context_rejection:
            logger.info(
                "Shadow paper context rejected for %s %s: %s",
                _signal_strategy(shadow_signal),
                shadow_signal.symbol,
                context_rejection,
            )
            await self._record_ml_feature_snapshot(
                shadow_signal,
                "SHADOW_PAPER_REJECTED_CONTEXT",
                context_rejection,
            )
            return
        if strict_context_rejection and not enforce_order_flow:
            logger.info(
                "Shadow paper OF gate observed, not enforced for %s %s: %s",
                _signal_strategy(shadow_signal),
                shadow_signal.symbol,
                strict_context_rejection,
            )
            await self._record_ml_feature_snapshot(
                shadow_signal,
                "SHADOW_ORDER_FLOW_GATE_OBSERVED",
                strict_context_rejection,
            )
        await self._open_shadow_paper_trade(shadow_signal, filters)

    async def _record_shadow_gate_counterfactuals(
        self,
        signal: Signal,
        filters: SymbolFilters | None = None,
    ) -> None:
        """Persist one-gate virtual counterfactuals without changing source admission."""
        variants, strict_reason, evaluated_gates = _shadow_gate_counterfactual_variants(
            signal,
            self.config.strategy,
        )
        if not evaluated_gates:
            return
        summary = {
            "source_strategy": _signal_strategy(signal),
            "source_cluster_id": _shadow_gate_source_cluster_id(signal),
            "strict_rejection_reason": strict_reason,
            "evaluated_gates": evaluated_gates,
            "admitted_cohorts": [_shadow_execution_strategy(item) for item in variants],
        }
        await self._record_ml_feature_snapshot(
            signal,
            "SHADOW_GATE_COUNTERFACTUAL_EVALUATED",
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
        )
        if not variants:
            return

        try:
            shadow_history = await self.db.recent_shadow_trades(limit=1_000)
        except Exception:
            logger.exception("Failed to load shadow history for gate counterfactuals")
            return
        for variant in variants:
            payload = _measurement_shadow_payload(variant)
            strategy = _shadow_execution_strategy(variant)
            source_cluster_id = str(payload.get("source_cluster_id") or "")
            if _measurement_shadow_source_seen(shadow_history, strategy, source_cluster_id):
                continue
            await self._record_shadow_signal(variant, filters)

    async def _record_shadow_parallel_lab(
        self,
        signal: Signal,
        filters: SymbolFilters | None = None,
    ) -> None:
        """Replay one pre-context candidate through independent virtual policies."""
        variants, strict_reason, evaluated_arms = _shadow_parallel_lab_variants(
            signal,
            self.config.strategy,
        )
        if not evaluated_arms:
            return
        source_cluster_id = _shadow_gate_source_cluster_id(signal)
        await self._record_ml_feature_snapshot(
            signal,
            "SHADOW_PARALLEL_LAB_EVALUATED",
            json.dumps(
                {
                    "source_strategy": _signal_strategy(signal),
                    "source_cluster_id": source_cluster_id,
                    "strict_rejection_reason": strict_reason,
                    "evaluated_arms": evaluated_arms,
                    "admitted_cohorts": [_shadow_execution_strategy(item) for item in variants],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        if not variants:
            return

        try:
            shadow_history = await self.db.recent_shadow_trades(limit=1_000)
        except Exception:
            logger.exception("Failed to load shadow history for parallel lab")
            return
        for variant in variants:
            payload = _measurement_shadow_payload(variant)
            strategy = _shadow_execution_strategy(variant)
            source_cluster_id = str(payload.get("source_cluster_id") or "")
            if _measurement_shadow_source_seen(shadow_history, strategy, source_cluster_id):
                continue
            await self._record_shadow_signal(variant, filters)

    async def _record_shadow_conditional_lab(
        self,
        signal: Signal,
        filters: SymbolFilters | None = None,
    ) -> None:
        """Place a candidate into isolated v1/v2 measurement buckets."""
        evaluated: list[tuple[Signal, dict[str, Any], str]] = []
        for variant, profile, decision in (
            (*_shadow_conditional_lab_variant(signal, self.config.strategy), "SHADOW_CONDITIONAL_LAB_EVALUATED"),
            (*_shadow_conditional_lab_v2_variant(signal, self.config.strategy), "SHADOW_CONDITIONAL_LAB_V2_EVALUATED"),
        ):
            if variant is None or profile is None:
                continue
            evaluated.append((variant, profile, decision))
            await self._record_ml_feature_snapshot(
                signal,
                decision,
                json.dumps(profile, ensure_ascii=False, sort_keys=True),
            )
        if not evaluated:
            return
        try:
            shadow_history = await self.db.recent_shadow_trades(limit=1_000)
        except Exception:
            logger.exception("Failed to load shadow history for conditional lab")
            return
        for variant, _, _ in evaluated:
            payload = _measurement_shadow_payload(variant)
            strategy = _shadow_execution_strategy(variant)
            source_cluster_id = str(payload.get("source_cluster_id") or "")
            if _conditional_shadow_source_seen(shadow_history, payload):
                continue
            await self._record_shadow_signal(variant, filters)

    async def _record_sqz_gate_cohort_shadows(
        self,
        signal: Signal,
        filters: SymbolFilters | None = None,
    ) -> None:
        """Measure one SQZ admission gate at a time without touching paper flow."""
        variants, gate_vector, safety_rejections = _sqz_gate_cohort_shadow_variants(
            signal,
            self.config.strategy,
        )
        if _signal_strategy(signal) != "SQUEEZE_BREAKOUT":
            return

        source_cluster_id = _sqz_source_cluster_id(signal)
        summary = {
            "source_cluster_id": source_cluster_id,
            "gate_vector": gate_vector,
            "safety_rejections": safety_rejections,
            "cohorts": [_shadow_execution_strategy(item) for item in variants],
        }
        await self._record_ml_feature_snapshot(
            signal,
            "SQZ_SHADOW_COHORT_EVALUATED",
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
        )
        if not variants:
            return

        shadow_history = await self.db.recent_shadow_trades(limit=1_000)
        for variant in variants:
            strategy = _shadow_execution_strategy(variant)
            if _measurement_shadow_source_seen(shadow_history, strategy, source_cluster_id):
                continue
            await self._record_shadow_signal(variant, filters)

    async def _open_shadow_paper_trade(self, signal: Signal, filters: SymbolFilters | None = None) -> None:
        metadata = signal.metadata or {}
        source_strategy = str(metadata.get("strategy_source") or _signal_strategy(signal))
        strategy = _shadow_execution_strategy(signal)
        policy_signal = signal
        if strategy != source_strategy:
            policy_signal = replace(
                signal,
                metadata={
                    **dict(signal.metadata or {}),
                    "source_strategy": source_strategy,
                    "strategy": strategy,
                },
            )
        if not signal.is_tradeable:
            return
        if signal.stop_loss is None or signal.take_profit is None:
            return
        try:
            if await self.db.has_open_shadow_trade(signal.symbol, strategy):
                logger.info("Shadow paper %s %s already open; skipping duplicate.", strategy, signal.symbol)
                return
            shadow_history = await self.db.recent_shadow_trades(limit=1_000)
            measurement_payload = _measurement_shadow_payload(signal)
            source_cluster_id = str(measurement_payload.get("source_cluster_id") or "")
            conditional_seen = (
                _conditional_shadow_source_seen(shadow_history, measurement_payload)
                if measurement_payload
                else False
            )
            strategy_seen = (
                _measurement_shadow_source_seen(shadow_history, strategy, source_cluster_id)
                if measurement_payload
                else False
            )
            if measurement_payload and (conditional_seen or strategy_seen):
                logger.info("Shadow measurement %s already recorded source %s.", strategy, source_cluster_id)
                return
            if not measurement_payload:
                reentry_reason = _strategy_reentry_policy_reason(
                    signal=policy_signal,
                    trades=shadow_history,
                    cooldown_minutes=self.config.risk.strategy_reentry_cooldown_minutes,
                    winning_cooldown_minutes=self.config.risk.strategy_reentry_winning_cooldown_minutes,
                    scale_in_enabled=False,
                    max_scale_ins_per_symbol_strategy=0,
                )
                if reentry_reason:
                    logger.info("Shadow paper %s %s re-entry blocked: %s", strategy, signal.symbol, reentry_reason)
                    await self._record_ml_feature_snapshot(
                        signal,
                        "SHADOW_PAPER_REJECTED_COOLDOWN",
                        reentry_reason,
                    )
                    return
                series_reason = _sqz_dynamic_upd_series_rejection_reason(
                    signal=policy_signal,
                    trades=shadow_history,
                    window_minutes=90,
                    max_same_direction_trades=2,
                )
                if series_reason:
                    logger.info("Shadow paper %s %s series blocked: %s", strategy, signal.symbol, series_reason)
                    await self._record_ml_feature_snapshot(
                        signal,
                        "SHADOW_PAPER_REJECTED_CONTEXT",
                        series_reason,
                    )
                    return
                if not _is_shadow_revalidation_signal(signal):
                    loss_control_reason = _shadow_strategy_loss_control_reason(
                        signal=policy_signal,
                        trades=shadow_history,
                        window_hours=168,
                        min_closed_trades=2,
                        max_total_r=Decimal("-1.50"),
                        max_loss_count=2,
                        loss_count_max_total_r=Decimal("-1.00"),
                    )
                    if loss_control_reason:
                        logger.info("Shadow paper %s %s loss-control blocked: %s", strategy, signal.symbol, loss_control_reason)
                        await self._record_ml_feature_snapshot(
                            signal,
                            "SHADOW_PAPER_REJECTED_LOSS_CONTROL",
                            loss_control_reason,
                        )
                        return
        except Exception:
            logger.exception("Failed to check shadow paper re-entry policy for %s %s", strategy, signal.symbol)
            return
        try:
            equity = await self.current_equity_usdt()
        except Exception:
            logger.exception("Failed to calculate compounding equity for shadow paper %s %s", strategy, signal.symbol)
            equity = self.config.account.initial_equity_usdt
        if equity is None or equity <= 0:
            return
        sizing = self._dynamic_sizing_decision(
            signal,
            self.config.risk.risk_per_trade_pct,
            strategy_mode="shadow",
        )
        sizing = _cap_controlled_shadow_sizing(sizing, _controlled_shadow_risk_cap(signal))
        signal = self._annotate_dynamic_sizing(signal, sizing)
        plan_metadata: dict[str, Any] = {
            "equity_used": str(equity),
            "risk_per_trade_pct": str(sizing.risk_pct),
            "dynamic_sizing": sizing.to_metadata(),
        }
        measurement_payload = _measurement_shadow_payload(signal)
        if measurement_payload:
            plan_metadata["trade_cluster_id"] = measurement_payload.get("source_cluster_id")
            plan_metadata["trade_cluster_sequence"] = 1
            plan_metadata["measurement_shadow"] = measurement_payload
        if filters is not None:
            try:
                plan = self.risk.calculate_plan(
                    signal=signal,
                    equity_usdt=equity,
                    filters=filters,
                    leverage=sizing.leverage,
                    active_positions=[],
                    live_mode=False,
                    risk_per_trade_pct=sizing.risk_pct,
                )
            except RiskError as exc:
                logger.info("Shadow paper rejected by risk manager for %s %s: %s", strategy, signal.symbol, exc)
                await self._record_ml_feature_snapshot(signal, "SHADOW_PAPER_REJECTED_RISK", str(exc))
                return
            quantity = plan.quantity
            risk_amount = plan.risk_amount
            plan_metadata.update(
                {
                    "notional": str(plan.notional),
                    "initial_margin": str(plan.initial_margin),
                    "leverage": str(plan.leverage),
                    "risk_pct": str(plan.risk_pct),
                    "risk_warnings": list(plan.warnings),
                }
            )
            plan_metadata.update(_risk_plan_exit_metadata(plan))
        else:
            quantity, risk_amount = self._fallback_shadow_size(signal, equity, sizing.risk_pct)
            if quantity is None or risk_amount is None:
                return
            plan_metadata["sizing_fallback"] = True

        if quantity <= 0 or risk_amount <= 0:
            return
        try:
            shadow_entry_order = simulated_local_entry_order(
                symbol=signal.symbol,
                direction=signal.direction,
                quantity=quantity,
                planned_entry_price=signal.entry_price,
                taker_fee_bps=self.config.risk.taker_fee_bps,
                slippage_bps=self.config.risk.slippage_bps,
            )
            shadow_signal = replace(
                signal,
                entry_price=Decimal(str(shadow_entry_order["avgPrice"])),
                metadata={
                    **dict(signal.metadata or {}),
                    "source_strategy": source_strategy,
                    "strategy": strategy,
                    "strategy_logic_version": _strategy_logic_version(strategy)
                    or (signal.metadata or {}).get("strategy_logic_version"),
                },
            )
            await self.db.insert_shadow_trade(
                signal=shadow_signal,
                strategy=strategy,
                quantity=str(quantity),
                risk_amount=str(risk_amount),
                metadata={
                    "strategy": strategy,
                    "strategy_mode": "shadow",
                    "shadow_only": True,
                    "shadow_paper": True,
                    "source": "shadow_signal",
                    "signal_reason": shadow_signal.reason,
                    "signal_metadata": dict(shadow_signal.metadata or {}),
                    "entry_order": shadow_entry_order,
                    "execution": OrderManager._execution_metadata(shadow_entry_order, quantity),
                    **plan_metadata,
                },
            )
            logger.info(
                "Shadow paper opened: %s %s %s qty=%s risk=%s equity=%s",
                strategy,
                signal.symbol,
                signal.direction.value,
                quantity,
                risk_amount,
                equity,
            )
            await self._record_ml_feature_snapshot(
                shadow_signal,
                "SHADOW_PAPER_OPENED",
                "virtual shadow-paper trade opened; no real/paper order attempted",
            )
        except Exception:
            logger.exception("Failed to open shadow paper trade for %s %s", strategy, signal.symbol)

    def _fallback_shadow_size(
        self,
        signal: Signal,
        equity: Decimal,
        risk_per_trade_pct: Decimal | None = None,
    ) -> tuple[Decimal | None, Decimal | None]:
        stop_distance = abs(signal.entry_price - signal.stop_loss)
        if stop_distance <= 0:
            return None, None
        risk_amount = equity * (risk_per_trade_pct or self.config.risk.risk_per_trade_pct)
        if risk_amount <= 0:
            return None, None
        quantity = risk_amount / stop_distance
        if quantity <= 0:
            return None, None
        return quantity, risk_amount

    async def _record_ml_feature_snapshot(self, signal: Signal, decision: str, reason: str) -> None:
        try:
            await self.db.insert_ml_feature_snapshot(
                symbol=signal.symbol,
                direction=signal.direction.value,
                strategy=_signal_strategy(signal),
                confidence=str(signal.confidence),
                decision=decision,
                reason=reason,
                features=self.ml_filter.features_for_signal(signal),
                metadata=dict(signal.metadata or {}),
            )
        except Exception as exc:
            logger.debug("ML feature snapshot failed for %s: %s", signal.symbol, exc)

    async def _record_order_flow_annotation(self, signal: Signal, annotation: OrderFlowAnnotation) -> None:
        payload = annotation.to_metadata()
        try:
            await self.db.insert_ml_feature_snapshot(
                symbol=signal.symbol,
                direction=signal.direction.value,
                strategy=_signal_strategy(signal),
                confidence=str(signal.confidence),
                decision="ORDER_FLOW_ANNOTATION",
                reason=f"alignment={annotation.alignment}; score={payload['score']}",
                features=payload,
                metadata={
                    **dict(signal.metadata or {}),
                    "order_flow": payload,
                    "order_flow_research_only": True,
                },
            )
        except Exception as exc:
            logger.debug("Order-flow annotation snapshot failed for %s: %s", signal.symbol, exc)

    def _annotate_relative_strength(
        self,
        signal: Signal,
        candles_4h: list[Candle],
        btc_4h_change: Decimal | None,
    ) -> tuple[Signal, RelativeStrengthAnnotation]:
        annotation = annotate_relative_strength(candles_4h, signal.direction, btc_4h_change)
        metadata = {
            **dict(signal.metadata or {}),
            "relative_strength": annotation.to_metadata(),
            "relative_strength_research_only": True,
        }
        return replace(signal, metadata=metadata), annotation

    async def _record_relative_strength_annotation(
        self,
        signal: Signal,
        annotation: RelativeStrengthAnnotation,
    ) -> None:
        payload = annotation.to_metadata()
        try:
            await self.db.insert_ml_feature_snapshot(
                symbol=signal.symbol,
                direction=signal.direction.value,
                strategy=_signal_strategy(signal),
                confidence=str(signal.confidence),
                decision="RELATIVE_STRENGTH_ANNOTATION",
                reason=f"alignment={annotation.alignment}; score={payload['score']}",
                features=payload,
                metadata={
                    **dict(signal.metadata or {}),
                    "relative_strength": payload,
                    "relative_strength_research_only": True,
                },
            )
        except Exception as exc:
            logger.debug("Relative-strength annotation snapshot failed for %s: %s", signal.symbol, exc)

    async def _record_strategy_diagnostics(self, diagnostics: list[dict[str, Any]]) -> None:
        now = time.monotonic()
        ttl_seconds = 900.0
        for diagnostic in diagnostics:
            if diagnostic.get("decision") == "SIGNAL":
                continue
            strategy = str(diagnostic.get("strategy") or "UNKNOWN")
            symbol = str(diagnostic.get("symbol") or "UNKNOWN")
            reason = str(diagnostic.get("block_reason") or "unknown")
            if reason in {"not_evaluated", "insufficient_candles"}:
                continue
            key = (strategy, symbol, reason)
            last_seen = self._strategy_diagnostic_seen.get(key)
            if last_seen is not None and now - last_seen < ttl_seconds:
                continue
            self._strategy_diagnostic_seen[key] = now
            direction = str(diagnostic.get("direction") or Direction.NONE.value)
            confidence = str(diagnostic.get("confidence") or "0")
            try:
                await self.db.insert_ml_feature_snapshot(
                    symbol=symbol,
                    direction=direction,
                    strategy=strategy,
                    confidence=confidence,
                    decision="STRATEGY_DIAGNOSTIC",
                    reason=reason,
                    features={},
                    metadata={**diagnostic, "diagnostic": True},
                )
            except Exception as exc:
                logger.debug("Strategy diagnostic snapshot failed for %s %s: %s", strategy, symbol, exc)

    async def current_equity_usdt(self) -> Decimal:
        if self.config.is_live:
            balances = await self.binance.balance()
            for balance in balances:
                if balance.get("asset") == self.config.account.quote_asset:
                    return Decimal(str(balance.get("availableBalance") or balance.get("balance") or "0"))
            raise RuntimeError(f"No {self.config.account.quote_asset} futures balance found.")
        equity = self.config.account.initial_equity_usdt
        if equity is None:
            raise RuntimeError(
                "No USDT-equivalent starting equity configured. Set STARTING_DEPOSIT_USDT or MANUAL_TENGE_USDT_RATE."
            )
        if self.config.mode == TradingMode.PAPER_TRADING:
            realized_pnl = Decimal("0")
            try:
                summary = await self.db.pnl_summary(TradingMode.PAPER_TRADING.value)
                realized_pnl = Decimal(str(summary.get("realized_pnl") or "0"))
            except Exception:
                logger.exception("Could not include realized paper PnL in equity.")
            unrealized_pnl = Decimal("0")
            try:
                unrealized_pnl = await self._paper_unrealized_pnl()
            except Exception:
                logger.exception("Could not include unrealized paper PnL in equity.")
            return equity + realized_pnl + unrealized_pnl
        return equity

    async def _paper_unrealized_pnl(self) -> Decimal:
        trades = await self.db.recent_trades(5_000)
        mark_prices: dict[str, Decimal] = {}
        total = Decimal("0")
        for trade in trades:
            if not _is_open_paper_trade(trade):
                continue
            symbol = str(trade.get("symbol") or "")
            if not symbol:
                continue
            if symbol not in mark_prices:
                price_payload = await self.binance.ticker_price(symbol)
                mark_prices[symbol] = to_decimal(price_payload.get("price", "0"))
            total += _paper_trade_unrealized_pnl(trade, mark_prices[symbol])
        return total

    async def _sync_paper_positions(self) -> None:
        if self.config.mode != TradingMode.PAPER_TRADING:
            return
        try:
            trades = await self.db.recent_trades(5_000)
        except Exception:
            logger.exception("Could not sync paper positions from database.")
            return

        open_rows: dict[str, dict] = {}
        open_statuses = {"ACCEPTED", "OPEN", "ACTIVE"}
        seen_symbols: set[str] = set()
        for row in trades:
            symbol = str(row.get("symbol", ""))
            if (
                not symbol
                or symbol in seen_symbols
                or row.get("mode") != TradingMode.PAPER_TRADING.value
            ):
                continue
            seen_symbols.add(symbol)
            if (
                row.get("status") in open_statuses
            ):
                open_rows[symbol] = row

        for position in self.positions.local_positions():
            if position.source in {TradingMode.PAPER_TRADING.value, "PAPER_TRADING_DB"} and position.symbol not in open_rows:
                self.positions.clear_local_position(position.symbol)

        for symbol, row in open_rows.items():
            try:
                metadata = _trade_metadata(row)
                self.positions.set_local_position(
                    Position(
                        symbol=symbol,
                        direction=Direction(str(row["direction"])),
                        quantity=Decimal(str(row["quantity"])),
                        entry_price=Decimal(str(row["entry_price"])),
                        stop_loss=_optional_decimal(row.get("stop_loss")),
                        take_profit=_optional_decimal(row.get("take_profit")),
                        managed_by_bot=True,
                        source="PAPER_TRADING_DB",
                        leverage=_optional_int(metadata.get("leverage")),
                        initial_margin=_optional_decimal(metadata.get("initial_margin")),
                    )
                )
            except Exception:
                logger.exception("Could not restore paper position %s from database row.", symbol)

    async def _sync_live_positions_from_exchange(self, required: bool = False) -> None:
        if not self.config.is_live:
            return
        try:
            raw_positions = await self.binance.position_risk()
            open_orders = await self.binance.open_orders()
        except Exception:
            logger.exception("Could not sync live positions from Binance.")
            self.disaster.record_api_failure()
            if required:
                raise
            return

        orders_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for order in open_orders:
            symbol = str(order.get("symbol", ""))
            if symbol:
                orders_by_symbol[symbol].append(order)

        open_symbols: set[str] = set()
        for item in raw_positions:
            amount = to_decimal(item.get("positionAmt", "0"))
            if amount == 0:
                continue
            symbol = str(item["symbol"])
            open_symbols.add(symbol)
            direction = Direction.LONG if amount > 0 else Direction.SHORT
            symbol_orders = orders_by_symbol.get(symbol, [])
            stop_loss, take_profit = _protection_prices(symbol_orders)
            recovery_evidence = restart_recovery_evidence(item, symbol_orders)
            managed_by_bot = bool(recovery_evidence["managed_by_bot"])
            entry_price = to_decimal(item.get("entryPrice", "0"))
            mark_price = to_decimal(item.get("markPrice", "0"))
            liquidation = item.get("liquidationPrice")
            self.positions.set_local_position(
                Position(
                    symbol=symbol,
                    direction=direction,
                    quantity=abs(amount),
                    entry_price=entry_price,
                    mark_price=mark_price,
                    liquidation_price=to_decimal(liquidation) if liquidation not in (None, "", "0") else None,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    managed_by_bot=managed_by_bot,
                    unrealized_pnl=to_decimal(item.get("unRealizedProfit", "0")),
                    source="BINANCE_RECONCILIATION",
                    leverage=_optional_int(item.get("leverage")),
                    initial_margin=_optional_decimal(item.get("positionInitialMargin")),
                )
            )
            await self.db.sync_live_position(
                symbol=symbol,
                direction=direction.value,
                quantity=str(abs(amount)),
                entry_price=str(entry_price),
                stop_loss=str(stop_loss) if stop_loss is not None else None,
                take_profit=str(take_profit) if take_profit is not None else None,
                mode=self.config.mode.value,
                status="OPEN",
                metadata={
                    "source": "BINANCE_RECONCILIATION",
                    "managed_by_bot": managed_by_bot,
                    "restart_recovery": recovery_evidence,
                    "raw_position": item,
                    "open_orders": symbol_orders,
                },
            )
            if not recovery_evidence["protected"]:
                await self.telegram.risk_warning(
                    f"{symbol}: restart recovery found active position without verified protective SL/TP."
                )

        await self.db.close_absent_live_positions(open_symbols, self.config.mode.value)
        for position in self.positions.local_positions():
            if position.source == "BINANCE_RECONCILIATION" and position.symbol not in open_symbols:
                self.positions.clear_local_position(position.symbol)
        self.disaster.record_api_success()

    async def _btc_4h_change(self) -> Decimal | None:
        if not self.config.market_filters.btc_4h_drop_filter_enabled:
            return None
        try:
            candles = await self.market_data.candles("BTCUSDT", "4h", limit=3)
        except Exception:
            logger.exception("Could not fetch BTCUSDT 4h candles for market filter.")
            return None
        return self.entry_filter.btc_4h_change(candles)

    async def _check_operational_incidents(self) -> None:
        if self.binance.recent_rate_limit_count(300) >= 3:
            await self.incidents.send(
                "binance_rate_limits",
                "Repeated Binance rate-limit/backoff events in the last 5 minutes.",
                self.telegram.risk_warning,
            )
        if self.config.account.initial_equity_usdt and self.risk.state.realized_pnl_today < 0:
            daily_loss_pct = abs(self.risk.state.realized_pnl_today) / self.config.account.initial_equity_usdt
            if daily_loss_pct >= self.config.risk.max_daily_loss_pct * Decimal("0.5"):
                await self.incidents.send(
                    "drawdown_pressure",
                    f"Daily realized drawdown reached {daily_loss_pct:.2%}; approaching risk limit.",
                    self.telegram.risk_warning,
                )

    async def _probe_disaster_recovery(self) -> bool:
        """Record a fresh Binance health result while entries are blocked.

        The detector's API failure counter is process-local. Without this probe,
        a critical API outage blocks the code path that normally records a
        successful request, so automatic recovery can never begin.
        """
        try:
            await self.binance.ping()
        except Exception as exc:
            self.disaster.record_api_failure()
            logger.warning("Disaster recovery API probe failed; entry block remains: %s", exc)
            return False

        self.disaster.record_api_success()
        logger.info("Disaster recovery API probe succeeded; re-evaluating safeguards.")
        return True

    async def _refresh_realtime_correlation(self, symbol: str, active_positions: list[Position]) -> str | None:
        if not self.config.risk.realtime_correlation_enabled or not active_positions:
            return None
        symbols = sorted({position.symbol for position in active_positions if position.symbol != symbol})
        if not symbols:
            return None
        failures: list[str] = []
        for active_symbol in symbols:
            try:
                candles = await self.market_data.candles(
                    active_symbol,
                    "1h",
                    limit=self.config.risk.realtime_correlation_lookback + 1,
                )
                self.corr_filter.update(active_symbol, candles)
            except Exception:
                logger.exception("Could not refresh realtime correlation for %s.", active_symbol)
                failures.append(active_symbol)
        if failures and self.config.is_live and self.config.risk.block_live_when_correlation_unavailable:
            return (
                "Realtime correlation check unavailable for active positions "
                f"{', '.join(failures)}; live entry blocked."
            )
        return None

    async def _mark_trade_closed(self, symbol: str, realized_pnl: Decimal) -> None:
        await self.db.mark_latest_trade_closed(symbol, str(realized_pnl))
        self.risk.record_closed_trade(realized_pnl)
        try:
            await self._persist_risk_state()
        except Exception:
            logger.exception("Could not persist risk state.")
        await self.telegram.trade_closed(symbol, str(realized_pnl))
        # Обновляем disaster detector
        try:
            equity = await self.current_equity_usdt()
            if equity > 0 and realized_pnl < 0:
                self.disaster.record_loss(abs(realized_pnl) / equity)
            elif realized_pnl > 0:
                self.disaster.record_win()
        except Exception:
            pass

    def emergency_stop_active(self) -> bool:
        return Path(self.config.safety.emergency_stop_file).exists()

    def activate_emergency_stop(self) -> None:
        path = Path(self.config.safety.emergency_stop_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("emergency stop active\n", encoding="utf-8")
        self.risk.emergency_stop()

    async def status(self) -> dict:
        await self.db.connect()
        await self._restore_risk_state()
        await self._sync_paper_positions()
        try:
            return {
                "mode": self.config.mode.value,
                "emergency_stop": self.emergency_stop_active(),
                "active_positions": [
                    {
                        "symbol": position.symbol,
                        "direction": position.direction.value,
                        "quantity": str(position.quantity),
                        "entry_price": str(position.entry_price),
                        "mark_price": str(position.mark_price) if position.mark_price else None,
                        "liquidation_price": str(position.liquidation_price) if position.liquidation_price else None,
                        "unrealized_pnl": str(position.unrealized_pnl),
                        "source": position.source,
                    }
                    for position in await self.positions.active_positions()
                ],
                "pnl": await self.db.pnl_summary(self.config.mode.value),
            }
        finally:
            await self.db.close()

    async def _handle_emergency(self, message: str) -> None:
        logger.critical("EMERGENCY: %s", message)
        await self.telegram.risk_warning(f"EMERGENCY: {message}")
        await self._close_all_positions(f"emergency: {message}")
        self.activate_emergency_stop()

    async def _close_all_positions(self, reason: str) -> None:
        logger.critical("Закрываем все позиции: %s", reason)
        try:
            active = await self.positions.active_positions()
            for position in active:
                try:
                    await self.orders.close_position(position, reason="disaster_mode")
                    logger.info("Позиция %s закрыта", position.symbol)
                except Exception as exc:
                    logger.exception("Не удалось закрыть %s: %s", position.symbol, exc)
        except Exception as exc:
            logger.exception("Ошибка закрытия: %s", exc)


    async def _apply_live_emergency_stop(self) -> None:
        logger.critical("Applying live emergency-stop actions.")
        if self.config.safety.emergency_cancel_orders_in_live:
            try:
                open_orders = await self.binance.open_orders()
                for symbol in {str(order.get("symbol", "")) for order in open_orders if order.get("symbol")}:
                    try:
                        await self.binance.cancel_all_orders(symbol)
                        logger.critical("Canceled open live orders for %s due to emergency stop.", symbol)
                    except Exception:
                        logger.exception("Could not cancel live orders for %s during emergency stop.", symbol)
            except Exception:
                logger.exception("Could not fetch open live orders during emergency stop.")
        if self.config.safety.emergency_close_positions_in_live:
            await self._close_all_positions("manual emergency stop")
        else:
            active = await self.positions.active_positions()
            if active:
                await self.telegram.risk_warning(
                    "Emergency stop is active: open orders were cancelled, "
                    "but live positions were left open because emergency_close_positions_in_live=false."
                )


def _trades_since_stats_epoch(trades: list[dict[str, Any]], epoch: str | None) -> list[dict[str, Any]]:
    """Отсекает закрытые сделки старше эпохи статистики.

    Правила self-learning не должны выводиться из сделок, совершённых до
    изменения торговой логики: «плохие» сегменты старой конфигурации не
    характеризуют новую. Сделки, открытые до эпохи и закрытые уже после
    деплоя, тоже исключаются: иначе старые paper-позиции станут гибридными
    AFTER-сделками и загрязнят обучение.
    """
    if not epoch:
        return trades
    epoch_dt = _parse_datetime_utc(epoch)
    if epoch_dt is None:
        logger.warning("analytics.stats_epoch не разобран: %r — фильтр не применён.", epoch)
        return trades
    filtered: list[dict[str, Any]] = []
    for trade in trades:
        status = str(trade.get("status") or "").upper()
        created_at = _parse_datetime_utc(trade.get("created_at") or trade.get("opened_at"))
        closed_at = _parse_datetime_utc(trade.get("closed_at"))
        if created_at is not None and created_at < epoch_dt:
            continue
        if closed_at is not None and closed_at < epoch_dt:
            continue
        if status == "CLOSED" and created_at is None and closed_at is None:
            continue
        filtered.append(trade)
    return filtered


def _symbol_4h_change_pct(candles_4h: list, lookback_bars: int) -> Decimal | None:
    """Относительное изменение закрытия за lookback_bars 4h-свечей (доля, не %)."""
    if not candles_4h or lookback_bars <= 0 or len(candles_4h) <= lookback_bars:
        return None
    try:
        last = Decimal(str(candles_4h[-1].close))
        base = Decimal(str(candles_4h[-1 - lookback_bars].close))
    except Exception:
        return None
    if base <= 0:
        return None
    return (last - base) / base


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    return Decimal(str(value))


def _optional_int(value: object) -> int | None:
    if value in (None, "", "None", "0"):
        return None
    try:
        parsed = int(str(value))
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _decimal_or_zero(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _protection_prices(orders: list[dict[str, Any]]) -> tuple[Decimal | None, Decimal | None]:
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    for order in orders:
        order_type = str(order.get("type", ""))
        stop_price = order.get("stopPrice")
        if stop_price in (None, "", "0"):
            continue
        if order_type in {"STOP", "STOP_MARKET", "TRAILING_STOP_MARKET"} and stop_loss is None:
            stop_loss = _optional_decimal(stop_price)
        elif order_type in {"TAKE_PROFIT", "TAKE_PROFIT_MARKET"} and take_profit is None:
            take_profit = _optional_decimal(stop_price)
    return stop_loss, take_profit


def _signal_strategy(signal: Signal) -> str:
    metadata = signal.metadata or {}
    return str(metadata.get("strategy") or "UNKNOWN")


def _risk_plan_exit_metadata(plan: Any) -> dict[str, Any]:
    return {
        "partial_take_profits": [
            {
                "name": target.name,
                "price": str(target.price),
                "quantity": str(target.quantity),
                "fraction": str(target.fraction),
                "reward_risk": str(target.reward_risk),
                "move_stop_to_breakeven": target.move_stop_to_breakeven,
                "activate_trailing": target.activate_trailing,
            }
            for target in getattr(plan, "partial_take_profits", ())
        ],
        "protection": (
            {
                "initial_stop": str(plan.protection.initial_stop),
                "breakeven_price": str(plan.protection.breakeven_price),
                "breakeven_after_target": plan.protection.breakeven_after_target,
                "trailing_enabled": plan.protection.trailing_enabled,
                "trailing_activation_reward_risk": str(plan.protection.trailing_activation_reward_risk),
                "trailing_callback_rate_pct": str(plan.protection.trailing_callback_rate_pct),
            }
            if getattr(plan, "protection", None)
            else None
        ),
        "filled_partial_targets": [],
    }


def _plan_with_paper_entry_fill(plan: Any, result: Any, mode: TradingMode) -> Any:
    if mode != TradingMode.PAPER_TRADING:
        return plan
    metadata = getattr(result, "execution_metadata", {}) or {}
    raw_entry = metadata.get("averageFillPrice") or metadata.get("effectiveEntryPrice")
    if raw_entry in (None, "", "None"):
        return plan
    try:
        entry_price = Decimal(str(raw_entry))
    except Exception:
        return plan
    if entry_price <= 0:
        return plan
    notional = entry_price * plan.quantity
    initial_margin = notional / Decimal(plan.leverage) if getattr(plan, "leverage", 0) else plan.initial_margin
    return replace(plan, entry_price=entry_price, notional=notional, initial_margin=initial_margin)


ACTIVE_TRADE_STATUSES = {"ACCEPTED", "OPEN", "ACTIVE"}


def _is_open_paper_trade(trade: dict[str, Any]) -> bool:
    return (
        str(trade.get("mode") or "") == TradingMode.PAPER_TRADING.value
        and _trade_status(trade) in ACTIVE_TRADE_STATUSES
    )


def _paper_trade_unrealized_pnl(trade: dict[str, Any], mark_price: Decimal) -> Decimal:
    if mark_price <= 0:
        return Decimal("0")
    try:
        direction = Direction(str(trade.get("direction") or Direction.NONE.value))
    except ValueError:
        return Decimal("0")
    quantity = _decimal_or_zero(trade.get("quantity"))
    entry_price = _decimal_or_zero(trade.get("entry_price"))
    if quantity <= 0 or entry_price <= 0:
        return Decimal("0")
    if direction == Direction.LONG:
        return (mark_price - entry_price) * quantity
    if direction == Direction.SHORT:
        return (entry_price - mark_price) * quantity
    return Decimal("0")


def _trade_metadata(trade: dict[str, Any]) -> dict[str, Any]:
    raw = trade.get("metadata") or {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _trade_strategy(trade: dict[str, Any]) -> str:
    metadata = _trade_metadata(trade)
    signal_metadata = metadata.get("signal_metadata")
    if isinstance(signal_metadata, dict) and signal_metadata.get("strategy"):
        return str(signal_metadata["strategy"])
    return str(metadata.get("strategy") or "UNKNOWN")


def _trade_status(trade: dict[str, Any]) -> str:
    return str(trade.get("status") or "").upper()


def _same_symbol_strategy(signal: Signal, trade: dict[str, Any]) -> bool:
    return trade.get("symbol") == signal.symbol and _trade_strategy(trade) == _signal_strategy(signal)


MR_ORDER_FLOW_AGAINST_FLAGS = {
    "taker_flow_against",
    "book_imbalance_against",
    "aggressive_delta_against",
    "liquidity_sweep_against",
    "absorption_against",
    "structure_break_against",
    "crowded_long_funding",
    "crowded_short_funding",
}
MR_ORDER_FLOW_SEVERE_FLAGS = {
    "liquidation_cascade",
    "adverse_liquidity_nearby",
}

STRATEGY_LOGIC_VERSIONS = {
    "SQUEEZE_BREAKOUT": "sqz_structure_break_gate_v1",
    "SQUEEZE_BREAKOUT_OF_MEASURE": "sqz_of_measure_weak_mixed_v1",
    "SQZ_STRICT_CONTROL_SHADOW": "sqz_gate_cohort_v1",
    "SQZ_OF_AGAINST_SHADOW": "sqz_gate_cohort_v1",
    "SQZ_OF_HOSTILE_SHADOW": "sqz_gate_cohort_v1",
    "SQZ_OF_ABSORPTION_SHADOW": "sqz_gate_cohort_v1",
    "SQZ_RS_NEUTRAL_SHADOW": "sqz_gate_cohort_v1",
    "SQZ_NO_RETEST_SHADOW": "sqz_gate_cohort_v1",
    "SQUEEZE_BREAKOUT_DYNAMIC": "sqz_dyn_of_retest_v2",
    "SQUEEZE_BREAKOUT_DYNAMIC_NEUTRAL_RS": "sqz_dyn_neutral_rs_shadow_v1",
    "SQUEEZE_BREAKOUT_DYNAMIC_UPD": "sqz_dyn_upd_confirmed_release_v2",
    "TREND_PULLBACK": "tpb_profitable_bucket_v3",
    "LIQUIDITY_SWEEP_REVERSAL": "lsr_research_gate_v2",
    "VWAP_REVERSION": "vwr_research_gate_v2",
    "VWAP_REVERSION_WATCH": "vwrw_research_gate_v2",
    "RANGE_GRID": "grid_research_gate_v2",
    "MOMENTUM_CONTINUATION": "mom_rs_target_space_v2",
    "TREND_FOLLOWING": "trend_following_edge_research_v5",
}

SHADOW_PAPER_DIAGNOSTIC_ONLY_STRATEGIES = {
    "MOMENTUM_CONTINUATION": "negative 30d expectancy; keep signals/diagnostics only until retuned",
    "RANGE_GRID": "negative 30d expectancy and latest post-fix shadow loss; keep diagnostics only",
    "SQUEEZE_BREAKOUT_DYNAMIC_UPD": "challenger underperformed base SQZ-DYN; keep diagnostics only until retuned",
    "TREND_PULLBACK": "negative 30d expectancy; keep signals/diagnostics only until retuned",
    "VWAP_REVERSION_WATCH": "watch variant is unstable; use stricter VWAP_REVERSION for edge discovery",
}


SHADOW_REVALIDATION_BUCKETS = {
    "MOMENTUM_CONTINUATION": "MOM_REVALIDATION",
    "RANGE_GRID": "GRID_REVALIDATION",
    "SQUEEZE_BREAKOUT_DYNAMIC_UPD": "SQZ_DYN_UPD_REVALIDATION",
    "TREND_PULLBACK": "TPB_REVALIDATION",
    "VWAP_REVERSION_WATCH": "VWR_W_REVALIDATION",
}


def _strategy_logic_version(strategy: str) -> str | None:
    return STRATEGY_LOGIC_VERSIONS.get(str(strategy or "").upper())


def _shadow_paper_diagnostic_only_reason(
    signal: Signal,
    config: StrategyConfig | None = None,
) -> str | None:
    strategy = _signal_strategy(signal)
    revalidation = set(config.shadow_revalidation_strategies) if config else set()
    if config and config.shadow_revalidation_enabled and strategy in revalidation:
        return None
    reason = SHADOW_PAPER_DIAGNOSTIC_ONLY_STRATEGIES.get(strategy)
    if not reason:
        return None
    return f"{strategy} shadow-paper disabled: {reason}."


def _controlled_shadow_revalidation_override(signal: Signal, config: StrategyConfig) -> Signal:
    """Route a configured diagnostic candidate into an isolated virtual cohort.

    The source strategy remains intact for its own safety/context checks. Only
    the stored shadow-trade bucket changes, so this cannot admit a paper order
    or mix a fresh cohort with the historical candidate sample.
    """
    strategy = _signal_strategy(signal)
    if (
        not config.shadow_revalidation_enabled
        or strategy not in set(config.shadow_revalidation_strategies)
        or strategy not in SHADOW_PAPER_DIAGNOSTIC_ONLY_STRATEGIES
    ):
        return signal
    metadata = signal.metadata or {}
    existing = metadata.get("controlled_shadow") if isinstance(metadata, dict) else None
    if isinstance(existing, dict) and existing.get("revalidation"):
        return signal
    bucket = SHADOW_REVALIDATION_BUCKETS.get(strategy, f"{strategy}_REVALIDATION")
    source_version = _strategy_logic_version(strategy) or "unknown"
    return replace(
        signal,
        metadata={
            **dict(metadata),
            "strategy_logic_version": f"{source_version}:revalidation:{config.shadow_revalidation_cohort}",
            "controlled_shadow": {
                "strategy_bucket": bucket,
                "risk_cap_pct": str(config.shadow_revalidation_risk_cap_pct),
                "revalidation": True,
                "cohort": config.shadow_revalidation_cohort,
                "source_strategy": strategy,
            },
        },
    )


def _is_shadow_revalidation_signal(signal: Signal) -> bool:
    metadata = signal.metadata or {}
    payload = metadata.get("controlled_shadow") if isinstance(metadata, dict) else None
    return isinstance(payload, dict) and bool(payload.get("revalidation"))


SHADOW_GATE_COUNTERFACTUAL_SOURCE_PREFIX = {
    "SQUEEZE_BREAKOUT_DYNAMIC_UPD": "SQZ_DYN_UPD_CLEAN",
    "TREND_PULLBACK": "TPB_CLEAN",
    "MOMENTUM_CONTINUATION": "MOM_CLEAN",
}
SHADOW_GATE_COUNTERFACTUAL_SOURCE_GATES = {
    "SQUEEZE_BREAKOUT_DYNAMIC_UPD": {
        "RS_NEUTRAL",
        "RS_AGAINST",
        "MISSING_OI",
        "NO_RETEST",
        "NEAR_LIQUIDITY",
    },
    "TREND_PULLBACK": {"RS_NEUTRAL", "RS_AGAINST", "NEAR_LIQUIDITY"},
    "MOMENTUM_CONTINUATION": {"RS_NEUTRAL", "RS_AGAINST", "NEAR_LIQUIDITY"},
}


def _shadow_gate_source_cluster_id(signal: Signal) -> str:
    metadata = signal.metadata or {}
    source = _signal_strategy(signal)
    close_time = metadata.get("signal_bar_close_time")
    if close_time not in (None, ""):
        return f"{source}:{signal.symbol}:{signal.direction.value}:{close_time}"
    return f"{source}:{signal.symbol}:{signal.direction.value}:{signal.entry_price}:{signal.stop_loss}"


def _shadow_gate_counterfactual_applies(signal: Signal, gate: str) -> bool:
    source = _signal_strategy(signal)
    gate = str(gate or "").upper()
    if gate not in SHADOW_GATE_COUNTERFACTUAL_SOURCE_GATES.get(source, set()):
        return False
    if gate == "RS_NEUTRAL":
        return _relative_strength_alignment(signal) == "neutral"
    if gate == "RS_AGAINST":
        return _relative_strength_alignment(signal) == "against"

    metadata = signal.metadata or {}
    order_flow = _order_flow_metadata(signal)
    if gate == "MISSING_OI":
        oi_change = _optional_decimal(order_flow.get("open_interest_change_pct"))
        if oi_change is None:
            oi_change = _optional_decimal(metadata.get("open_interest_change_pct"))
        return oi_change is None
    if gate == "NO_RETEST":
        return not bool(metadata.get("squeeze_retest_confirmed"))
    if gate == "NEAR_LIQUIDITY":
        target_distance = _target_liquidity_distance_bps(signal, order_flow)
        if target_distance is None:
            return False
        if source == "SQUEEZE_BREAKOUT_DYNAMIC_UPD":
            return not bool(metadata.get("squeeze_retest_confirmed")) and target_distance < Decimal("20")
        return target_distance < Decimal("12")
    return False


def _normalized_relaxed_gates(
    relaxed_gate: str | Collection[str] | None,
) -> frozenset[str]:
    if relaxed_gate is None:
        return frozenset()
    values = [relaxed_gate] if isinstance(relaxed_gate, str) else relaxed_gate
    return frozenset(str(value).strip().upper() for value in values if str(value).strip())


def _shadow_gate_is_relaxed(
    signal: Signal,
    relaxed_gate: str | Collection[str] | None,
    gate: str,
) -> bool:
    del signal
    return str(gate).upper() in _normalized_relaxed_gates(relaxed_gate)


def _shadow_relative_strength_gate_is_relaxed(
    signal: Signal,
    relaxed_gate: str | Collection[str] | None,
) -> bool:
    alignment = _relative_strength_alignment(signal)
    gate = "RS_NEUTRAL" if alignment == "neutral" else "RS_AGAINST" if alignment == "against" else ""
    return bool(gate and _shadow_gate_is_relaxed(signal, relaxed_gate, gate))


def _shadow_gate_counterfactual_variants(
    signal: Signal,
    config: StrategyConfig,
) -> tuple[list[Signal], str | None, list[str]]:
    """Build virtual entries where exactly one configured context gate is relaxed.

    The source shadow stream may observe rather than enforce order-flow, but a
    one-gate counterfactual must always retain every other strict gate.
    """
    source = _signal_strategy(signal)
    if (
        not config.shadow_gate_counterfactual_enabled
        or source not in SHADOW_GATE_COUNTERFACTUAL_SOURCE_GATES
    ):
        return [], None, []

    enabled_gates = {
        str(gate).strip().upper()
        for gate in config.shadow_gate_counterfactual_gates
        if str(gate).strip()
    }
    evaluated_gates = [
        gate
        for gate in sorted(enabled_gates)
        if _shadow_gate_counterfactual_applies(signal, gate)
    ]
    if not evaluated_gates:
        return [], None, []

    strict_reason = _shadow_candidate_context_rejection_reason(
        signal,
        enforce_order_flow=True,
    )
    if strict_reason is None:
        return [], None, []

    source_cluster_id = _shadow_gate_source_cluster_id(signal)
    source_prefix = SHADOW_GATE_COUNTERFACTUAL_SOURCE_PREFIX[source]
    source_version = _strategy_logic_version(source) or "unknown"
    variants: list[Signal] = []
    for gate in evaluated_gates:
        remaining_rejection = _shadow_candidate_context_rejection_reason(
            signal,
            enforce_order_flow=True,
            relaxed_gate=gate,
        )
        if remaining_rejection is not None:
            continue
        strategy_bucket = f"{source_prefix}_{gate}_SHADOW"
        metadata = dict(signal.metadata or {})
        metadata.pop("controlled_shadow", None)
        measurement_shadow = {
            "bucket": "shadow_gate_counterfactual_v1",
            "strategy_bucket": strategy_bucket,
            "source_strategy": source,
            "source_cluster_id": source_cluster_id,
            "relaxed_gate": gate,
            "strict_rejection_reason": strict_reason,
            "gate_vector": [gate],
            "cohort": config.shadow_gate_counterfactual_cohort,
            "risk_cap_pct": str(config.shadow_gate_counterfactual_risk_cap_pct),
            "execution_constraints": [
                "exactly_one_context_gate_relaxed",
                "virtual_shadow_only",
                "source_exit_profile_preserved",
                "source_cluster_deduplicated",
            ],
        }
        variants.append(
            replace(
                signal,
                metadata={
                    **metadata,
                    "strategy": strategy_bucket,
                    "strategy_mode": "shadow",
                    "strategy_source": source,
                    "exit_profile_strategy": source,
                    "measurement_shadow": measurement_shadow,
                    "strategy_logic_version": (
                        f"{source_version}:gate_counterfactual:"
                        f"{config.shadow_gate_counterfactual_cohort}:{gate}"
                    ),
                },
            )
        )
    return variants, strict_reason, evaluated_gates


SHADOW_PARALLEL_LAB_SOURCE_PREFIX = {
    "SQUEEZE_BREAKOUT_DYNAMIC_UPD": "SQZ_UPD_LAB",
    "TREND_PULLBACK": "TPB_LAB",
    "MOMENTUM_CONTINUATION": "MOM_LAB",
}
SHADOW_PARALLEL_LAB_SOURCE_GATES = {
    "SQUEEZE_BREAKOUT_DYNAMIC_UPD": {
        "OF_AGAINST",
        "OF_HOSTILE",
        "OF_WEAK",
        "OF_ABSORPTION",
        "RS_NEUTRAL",
        "RS_AGAINST",
        "MISSING_OI",
        "NO_RETEST",
        "NEAR_LIQUIDITY",
    },
    "TREND_PULLBACK": {
        "OF_AGAINST",
        "OF_HOSTILE",
        "OF_WEAK",
        "RS_NEUTRAL",
        "RS_AGAINST",
        "NEAR_LIQUIDITY",
    },
    "MOMENTUM_CONTINUATION": {
        "OF_AGAINST",
        "OF_HOSTILE",
        "OF_WEAK",
        "OF_ABSORPTION",
        "RS_NEUTRAL",
        "RS_AGAINST",
        "NEAR_LIQUIDITY",
    },
}


def _shadow_parallel_lab_gate_applies(signal: Signal, gate: str) -> bool:
    source = _signal_strategy(signal)
    gate = str(gate).upper()
    if gate not in SHADOW_PARALLEL_LAB_SOURCE_GATES.get(source, set()):
        return False
    if gate in {"RS_NEUTRAL", "RS_AGAINST", "MISSING_OI", "NO_RETEST", "NEAR_LIQUIDITY"}:
        return _shadow_gate_counterfactual_applies(signal, gate)

    order_flow = _order_flow_metadata(signal)
    alignment = str(order_flow.get("alignment") or "mixed")
    score = _optional_decimal(order_flow.get("score")) or Decimal("0")
    risk_flags = {str(flag) for flag in order_flow.get("risk_flags") or []}
    if gate == "OF_AGAINST":
        return alignment == "against"
    if gate == "OF_ABSORPTION":
        return "absorption_against" in risk_flags
    if gate == "OF_HOSTILE":
        hostile_flags = {
            "adverse_liquidity_nearby",
            "taker_flow_against",
            "book_imbalance_against",
            "aggressive_delta_against",
            "structure_break_against",
            "liquidation_cascade",
        }
        return bool(risk_flags.intersection(hostile_flags))
    if gate == "OF_WEAK":
        if source == "SQUEEZE_BREAKOUT_DYNAMIC_UPD":
            return (alignment == "mixed" and score < Decimal("0.50")) or (
                alignment == "aligned" and score < Decimal("0.55")
            )
        if source == "TREND_PULLBACK":
            floor = Decimal("0.68") if signal.direction == Direction.SHORT else Decimal("0.62")
            return alignment == "mixed" or score < floor
        if source == "MOMENTUM_CONTINUATION":
            return alignment == "mixed" or score < Decimal("0.78")
    return False


def _shadow_parallel_lab_variants(
    signal: Signal,
    config: StrategyConfig,
) -> tuple[list[Signal], str | None, list[str]]:
    """Build strict and fixed relaxed-policy variants from one unique candidate."""
    source = _signal_strategy(signal)
    if (
        not config.shadow_parallel_lab_enabled
        or source not in SHADOW_PARALLEL_LAB_SOURCE_GATES
    ):
        return [], None, []

    strict_reason = _shadow_candidate_context_rejection_reason(
        signal,
        enforce_order_flow=True,
    )
    source_cluster_id = _shadow_gate_source_cluster_id(signal)
    source_prefix = SHADOW_PARALLEL_LAB_SOURCE_PREFIX[source]
    source_version = _strategy_logic_version(source) or "unknown"
    observed_failed_gates = sorted(
        gate
        for gate in SHADOW_PARALLEL_LAB_SOURCE_GATES[source]
        if _shadow_parallel_lab_gate_applies(signal, gate)
    )
    variants: list[Signal] = []
    evaluated_arms: list[str] = []
    seen_arms: set[str] = set()
    for configured_arm in config.shadow_parallel_lab_arms:
        arm = "+".join(
            part.strip().upper()
            for part in str(configured_arm).split("+")
            if part.strip()
        )
        if not arm or arm in seen_arms:
            continue
        seen_arms.add(arm)
        if arm == "STRICT":
            evaluated_arms.append(arm)
            relaxed_gates = frozenset()
            if strict_reason is not None:
                continue
        else:
            relaxed_gates = frozenset(arm.split("+"))
            if not relaxed_gates.issubset(SHADOW_PARALLEL_LAB_SOURCE_GATES[source]):
                continue
            if not all(_shadow_parallel_lab_gate_applies(signal, gate) for gate in relaxed_gates):
                continue
            evaluated_arms.append(arm)
            remaining_rejection = _shadow_candidate_context_rejection_reason(
                signal,
                enforce_order_flow=True,
                relaxed_gate=relaxed_gates,
            )
            if remaining_rejection is not None:
                continue

        arm_token = arm.replace("+", "_")
        strategy_bucket = f"{source_prefix}_{arm_token}_SHADOW"
        metadata = dict(signal.metadata or {})
        metadata.pop("controlled_shadow", None)
        measurement_shadow = {
            "bucket": "parallel_shadow_lab_v1",
            "strategy_bucket": strategy_bucket,
            "source_strategy": source,
            "source_cluster_id": source_cluster_id,
            "policy_arm": arm,
            "relaxed_gate": arm if arm != "STRICT" else "NONE_STRICT_CONTROL",
            "relaxed_gates": sorted(relaxed_gates),
            "observed_failed_gates": observed_failed_gates,
            "strict_rejection_reason": strict_reason,
            "cohort": config.shadow_parallel_lab_cohort,
            "risk_cap_pct": str(config.shadow_parallel_lab_risk_cap_pct),
            "execution_constraints": [
                "pre_context_candidate",
                "fixed_policy_matrix",
                "identical_source_exit_profile",
                "virtual_shadow_only",
                "source_cluster_deduplicated",
                "no_portfolio_capacity_competition",
            ],
        }
        variants.append(
            replace(
                signal,
                metadata={
                    **metadata,
                    "strategy": strategy_bucket,
                    "strategy_mode": "shadow",
                    "strategy_source": source,
                    "exit_profile_strategy": source,
                    "measurement_shadow": measurement_shadow,
                    "strategy_logic_version": (
                        f"{source_version}:parallel_lab:"
                        f"{config.shadow_parallel_lab_cohort}:{arm}"
                    ),
                },
            )
        )
    return variants, strict_reason, evaluated_arms


SHADOW_CONDITIONAL_LAB_SOURCE_PREFIX = {
    "SQUEEZE_BREAKOUT_DYNAMIC_UPD": "SQZ_UPD",
    "TREND_PULLBACK": "TPB",
    "MOMENTUM_CONTINUATION": "MOM",
}
SHADOW_CONDITIONAL_HOSTILE_FLAGS = {
    "adverse_liquidity_nearby",
    "taker_flow_against",
    "book_imbalance_against",
    "aggressive_delta_against",
    "structure_break_against",
    "liquidation_cascade",
    "absorption_against",
}


def _conditional_regime_component(
    source: str,
    direction: Direction,
    regime: str,
) -> int:
    matching = "TREND_UP" if direction == Direction.LONG else "TREND_DOWN"
    opposite = "TREND_DOWN" if direction == Direction.LONG else "TREND_UP"
    if source == "SQUEEZE_BREAKOUT_DYNAMIC_UPD":
        if regime == "MOMENTUM":
            return 8
        if regime == matching:
            return 6
        if regime == opposite:
            return -10
        if regime == "RANGE":
            return 2
        return -2 if regime == "HIGH_VOLATILITY" else 0
    if regime == "MOMENTUM":
        return 8
    if regime == matching:
        return 10
    if regime == opposite:
        return -16
    if regime == "RANGE":
        return -10
    return -4 if regime == "HIGH_VOLATILITY" else 0


def _conditional_confirmation(
    signal: Signal,
    source: str,
) -> tuple[str, int]:
    metadata = signal.metadata or {}
    if source == "SQUEEZE_BREAKOUT_DYNAMIC_UPD":
        confirmed = bool(metadata.get("squeeze_retest_confirmed"))
        return ("RETEST", 10) if confirmed else ("NO_RETEST", -8)
    if source == "TREND_PULLBACK":
        depth = _optional_decimal(metadata.get("pullback_depth_atr"))
        if depth is None:
            return "PB_UNKNOWN", -3
        if Decimal("0.65") <= depth <= Decimal("1.95"):
            return "PB_VALID", 6
        return ("PB_SHALLOW", -8) if depth < Decimal("0.65") else ("PB_EXTENDED", -8)
    extension = _optional_decimal(metadata.get("breakout_extension_atr"))
    if extension is None:
        return "EXT_UNKNOWN", -3
    if extension >= Decimal("0.30"):
        return "EXT_STRONG", 6
    if extension >= Decimal("0.15"):
        return "EXT_VALID", 2
    return "EXT_WEAK", -6


def _shadow_conditional_profile(
    signal: Signal,
    config: StrategyConfig,
) -> dict[str, Any] | None:
    """Return a transparent research score, never a production admission decision."""
    source = _signal_strategy(signal)
    if source not in SHADOW_CONDITIONAL_LAB_SOURCE_PREFIX:
        return None

    metadata = signal.metadata or {}
    order_flow = _order_flow_metadata(signal)
    alignment = str(order_flow.get("alignment") or "missing").lower()
    flow_score = _optional_decimal(order_flow.get("score"))
    risk_flags = {str(flag) for flag in order_flow.get("risk_flags") or []}
    rs_alignment = _relative_strength_alignment(signal) or "missing"
    rs_score = _relative_strength_score(signal)
    regime = str(metadata.get("regime") or "UNKNOWN").upper()
    components: dict[str, int] = {}

    components["regime"] = _conditional_regime_component(source, signal.direction, regime)
    components["order_flow_alignment"] = {
        "aligned": 14,
        "mixed": -2,
        "against": -18,
    }.get(alignment, -6)
    if flow_score is None:
        components["order_flow_score"] = -4
        flow_band = "MISSING"
    elif flow_score >= Decimal("0.75"):
        components["order_flow_score"] = 8
        flow_band = "HIGH"
    elif flow_score >= Decimal("0.60"):
        components["order_flow_score"] = 4
        flow_band = "OK"
    elif flow_score < Decimal("0.40"):
        components["order_flow_score"] = -8
        flow_band = "LOW"
    else:
        components["order_flow_score"] = 0
        flow_band = "MID"
    hostile_count = len(risk_flags.intersection(SHADOW_CONDITIONAL_HOSTILE_FLAGS))
    components["hostile_flags"] = -min(18, hostile_count * 6)

    components["relative_strength_alignment"] = {
        "aligned": 12,
        "neutral": -2,
        "against": -16,
    }.get(rs_alignment, -6)
    if rs_score is None:
        components["relative_strength_score"] = -3
        rs_band = "MISSING"
    elif rs_score >= Decimal("0.75"):
        components["relative_strength_score"] = 5
        rs_band = "HIGH"
    elif rs_score >= Decimal("0.60"):
        components["relative_strength_score"] = 2
        rs_band = "OK"
    elif rs_score < Decimal("0.45"):
        components["relative_strength_score"] = -5
        rs_band = "LOW"
    else:
        components["relative_strength_score"] = 0
        rs_band = "MID"

    oi_change = _optional_decimal(order_flow.get("open_interest_change_pct"))
    if oi_change is None:
        oi_change = _optional_decimal(metadata.get("open_interest_change_pct"))
    if oi_change is None:
        oi_state = "MISSING"
        components["open_interest"] = -4
    elif oi_change > 0:
        oi_state = "RISING"
        components["open_interest"] = 5
    elif oi_change < 0:
        oi_state = "FALLING"
        components["open_interest"] = -5
    else:
        oi_state = "FLAT"
        components["open_interest"] = -1

    confirmation, confirmation_points = _conditional_confirmation(signal, source)
    components["confirmation"] = confirmation_points
    target_distance = _target_liquidity_distance_bps(signal, order_flow)
    if "adverse_liquidity_nearby" in risk_flags:
        liquidity_state = "ADVERSE"
        components["target_liquidity"] = -10
    elif target_distance is None:
        liquidity_state = "UNKNOWN"
        components["target_liquidity"] = 0
    elif target_distance < Decimal("12"):
        liquidity_state = "VERY_NEAR"
        components["target_liquidity"] = -8
    elif target_distance < Decimal("20"):
        liquidity_state = "NEAR"
        components["target_liquidity"] = -3
    else:
        liquidity_state = "CLEAR"
        components["target_liquidity"] = 3

    volume_ratio = _optional_decimal(metadata.get("volume_ratio"))
    if volume_ratio is None:
        volume_band = "UNKNOWN"
        components["volume"] = 0
    elif volume_ratio >= Decimal("1.50"):
        volume_band = "HIGH"
        components["volume"] = 5
    elif volume_ratio >= Decimal("1.20"):
        volume_band = "OK"
        components["volume"] = 2
    elif volume_ratio < Decimal("1.00"):
        volume_band = "LOW"
        components["volume"] = -4
    else:
        volume_band = "MID"
        components["volume"] = 0

    raw_score = Decimal("50") + Decimal(sum(components.values()))
    score = max(Decimal("0"), min(Decimal("100"), raw_score))
    if score >= config.shadow_conditional_lab_high_score:
        bucket = "HIGH"
    elif score >= config.shadow_conditional_lab_mid_score:
        bucket = "MID"
    else:
        bucket = "LOW"
    cell_traits = {
        "regime": regime,
        "direction": signal.direction.value,
        "of_alignment": alignment.upper(),
        "of_band": flow_band,
        "rs_alignment": rs_alignment.upper(),
        "rs_band": rs_band,
        "oi": oi_state,
        "confirmation": confirmation,
        "liquidity": liquidity_state,
        "volume": volume_band,
    }
    cell = "|".join(f"{key}={value}" for key, value in cell_traits.items())
    return {
        "score_version": "conditional_context_v1",
        "source_strategy": source,
        "source_cluster_id": _shadow_gate_source_cluster_id(signal),
        "score": str(score),
        "raw_score": str(raw_score),
        "bucket": bucket,
        "cell": cell,
        "traits": cell_traits,
        "components": components,
        "risk_flags": sorted(risk_flags),
        "strict_rejection_reason": _shadow_candidate_context_rejection_reason(
            signal,
            enforce_order_flow=True,
        ),
    }


def _shadow_conditional_lab_variant(
    signal: Signal,
    config: StrategyConfig,
) -> tuple[Signal | None, dict[str, Any] | None]:
    if not config.shadow_conditional_lab_enabled:
        return None, None
    profile = _shadow_conditional_profile(signal, config)
    if profile is None:
        return None, None
    source = str(profile["source_strategy"])
    bucket = str(profile["bucket"])
    strategy_bucket = f"{SHADOW_CONDITIONAL_LAB_SOURCE_PREFIX[source]}_COND_{bucket}_SHADOW"
    metadata = dict(signal.metadata or {})
    metadata.pop("controlled_shadow", None)
    measurement_shadow = {
        "bucket": "conditional_shadow_lab_v1",
        "strategy_bucket": strategy_bucket,
        "source_strategy": source,
        "source_cluster_id": profile["source_cluster_id"],
        "policy_arm": f"CONDITIONAL_{bucket}",
        "cohort": config.shadow_conditional_lab_cohort,
        "risk_cap_pct": str(config.shadow_conditional_lab_risk_cap_pct),
        "conditional_profile": profile,
        "execution_constraints": [
            "pre_context_candidate",
            "one_non_overlapping_bucket_per_source_cluster",
            "identical_source_exit_profile",
            "virtual_shadow_only",
            "no_production_admission_authority",
        ],
    }
    source_version = _strategy_logic_version(source) or "unknown"
    return replace(
        signal,
        metadata={
            **metadata,
            "strategy": strategy_bucket,
            "strategy_mode": "shadow",
            "strategy_source": source,
            "exit_profile_strategy": source,
            "measurement_shadow": measurement_shadow,
            "conditional_profile": profile,
            "strategy_logic_version": (
                f"{source_version}:conditional_lab:"
                f"{config.shadow_conditional_lab_cohort}:{bucket}"
            ),
        },
    ), profile


def _shadow_conditional_profile_v2(
    signal: Signal,
    config: StrategyConfig,
) -> dict[str, Any] | None:
    """Return an isolated SQZ score recalibration for future OOS evidence.

    V1 remains untouched as the control. V2 reduces unsupported rewards for
    OF/retest, penalizes range entries, and gives relative-strength quality
    more weight. The score is research metadata, never an admission decision.
    """
    if _signal_strategy(signal) != "SQUEEZE_BREAKOUT_DYNAMIC_UPD":
        return None
    base = _shadow_conditional_profile(signal, config)
    if base is None:
        return None

    traits = dict(base.get("traits") or {})
    risk_flags = {str(flag) for flag in base.get("risk_flags") or []}
    direction = str(traits.get("direction") or "")
    regime = str(traits.get("regime") or "UNKNOWN")
    matching_regime = "TREND_UP" if direction == Direction.LONG.value else "TREND_DOWN"
    opposite_regime = "TREND_DOWN" if direction == Direction.LONG.value else "TREND_UP"
    if regime == "MOMENTUM":
        regime_points = 12
    elif regime == matching_regime:
        regime_points = 8
    elif regime == opposite_regime:
        regime_points = -16
    elif regime == "RANGE":
        regime_points = -8
    elif regime == "HIGH_VOLATILITY":
        regime_points = -4
    else:
        regime_points = 0

    components: dict[str, int] = {
        "regime": regime_points,
        "order_flow_alignment": {
            "ALIGNED": 4,
            "MIXED": 0,
            "AGAINST": -12,
        }.get(str(traits.get("of_alignment") or ""), -8),
        "order_flow_score": {
            "HIGH": 2,
            "OK": 1,
            "MID": 0,
            "LOW": -4,
        }.get(str(traits.get("of_band") or ""), -4),
        "relative_strength_alignment": {
            "ALIGNED": 6,
            "NEUTRAL": 0,
            "AGAINST": -20,
        }.get(str(traits.get("rs_alignment") or ""), -10),
        "relative_strength_score": {
            "HIGH": 18,
            "OK": 4,
            "MID": -2,
            "LOW": -18,
        }.get(str(traits.get("rs_band") or ""), -6),
        "open_interest": {
            "RISING": 4,
            "FLAT": -1,
            "FALLING": -4,
            "MISSING": -2,
        }.get(str(traits.get("oi") or ""), -2),
        "confirmation": 2 if traits.get("confirmation") == "RETEST" else -2,
        "target_liquidity": {
            "CLEAR": 4,
            "NEAR": -3,
            "VERY_NEAR": -10,
            "ADVERSE": -15,
            "UNKNOWN": -2,
        }.get(str(traits.get("liquidity") or ""), -2),
        "volume": {
            "HIGH": 6,
            "MID": 0,
            "OK": -4,
            "LOW": -10,
            "UNKNOWN": -2,
        }.get(str(traits.get("volume") or ""), -2),
    }

    permanent_flags = {
        "adverse_liquidity_nearby",
        "liquidation_cascade",
        "structure_break_against",
    }
    severe_count = len(risk_flags.intersection(permanent_flags))
    components["permanent_risk_flags"] = -min(30, severe_count * 20)
    if "absorption_against" in risk_flags:
        components["absorption"] = -10
    soft_hostile = risk_flags.intersection({
        "taker_flow_against",
        "aggressive_delta_against",
        "book_imbalance_against",
    })
    components["soft_hostile_flags"] = -min(6, len(soft_hostile) * 2)

    raw_score = Decimal("50") + Decimal(sum(components.values()))
    score = max(Decimal("0"), min(Decimal("100"), raw_score))
    if score >= config.shadow_conditional_lab_v2_high_score:
        bucket = "HIGH"
    elif score >= config.shadow_conditional_lab_v2_mid_score:
        bucket = "MID"
    else:
        bucket = "LOW"
    return {
        **base,
        "score_version": "conditional_context_v2",
        "score": str(score),
        "raw_score": str(raw_score),
        "bucket": bucket,
        "components": components,
        "calibration_basis": "post_v1_hypothesis_frozen_for_future_oos",
    }


def _shadow_conditional_lab_v2_variant(
    signal: Signal,
    config: StrategyConfig,
) -> tuple[Signal | None, dict[str, Any] | None]:
    if not config.shadow_conditional_lab_v2_enabled:
        return None, None
    profile = _shadow_conditional_profile_v2(signal, config)
    if profile is None:
        return None, None
    source = str(profile["source_strategy"])
    bucket = str(profile["bucket"])
    strategy_bucket = f"SQZ_UPD_C2_COND_{bucket}_SHADOW"
    metadata = dict(signal.metadata or {})
    metadata.pop("controlled_shadow", None)
    measurement_shadow = {
        "bucket": "conditional_shadow_lab_v2",
        "strategy_bucket": strategy_bucket,
        "source_strategy": source,
        "source_cluster_id": profile["source_cluster_id"],
        "policy_arm": f"CONDITIONAL_V2_{bucket}",
        "cohort": config.shadow_conditional_lab_v2_cohort,
        "risk_cap_pct": str(config.shadow_conditional_lab_v2_risk_cap_pct),
        "conditional_profile": profile,
        "execution_constraints": [
            "pre_context_candidate",
            "one_non_overlapping_bucket_per_source_cluster",
            "identical_source_exit_profile",
            "virtual_shadow_only",
            "no_production_admission_authority",
            "future_oos_only",
        ],
    }
    source_version = _strategy_logic_version(source) or "unknown"
    return replace(
        signal,
        metadata={
            **metadata,
            "strategy": strategy_bucket,
            "strategy_mode": "shadow",
            "strategy_source": source,
            "exit_profile_strategy": source,
            "measurement_shadow": measurement_shadow,
            "conditional_profile": profile,
            "strategy_logic_version": (
                f"{source_version}:conditional_lab_v2:"
                f"{config.shadow_conditional_lab_v2_cohort}:{bucket}"
            ),
        },
    ), profile


def _order_flow_metadata(signal: Signal) -> dict[str, Any]:
    metadata = signal.metadata or {}
    payload = metadata.get("order_flow") if isinstance(metadata, dict) else None
    return payload if isinstance(payload, dict) else {}


def _relative_strength_alignment(signal: Signal) -> str:
    metadata = signal.metadata or {}
    payload = metadata.get("relative_strength") if isinstance(metadata, dict) else None
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("alignment") or "")


def _relative_strength_score(signal: Signal) -> Decimal | None:
    metadata = signal.metadata or {}
    payload = metadata.get("relative_strength") if isinstance(metadata, dict) else None
    if not isinstance(payload, dict):
        return None
    return _optional_decimal(payload.get("score"))


def _is_strong_clean_squeeze_release(
    signal: Signal,
    *,
    alignment: str,
    score: Decimal,
    risk_flags: set[str],
) -> bool:
    metadata = signal.metadata or {}
    state = str(metadata.get("squeeze_state") or "")
    timing = str(metadata.get("squeeze_entry_timing") or "")
    breakout_atr = _optional_decimal(metadata.get("breakout_atr"))
    return (
        state == "release"
        and timing == "release_followthrough"
        and breakout_atr is not None
        and breakout_atr >= Decimal("1.50")
        and alignment == "aligned"
        and score >= Decimal("0.72")
        and not risk_flags
    )


def _controlled_paper_sqz_override(
    signal: Signal,
    rejection: tuple[str, str] | None,
    config: StrategyConfig,
) -> Signal | None:
    """Admit one narrowly defined SQZ paper experiment without weakening safety gates."""
    if not config.squeeze_controlled_paper_enabled or _signal_strategy(signal) != "SQUEEZE_BREAKOUT":
        return None
    if rejection is None or rejection[0] != "RELATIVE_STRENGTH":
        return None

    order_flow = _order_flow_metadata(signal)
    alignment = str(order_flow.get("alignment") or "mixed")
    score = _optional_decimal(order_flow.get("score")) or Decimal("0")
    risk_flags = {str(flag) for flag in order_flow.get("risk_flags") or []}
    reasons = {str(reason) for reason in order_flow.get("reasons") or []}

    # Only neutral RS is relaxed. Missing benchmark or an asset moving against
    # the trade remains a hard stop, as do all adverse order-flow flags.
    if _relative_strength_alignment(signal) != "neutral":
        return None
    if alignment != "aligned" or score < config.squeeze_controlled_paper_min_order_flow_score:
        return None
    if risk_flags or "structure_break_aligned" not in reasons:
        return None
    retest_confirmed = bool((signal.metadata or {}).get("squeeze_retest_confirmed"))
    if not retest_confirmed and not _is_strong_clean_squeeze_release(
        signal,
        alignment=alignment,
        score=score,
        risk_flags=risk_flags,
    ):
        return None

    metadata = {
        **dict(signal.metadata or {}),
        "controlled_paper": {
            "bucket": "sqz_relative_strength_neutral_v1",
            "relaxed_gate": "RELATIVE_STRENGTH",
            "relative_strength_alignment": "neutral",
            "order_flow_score": str(score),
            "risk_cap_pct": str(config.squeeze_controlled_paper_risk_cap_pct),
        },
    }
    return replace(signal, metadata=metadata)


def _squeeze_order_flow_measurement_override(
    signal: Signal,
    rejection: tuple[str, str] | None,
    config: StrategyConfig,
) -> Signal | None:
    """Create a separate paper bucket for one measurable weak-mixed OF case.

    This is deliberately not a global gate bypass.  The control SQZ remains
    strict, and the experimental bucket may relax only a low mixed OF score
    after every independent breakout confirmation has passed.
    """
    if (
        not config.squeeze_order_flow_measurement_enabled
        or config.order_flow_entry_gate_mode != "measure"
        or _signal_strategy(signal) != "SQUEEZE_BREAKOUT"
        or rejection is None
        or rejection[0] != "ORDER_FLOW"
    ):
        return None

    order_flow = _order_flow_metadata(signal)
    alignment = str(order_flow.get("alignment") or "mixed")
    score = _optional_decimal(order_flow.get("score")) or Decimal("0")
    risk_flags = {str(flag) for flag in order_flow.get("risk_flags") or []}
    reasons = {str(reason) for reason in order_flow.get("reasons") or []}
    retest_confirmed = bool((signal.metadata or {}).get("squeeze_retest_confirmed"))

    # The only relaxed condition is the score of a mixed, otherwise clean flow.
    # "against", any risk flag, no RS, no retest and no structure break stay
    # hard-blocked even while this bucket is enabled.
    if alignment != "mixed" or risk_flags:
        return None
    if not (config.squeeze_order_flow_measurement_min_score <= score < config.order_flow_mixed_score_floor):
        return None
    if _relative_strength_alignment(signal) != "aligned":
        return None
    if "structure_break_aligned" not in reasons or not retest_confirmed:
        return None

    metadata = {
        **dict(signal.metadata or {}),
        "strategy": "SQUEEZE_BREAKOUT_OF_MEASURE",
        "strategy_mode": "paper",
        "strategy_source": "SQUEEZE_BREAKOUT",
        "measurement_paper": {
            "bucket": "sqz_of_measure_weak_mixed_v1",
            "source_strategy": "SQUEEZE_BREAKOUT",
            "relaxed_gate": "ORDER_FLOW_WEAK_MIXED_SCORE",
            "order_flow_alignment": alignment,
            "order_flow_score": str(score),
            "risk_cap_pct": str(config.squeeze_order_flow_measurement_risk_cap_pct),
        },
        "strategy_logic_version": "sqz_of_measure_weak_mixed_v1",
    }
    return replace(signal, metadata=metadata)


def _controlled_paper_risk_cap(signal: Signal) -> Decimal | None:
    metadata = signal.metadata or {}
    if not isinstance(metadata, dict):
        return None
    for key in ("controlled_paper", "measurement_paper"):
        payload = metadata.get(key)
        if isinstance(payload, dict):
            risk_cap = _optional_decimal(payload.get("risk_cap_pct"))
            if risk_cap is not None:
                return risk_cap
    return None


def _cap_controlled_paper_sizing(
    sizing: DynamicSizingDecision,
    risk_cap_pct: Decimal | None,
) -> DynamicSizingDecision:
    if risk_cap_pct is None or sizing.risk_pct <= risk_cap_pct:
        return sizing
    return replace(
        sizing,
        risk_pct=risk_cap_pct,
        cap_risk_pct=min(sizing.cap_risk_pct, risk_cap_pct),
        reasons=(*sizing.reasons, f"controlled_paper_risk_cap={risk_cap_pct}"),
    )


def _controlled_shadow_sqz_dynamic_neutral_override(
    signal: Signal,
    config: StrategyConfig,
) -> Signal:
    """Create an isolated shadow sample for clean SQZ-DYN neutral-RS retests."""
    if (
        not config.squeeze_dynamic_neutral_shadow_enabled
        or _signal_strategy(signal) != "SQUEEZE_BREAKOUT_DYNAMIC"
        or _relative_strength_alignment(signal) != "neutral"
    ):
        return signal

    metadata = signal.metadata or {}
    order_flow = _order_flow_metadata(signal)
    alignment = str(order_flow.get("alignment") or "mixed")
    score = _optional_decimal(order_flow.get("score")) or Decimal("0")
    risk_flags = {str(flag) for flag in order_flow.get("risk_flags") or []}
    reasons = {str(reason) for reason in order_flow.get("reasons") or []}
    retest_confirmed = bool(metadata.get("squeeze_retest_confirmed"))

    if (
        alignment != "aligned"
        or score < config.squeeze_dynamic_neutral_shadow_min_order_flow_score
        or risk_flags
        or "structure_break_aligned" not in reasons
        or not retest_confirmed
    ):
        return signal

    return replace(
        signal,
        metadata={
            **dict(metadata),
            "controlled_shadow": {
                "bucket": "sqz_dyn_neutral_rs_retest_v1",
                "strategy_bucket": "SQUEEZE_BREAKOUT_DYNAMIC_NEUTRAL_RS",
                "source_strategy": "SQUEEZE_BREAKOUT_DYNAMIC",
                "relaxed_gate": "RELATIVE_STRENGTH",
                "relative_strength_alignment": "neutral",
                "order_flow_score": str(score),
                "risk_cap_pct": str(config.squeeze_dynamic_neutral_shadow_risk_cap_pct),
            },
            "strategy_logic_version": "sqz_dyn_neutral_rs_shadow_v1",
        },
    )


def _is_controlled_shadow_sqz_dynamic_neutral(signal: Signal) -> bool:
    metadata = signal.metadata or {}
    payload = metadata.get("controlled_shadow") if isinstance(metadata, dict) else None
    return bool(
        isinstance(payload, dict)
        and payload.get("bucket") == "sqz_dyn_neutral_rs_retest_v1"
        and payload.get("strategy_bucket") == "SQUEEZE_BREAKOUT_DYNAMIC_NEUTRAL_RS"
    )


SQZ_GATE_COHORT_STRATEGIES = {
    "SQZ_STRICT_CONTROL_SHADOW",
    "SQZ_OF_AGAINST_SHADOW",
    "SQZ_OF_HOSTILE_SHADOW",
    "SQZ_OF_ABSORPTION_SHADOW",
    "SQZ_RS_NEUTRAL_SHADOW",
    "SQZ_NO_RETEST_SHADOW",
}
SQZ_COHORT_PERMANENT_SAFETY_FLAGS = {
    "liquidation_cascade",
    "adverse_liquidity_nearby",
    "structure_break_against",
}
SQZ_COHORT_TESTABLE_HOSTILE_FLAGS = {
    "taker_flow_against",
    "aggressive_delta_against",
    "book_imbalance_against",
}
SQZ_COHORT_RELAXED_GATE_TO_STRATEGY = {
    "OF_AGAINST": "SQZ_OF_AGAINST_SHADOW",
    "OF_HOSTILE": "SQZ_OF_HOSTILE_SHADOW",
    "OF_ABSORPTION": "SQZ_OF_ABSORPTION_SHADOW",
    "RS_NEUTRAL": "SQZ_RS_NEUTRAL_SHADOW",
    "SQZ_RETEST_OR_STRONG_RELEASE": "SQZ_NO_RETEST_SHADOW",
}


def _sqz_source_cluster_id(signal: Signal) -> str:
    metadata = signal.metadata or {}
    close_time = metadata.get("signal_bar_close_time")
    if close_time not in (None, ""):
        return f"{signal.symbol}:{signal.direction.value}:{close_time}"
    return f"{signal.symbol}:{signal.direction.value}:{signal.entry_price}:{signal.stop_loss}"


def _sqz_gate_cohort_shadow_variants(
    signal: Signal,
    config: StrategyConfig,
) -> tuple[list[Signal], list[str], list[str]]:
    """Return independent SQZ counterfactuals with exactly one relaxed gate."""
    if (
        not config.squeeze_gate_cohort_shadow_enabled
        or _signal_strategy(signal) != "SQUEEZE_BREAKOUT"
    ):
        return [], [], []

    metadata = signal.metadata or {}
    order_flow = _order_flow_metadata(signal)
    alignment = str(order_flow.get("alignment") or "mixed")
    score = _optional_decimal(order_flow.get("score")) or Decimal("0")
    risk_flags = {str(flag) for flag in order_flow.get("risk_flags") or []}
    reasons = {str(reason) for reason in order_flow.get("reasons") or []}
    safety_rejections = sorted(risk_flags.intersection(SQZ_COHORT_PERMANENT_SAFETY_FLAGS))

    gate_failures: list[str] = []
    if alignment == "against":
        gate_failures.append("OF_AGAINST")
    hostile_flags = risk_flags.intersection(SQZ_COHORT_TESTABLE_HOSTILE_FLAGS)
    if hostile_flags and score < config.order_flow_hostile_score_floor:
        gate_failures.append("OF_HOSTILE")
    if "absorption_against" in risk_flags:
        gate_failures.append("OF_ABSORPTION")
    if alignment == "mixed" and score < config.order_flow_mixed_score_floor:
        gate_failures.append("OF_WEAK_MIXED_SCORE")

    relative_strength = _relative_strength_alignment(signal)
    if relative_strength == "neutral":
        gate_failures.append("RS_NEUTRAL")
    elif relative_strength != "aligned":
        gate_failures.append("RS_UNALIGNED")

    retest_confirmed = bool(metadata.get("squeeze_retest_confirmed"))
    if not retest_confirmed and not _is_strong_clean_squeeze_release(
        signal,
        alignment=alignment,
        score=score,
        risk_flags=risk_flags,
    ):
        gate_failures.append("SQZ_RETEST_OR_STRONG_RELEASE")
    if "structure_break_aligned" not in reasons:
        gate_failures.append("STRUCTURE_BREAK")

    if safety_rejections:
        return [], gate_failures, safety_rejections

    strategy = None
    relaxed_gate = None
    if not gate_failures:
        strategy = "SQZ_STRICT_CONTROL_SHADOW"
        relaxed_gate = "NONE_STRICT_CONTROL"
    elif len(gate_failures) == 1:
        relaxed_gate = gate_failures[0]
        strategy = SQZ_COHORT_RELAXED_GATE_TO_STRATEGY.get(relaxed_gate)

    if strategy is None:
        return [], gate_failures, safety_rejections

    source_cluster_id = _sqz_source_cluster_id(signal)
    measurement_shadow = {
        "bucket": "sqz_gate_cohort_v1",
        "strategy_bucket": strategy,
        "source_strategy": "SQUEEZE_BREAKOUT",
        "source_cluster_id": source_cluster_id,
        "relaxed_gate": relaxed_gate,
        "gate_vector": gate_failures,
        "risk_cap_pct": str(config.squeeze_gate_cohort_shadow_risk_cap_pct),
        "execution_constraints": [
            "one_open_trade_per_symbol_bucket",
            "no_reentry_or_loss_control_suppression",
            "no_portfolio_capacity_competition",
        ],
    }
    variant = replace(
        signal,
        metadata={
            **dict(metadata),
            "strategy": strategy,
            "strategy_mode": "shadow",
            "strategy_source": "SQUEEZE_BREAKOUT",
            "exit_profile_strategy": "SQUEEZE_BREAKOUT",
            "measurement_shadow": measurement_shadow,
            "strategy_logic_version": "sqz_gate_cohort_v1",
        },
    )
    return [variant], gate_failures, safety_rejections


def _measurement_shadow_payload(signal: Signal) -> dict[str, Any]:
    metadata = signal.metadata or {}
    payload = metadata.get("measurement_shadow") if isinstance(metadata, dict) else None
    return payload if isinstance(payload, dict) else {}


def _measurement_shadow_source_seen(
    trades: list[dict[str, Any]],
    strategy: str,
    source_cluster_id: str,
) -> bool:
    if not source_cluster_id:
        return False
    for trade in trades:
        if _trade_strategy(trade) != strategy:
            continue
        metadata = _trade_metadata(trade)
        signal_metadata = metadata.get("signal_metadata")
        payload = signal_metadata.get("measurement_shadow") if isinstance(signal_metadata, dict) else None
        if not isinstance(payload, dict):
            payload = metadata.get("measurement_shadow")
        if isinstance(payload, dict) and str(payload.get("source_cluster_id") or "") == source_cluster_id:
            return True
    return False


def _conditional_shadow_source_seen(
    trades: list[dict[str, Any]],
    candidate_payload: dict[str, Any],
) -> bool:
    """Keep each conditional-lab source cluster in one bucket per cohort."""
    bucket = str(candidate_payload.get("bucket") or "")
    cohort = str(candidate_payload.get("cohort") or "")
    source_cluster_id = str(candidate_payload.get("source_cluster_id") or "")
    if not bucket.startswith("conditional_shadow_lab_") or not cohort or not source_cluster_id:
        return False
    for trade in trades:
        metadata = _trade_metadata(trade)
        signal_metadata = metadata.get("signal_metadata")
        payload = signal_metadata.get("measurement_shadow") if isinstance(signal_metadata, dict) else None
        if not isinstance(payload, dict):
            payload = metadata.get("measurement_shadow")
        if not isinstance(payload, dict):
            continue
        if (
            str(payload.get("bucket") or "") == bucket
            and str(payload.get("cohort") or "") == cohort
            and str(payload.get("source_cluster_id") or "") == source_cluster_id
        ):
            return True
    return False


def _shadow_execution_strategy(signal: Signal) -> str:
    metadata = signal.metadata or {}
    if isinstance(metadata, dict):
        for key in ("measurement_shadow", "controlled_shadow"):
            payload = metadata.get(key)
            if isinstance(payload, dict) and payload.get("strategy_bucket"):
                return str(payload["strategy_bucket"])
    return _signal_strategy(signal)


def _controlled_shadow_risk_cap(signal: Signal) -> Decimal | None:
    metadata = signal.metadata or {}
    if not isinstance(metadata, dict):
        return None
    for key in ("measurement_shadow", "controlled_shadow"):
        payload = metadata.get(key)
        if isinstance(payload, dict):
            risk_cap = _optional_decimal(payload.get("risk_cap_pct"))
            if risk_cap is not None:
                return risk_cap
    return None


def _cap_controlled_shadow_sizing(
    sizing: DynamicSizingDecision,
    risk_cap_pct: Decimal | None,
) -> DynamicSizingDecision:
    if risk_cap_pct is None or sizing.risk_pct <= risk_cap_pct:
        return sizing
    return replace(
        sizing,
        risk_pct=risk_cap_pct,
        cap_risk_pct=min(sizing.cap_risk_pct, risk_cap_pct),
        reasons=(*sizing.reasons, f"controlled_shadow_risk_cap={risk_cap_pct}"),
    )


def _annotate_shadow_order_flow_gate(
    signal: Signal,
    *,
    strict_rejection_reason: str | None,
    execution_rejection_reason: str | None,
    enforced: bool,
    overridden: bool,
) -> Signal:
    """Keep the counterfactual OF decision with every virtual shadow entry."""
    metadata = {
        **dict(signal.metadata or {}),
        "shadow_order_flow_gate": {
            "enforced": enforced,
            "would_block": strict_rejection_reason is not None,
            "would_block_reason": strict_rejection_reason,
            "overridden_for_research": overridden,
            "execution_eligible": execution_rejection_reason is None,
            "structural_rejection_reason": execution_rejection_reason,
        },
    }
    return replace(signal, metadata=metadata)


def _shadow_candidate_context_rejection_reason(
    signal: Signal,
    *,
    enforce_order_flow: bool = True,
    relaxed_gate: str | Collection[str] | None = None,
) -> str | None:
    strategy = _signal_strategy(signal)
    if strategy not in {
        "SQUEEZE_BREAKOUT_DYNAMIC",
        "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
        "LIQUIDITY_SWEEP_REVERSAL",
        "TREND_PULLBACK",
        "VWAP_REVERSION",
        "VWAP_REVERSION_WATCH",
        "RANGE_GRID",
        "MOMENTUM_CONTINUATION",
        "TREND_FOLLOWING",
    }:
        return None

    metadata = signal.metadata or {}
    order_flow = _order_flow_metadata(signal)
    if not order_flow:
        return None

    alignment = str(order_flow.get("alignment") or "mixed")
    score = Decimal(str(order_flow.get("score") or "0"))
    risk_flags = {str(flag) for flag in order_flow.get("risk_flags") or []}
    if strategy in {"SQUEEZE_BREAKOUT_DYNAMIC", "SQUEEZE_BREAKOUT_DYNAMIC_UPD"}:
        hard_flags = {
            "taker_flow_against",
            "aggressive_delta_against",
            "book_imbalance_against",
            "liquidation_cascade",
            "structure_break_against",
            "adverse_liquidity_nearby",
        }
        retest_confirmed = bool(metadata.get("squeeze_retest_confirmed"))
        if enforce_order_flow:
            if alignment == "against" and not _shadow_gate_is_relaxed(signal, relaxed_gate, "OF_AGAINST"):
                return "SQZ-DYN shadow blocked: order-flow is against breakout."
            if (
                risk_flags.intersection(hard_flags)
                and not _shadow_gate_is_relaxed(signal, relaxed_gate, "OF_HOSTILE")
            ):
                flags = ",".join(sorted(risk_flags.intersection(hard_flags)))
                return f"SQZ-DYN shadow blocked: hostile breakout flow ({flags})."
            if (
                "absorption_against" in risk_flags
                and not _shadow_gate_is_relaxed(signal, relaxed_gate, "OF_ABSORPTION")
            ):
                return f"SQZ-DYN shadow blocked: absorption against breakout, score {score:.2f}."
        rs_alignment = _relative_strength_alignment(signal)
        if (
            rs_alignment != "aligned"
            and not _is_controlled_shadow_sqz_dynamic_neutral(signal)
            and not _shadow_relative_strength_gate_is_relaxed(signal, relaxed_gate)
        ):
            return (
                "SQZ-DYN shadow blocked: relative-strength confirmation is "
                f"{rs_alignment or 'missing'}."
            )
        if strategy == "SQUEEZE_BREAKOUT_DYNAMIC_UPD":
            target_distance = _target_liquidity_distance_bps(signal, order_flow)
            if (
                not _shadow_gate_is_relaxed(signal, relaxed_gate, "NEAR_LIQUIDITY")
                and not retest_confirmed
                and target_distance is not None
                and target_distance < Decimal("20")
            ):
                return f"SQZ-DYN-UPD shadow blocked: target liquidity is too close without retest ({target_distance:.1f} bps)."
            oi_change = _optional_decimal(order_flow.get("open_interest_change_pct"))
            if oi_change is None:
                oi_change = _optional_decimal(metadata.get("open_interest_change_pct"))
            if oi_change is None and not _shadow_gate_is_relaxed(signal, relaxed_gate, "MISSING_OI"):
                return "SQZ-DYN-UPD shadow blocked: missing open-interest confirmation."
        if not retest_confirmed:
            if _shadow_gate_is_relaxed(signal, relaxed_gate, "NO_RETEST"):
                pass
            elif not enforce_order_flow:
                return "SQZ-DYN shadow blocked: retest is required for the baseline sample."
            elif not _is_strong_clean_squeeze_release(
                signal,
                alignment=alignment,
                score=score,
                risk_flags=risk_flags,
            ):
                return "SQZ-DYN shadow blocked: no retest and release is not strong enough."
        if enforce_order_flow:
            if (
                alignment == "mixed"
                and score < Decimal("0.50")
                and not _shadow_gate_is_relaxed(signal, relaxed_gate, "OF_WEAK")
            ):
                return f"SQZ-DYN shadow blocked: mixed flow needs retest confirmation, score {score:.2f}."
            if (
                alignment == "mixed"
                and not retest_confirmed
                and score < Decimal("0.58")
                and not _shadow_gate_is_relaxed(signal, relaxed_gate, "OF_WEAK")
            ):
                return f"SQZ-DYN shadow blocked: mixed flow needs retest confirmation, score {score:.2f}."
            if (
                alignment == "aligned"
                and score < Decimal("0.55")
                and not _shadow_gate_is_relaxed(signal, relaxed_gate, "OF_WEAK")
            ):
                return f"SQZ-DYN shadow blocked: aligned flow is too weak, score {score:.2f}."
    elif strategy == "LIQUIDITY_SWEEP_REVERSAL":
        if enforce_order_flow:
            if "adverse_liquidity_nearby" in risk_flags:
                return "LSR shadow blocked: adverse liquidity remains nearby after the sweep."
            if alignment == "against" or score < Decimal("0.65"):
                return f"LSR shadow blocked: order-flow score {score:.2f} is not strong enough after sweep."
    elif strategy == "TREND_PULLBACK":
        hard_flags = {
            "adverse_liquidity_nearby",
            "taker_flow_against",
            "book_imbalance_against",
            "aggressive_delta_against",
            "structure_break_against",
            "liquidation_cascade",
        }
        if enforce_order_flow:
            if (
                risk_flags.intersection(hard_flags)
                and not _shadow_gate_is_relaxed(signal, relaxed_gate, "OF_HOSTILE")
            ):
                flags = ",".join(sorted(risk_flags.intersection(hard_flags)))
                return f"TPB shadow blocked: hostile continuation flow ({flags})."
            alignment_gate = "OF_AGAINST" if alignment == "against" else "OF_WEAK"
            if alignment != "aligned" and not _shadow_gate_is_relaxed(signal, relaxed_gate, alignment_gate):
                return f"TPB shadow blocked: profitable bucket requires aligned order-flow, got {alignment}."
            min_score = Decimal("0.68") if signal.direction == Direction.SHORT else Decimal("0.62")
            if score < min_score and not _shadow_gate_is_relaxed(signal, relaxed_gate, "OF_WEAK"):
                return f"TPB shadow blocked: order-flow score {score:.2f} below {min_score:.2f}."
        relative_strength = (signal.metadata or {}).get("relative_strength")
        rs_alignment = ""
        rs_score: Decimal | None = None
        if isinstance(relative_strength, dict):
            rs_alignment = str(relative_strength.get("alignment") or "")
            rs_score = _optional_decimal(relative_strength.get("score"))
        if rs_alignment != "aligned" and not _shadow_relative_strength_gate_is_relaxed(signal, relaxed_gate):
            if signal.direction == Direction.SHORT:
                return f"TPB shadow blocked: short needs relative-weakness confirmation, got {rs_alignment or 'missing'}."
            return f"TPB shadow blocked: long needs relative-strength confirmation, got {rs_alignment or 'missing'}."
        if rs_score is None or rs_score < Decimal("0.60"):
            return f"TPB shadow blocked: relative-strength score {rs_score or Decimal('0'):.2f} below 0.60."
        depth_atr = _optional_decimal((signal.metadata or {}).get("pullback_depth_atr"))
        if depth_atr is not None and depth_atr < Decimal("0.65"):
            return f"TPB shadow blocked: pullback depth {depth_atr:.2f} ATR is too shallow for the profitable bucket."
        if depth_atr is not None and depth_atr > Decimal("1.95"):
            return f"TPB shadow blocked: pullback depth {depth_atr:.2f} ATR is too extended for the profitable bucket."
        volume_ratio = _optional_decimal((signal.metadata or {}).get("volume_ratio"))
        if volume_ratio is not None and volume_ratio < Decimal("1.20"):
            return f"TPB shadow blocked: volume ratio {volume_ratio:.2f} is below profitable bucket minimum."
        target_distance = _target_liquidity_distance_bps(signal, order_flow)
        if (
            not _shadow_gate_is_relaxed(signal, relaxed_gate, "NEAR_LIQUIDITY")
            and target_distance is not None
            and target_distance < Decimal("12")
        ):
            return f"TPB shadow blocked: target liquidity is already too close ({target_distance:.1f} bps)."
    elif strategy == "MOMENTUM_CONTINUATION":
        hard_flags = {
            "adverse_liquidity_nearby",
            "taker_flow_against",
            "book_imbalance_against",
            "aggressive_delta_against",
            "structure_break_against",
            "liquidation_cascade",
            "absorption_against",
        }
        if enforce_order_flow:
            if (
                risk_flags.intersection(hard_flags)
                and not _shadow_gate_is_relaxed(signal, relaxed_gate, "OF_HOSTILE")
            ):
                flags = ",".join(sorted(risk_flags.intersection(hard_flags)))
                return f"MOM shadow blocked: hostile momentum flow ({flags})."
            alignment_gate = "OF_AGAINST" if alignment == "against" else "OF_WEAK"
            if alignment != "aligned" and not _shadow_gate_is_relaxed(signal, relaxed_gate, alignment_gate):
                return f"MOM shadow blocked: continuation requires aligned order-flow, got {alignment}."
            if score < Decimal("0.78") and not _shadow_gate_is_relaxed(signal, relaxed_gate, "OF_WEAK"):
                return f"MOM shadow blocked: order-flow score {score:.2f} below 0.78."
        rs_alignment = _relative_strength_alignment(signal)
        rs_score = _relative_strength_score(signal)
        if rs_alignment != "aligned" and not _shadow_relative_strength_gate_is_relaxed(signal, relaxed_gate):
            return f"MOM shadow blocked: continuation needs relative-strength confirmation, got {rs_alignment or 'missing'}."
        if rs_score is None or rs_score < Decimal("0.62"):
            return f"MOM shadow blocked: relative-strength score {rs_score or Decimal('0'):.2f} below 0.62."
        target_distance = _target_liquidity_distance_bps(signal, order_flow)
        if (
            not _shadow_gate_is_relaxed(signal, relaxed_gate, "NEAR_LIQUIDITY")
            and target_distance is not None
            and target_distance < Decimal("12")
        ):
            return f"MOM shadow blocked: target liquidity is already too close ({target_distance:.1f} bps)."
        breakout_extension = _optional_decimal(metadata.get("breakout_extension_atr"))
        if breakout_extension is not None and breakout_extension < Decimal("0.15"):
            return f"MOM shadow blocked: breakout extension {breakout_extension:.2f} ATR is too weak."
    elif strategy in {"VWAP_REVERSION", "VWAP_REVERSION_WATCH"}:
        dangerous_flags = {
            "liquidation_cascade",
            "structure_break_against",
            "aggressive_delta_against",
        }
        if enforce_order_flow:
            if risk_flags.intersection(dangerous_flags):
                flags = ",".join(sorted(risk_flags))
                return f"{strategy} shadow blocked: dangerous liquidity context ({flags})."
            min_score = Decimal("0.45") if strategy == "VWAP_REVERSION" else Decimal("0.40")
            if alignment == "against" or score < min_score:
                return f"{strategy} shadow blocked: order-flow is not clean enough for reversion, score {score:.2f}."
    elif strategy == "RANGE_GRID":
        dangerous_flags = {
            "structure_break_against",
            "absorption_against",
            "liquidation_cascade",
        }
        if enforce_order_flow:
            if risk_flags.intersection(dangerous_flags):
                flags = ",".join(sorted(risk_flags))
                return f"GRID shadow blocked: dangerous range-edge flow ({flags})."
            if alignment == "against":
                return f"GRID shadow blocked: order-flow is against range fade with score {score:.2f}."
            if alignment == "mixed" and score < Decimal("0.42"):
                return f"GRID shadow blocked: mixed order-flow is too weak for range fade, score {score:.2f}."
            if alignment == "aligned" and score < Decimal("0.30"):
                return f"GRID shadow blocked: aligned order-flow is too weak for range fade, score {score:.2f}."
    elif strategy == "TREND_FOLLOWING":
        hard_flags = {
            "taker_flow_against",
            "aggressive_delta_against",
            "book_imbalance_against",
            "structure_break_against",
            "liquidation_cascade",
        }
        if enforce_order_flow:
            if risk_flags.intersection(hard_flags):
                flags = ",".join(sorted(risk_flags.intersection(hard_flags)))
                return f"TF shadow blocked: hostile trend-following flow ({flags})."
            if alignment == "against" and score < Decimal("0.40"):
                return f"TF shadow blocked: trend continuation needs strong aligned order-flow, got {alignment} {score:.2f}."
        relative_strength = metadata.get("relative_strength")
        rs_alignment = ""
        if isinstance(relative_strength, dict):
            rs_alignment = str(relative_strength.get("alignment") or "")
        if signal.direction == Direction.SHORT and rs_alignment == "against":
            return f"TF shadow blocked: short needs relative-weakness confirmation, got {rs_alignment or 'missing'}."
        if signal.direction == Direction.LONG and rs_alignment == "against":
            return f"TF shadow blocked: long needs relative-strength confirmation, got {rs_alignment or 'missing'}."
        atr_pct = _optional_decimal(metadata.get("atr_pct"))
        if atr_pct is not None and atr_pct < Decimal("0.35"):
            return f"TF shadow blocked: ATR {atr_pct:.2f}% is too low for trend continuation."
        liquidity_side = str(order_flow.get("liquidity_side") or "")
        if signal.direction == Direction.SHORT and liquidity_side == "downside":
            distance = _optional_decimal(order_flow.get("distance_to_lower_liquidity_bps"))
            if distance is not None and distance <= Decimal("12"):
                return f"TF shadow blocked: downside liquidity is already too close ({distance:.1f} bps)."
        if signal.direction == Direction.LONG and liquidity_side == "upside":
            distance = _optional_decimal(order_flow.get("distance_to_upper_liquidity_bps"))
            if distance is not None and distance <= Decimal("12"):
                return f"TF shadow blocked: upside liquidity is already too close ({distance:.1f} bps)."
    return None


def _order_flow_entry_rejection_reason(
    signal: Signal,
    strategy_config: "StrategyConfig | None" = None,
) -> tuple[str, str] | None:
    strategy = _signal_strategy(signal)
    order_flow = _order_flow_metadata(signal)
    if not order_flow:
        return None

    alignment = str(order_flow.get("alignment") or "mixed")
    score = Decimal(str(order_flow.get("score") or "0"))
    risk_flags = {str(flag) for flag in order_flow.get("risk_flags") or []}

    hostile_floor = (
        strategy_config.order_flow_hostile_score_floor if strategy_config is not None else Decimal("0.70")
    )
    mixed_floor = (
        strategy_config.order_flow_mixed_score_floor if strategy_config is not None else Decimal("0.45")
    )

    if strategy in {"SQUEEZE_BREAKOUT", "SQUEEZE_BREAKOUT_DYNAMIC"}:
        reasons = {str(reason) for reason in order_flow.get("reasons") or []}
        hard_flags = {
            "taker_flow_against",
            "aggressive_delta_against",
            "book_imbalance_against",
            "liquidation_cascade",
            "structure_break_against",
            "adverse_liquidity_nearby",
        }
        if alignment == "against":
            return "ORDER_FLOW", f"{strategy} blocked: order-flow is against breakout."
        if risk_flags.intersection(hard_flags) and score < hostile_floor:
            flags = ",".join(sorted(risk_flags.intersection(hard_flags)))
            return "ORDER_FLOW", f"{strategy} blocked: hostile breakout flow ({flags}), score {score:.2f}."
        if "absorption_against" in risk_flags:
            return "ORDER_FLOW", f"{strategy} blocked: absorption against breakout, score {score:.2f}."
        if alignment == "mixed" and score < mixed_floor:
            return "ORDER_FLOW", f"{strategy} blocked: weak mixed order-flow score {score:.2f}."
        rs_alignment = _relative_strength_alignment(signal)
        if rs_alignment != "aligned":
            return (
                "RELATIVE_STRENGTH",
                f"{strategy} blocked: relative-strength confirmation is {rs_alignment or 'missing'}.",
            )
        retest_confirmed = bool((signal.metadata or {}).get("squeeze_retest_confirmed"))
        if not retest_confirmed and not _is_strong_clean_squeeze_release(
            signal,
            alignment=alignment,
            score=score,
            risk_flags=risk_flags,
        ):
            return "SQZ_RETEST", f"{strategy} blocked: no retest and release is not strong enough."
        if strategy == "SQUEEZE_BREAKOUT" and "structure_break_aligned" not in reasons:
            return (
                "STRUCTURE_BREAK",
                "SQUEEZE_BREAKOUT blocked: missing 15m structure-break confirmation.",
            )

    if strategy == "LIQUIDITY_SWEEP_REVERSAL":
        if "adverse_liquidity_nearby" in risk_flags:
            return "ORDER_FLOW", "LSR blocked: adverse liquidity remains nearby after sweep."
        if alignment != "aligned" or score < Decimal("0.72"):
            return "ORDER_FLOW", f"LSR blocked: sweep reclaim lacks clean follow-through, score {score:.2f}."

    if strategy in {"VWAP_REVERSION", "VWAP_REVERSION_WATCH"}:
        hard_flags = {"adverse_liquidity_nearby", "liquidation_cascade", "structure_break_against"}
        if risk_flags.intersection(hard_flags):
            flags = ",".join(sorted(risk_flags.intersection(hard_flags)))
            return "ORDER_FLOW", f"{strategy} blocked: reversion context is dangerous ({flags})."
        if alignment == "against" or score < Decimal("0.62"):
            return "ORDER_FLOW", f"{strategy} blocked: order-flow is not clean enough for reversion, score {score:.2f}."

    if strategy == "RANGE_GRID":
        hard_flags = {
            "adverse_liquidity_nearby",
            "book_imbalance_against",
            "taker_flow_against",
            "aggressive_delta_against",
            "structure_break_against",
            "liquidation_cascade",
        }
        if risk_flags.intersection(hard_flags):
            flags = ",".join(sorted(risk_flags.intersection(hard_flags)))
            return "ORDER_FLOW", f"GRID blocked: unsafe range-flow context ({flags})."
        if alignment == "against" or score < Decimal("0.55"):
            return "ORDER_FLOW", f"GRID blocked: range fade flow is too weak, score {score:.2f}."

    return None


def _mean_reversion_context_rejection_reason(
    signal: Signal,
    btc_4h_change: Decimal | None,
    annotation: OrderFlowAnnotation,
    strategy_config: Any,
) -> tuple[str, str] | None:
    if _signal_strategy(signal) != "MEAN_REVERSION":
        return None

    if getattr(strategy_config, "mean_reversion_btc_direction_gate_enabled", True) and btc_4h_change is not None:
        threshold = abs(Decimal(str(getattr(strategy_config, "mean_reversion_btc_direction_gate_pct", "0.012"))))
        if threshold > 0:
            if signal.direction == Direction.SHORT and btc_4h_change >= threshold:
                return (
                    "MR_CONTEXT",
                    f"MR short blocked: BTC 4h impulse {btc_4h_change:.2%} is upward.",
                )
            if signal.direction == Direction.LONG and btc_4h_change <= -threshold:
                return (
                    "MR_CONTEXT",
                    f"MR long blocked: BTC 4h impulse {btc_4h_change:.2%} is downward.",
                )

    if getattr(strategy_config, "mean_reversion_order_flow_gate_enabled", True):
        min_score = Decimal(str(getattr(strategy_config, "mean_reversion_min_order_flow_score", "0.25")))
        risk_flags = set(annotation.risk_flags)
        against_flags = risk_flags.intersection(MR_ORDER_FLOW_AGAINST_FLAGS)
        severe_flags = risk_flags.intersection(MR_ORDER_FLOW_SEVERE_FLAGS)
        if annotation.alignment == "against":
            return (
                "MR_CONTEXT",
                f"MR blocked: order-flow alignment is against the signal; flags={','.join(sorted(risk_flags)) or 'none'}.",
            )
        if annotation.score < min_score and (severe_flags or len(against_flags) >= 2):
            flags = ",".join(sorted(severe_flags or against_flags))
            return (
                "MR_CONTEXT",
                f"MR blocked: weak order-flow score {annotation.score:.2f} below {min_score:.2f}; flags={flags}.",
            )
        if against_flags and annotation.score < Decimal("0.55"):
            flags = ",".join(sorted(against_flags))
            return (
                "MR_CONTEXT",
                f"MR blocked: mixed order-flow has adverse flags ({flags}) with score {annotation.score:.2f}.",
            )

    rs_alignment = _relative_strength_alignment(signal)
    rs_score = _relative_strength_score(signal)
    if rs_alignment == "against" and (rs_score is None or rs_score < Decimal("0.45")):
        return (
            "MR_CONTEXT",
            f"MR blocked: relative strength is against the reversal signal (score {rs_score or Decimal('0'):.2f}).",
        )

    return None


def _mean_reversion_expectancy_rejection_reason(
    signal: Signal,
    strategy_config: Any,
    risk_config: Any,
) -> tuple[str, str] | None:
    if _signal_strategy(signal) != "MEAN_REVERSION":
        return None
    if signal.stop_loss is None or signal.take_profit is None:
        return ("MR_EXPECTANCY", "MR blocked: missing stop-loss or take-profit for expectancy check.")

    entry = to_decimal(signal.entry_price)
    stop = to_decimal(signal.stop_loss)
    take = to_decimal(signal.take_profit)
    stop_distance = abs(entry - stop)
    reward_distance = abs(take - entry)
    if entry <= 0 or stop_distance <= 0:
        return ("MR_EXPECTANCY", "MR blocked: invalid entry or stop distance for expectancy check.")

    cost_bps = _mean_reversion_cost_bps(signal, risk_config)
    cost_per_unit = entry * cost_bps / Decimal("10000")
    net_reward_distance = reward_distance - cost_per_unit
    net_rr = net_reward_distance / stop_distance
    min_net_rr = Decimal(str(getattr(strategy_config, "mean_reversion_min_net_reward_risk", "1.15")))
    if net_rr < min_net_rr:
        return (
            "MR_EXPECTANCY",
            f"MR blocked: net RR {net_rr:.2f} below minimum {min_net_rr:.2f} after estimated costs {cost_bps:.2f} bps.",
        )

    min_winrate = Decimal(str(getattr(strategy_config, "mean_reversion_expected_winrate_floor", "0.48")))
    estimated_winrate = max(min_winrate, min(Decimal("0.68"), signal.confidence * Decimal("0.75")))
    expected_net_r = estimated_winrate * net_rr - (Decimal("1") - estimated_winrate)
    min_expected = Decimal(str(getattr(strategy_config, "mean_reversion_min_expected_net_r", "0.05")))
    if expected_net_r < min_expected:
        return (
            "MR_EXPECTANCY",
            f"MR blocked: expected net R {expected_net_r:.3f} below minimum {min_expected:.3f} "
            f"(p={estimated_winrate:.2f}, net_rr={net_rr:.2f}, costs={cost_bps:.2f} bps).",
        )

    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    metadata["mr_net_reward_risk"] = str(net_rr)
    metadata["mr_expected_net_r"] = str(expected_net_r)
    metadata["mr_estimated_cost_bps"] = str(cost_bps)
    metadata["mr_estimated_winrate"] = str(estimated_winrate)
    return None


def _mean_reversion_cost_bps(signal: Signal, risk_config: Any) -> Decimal:
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    taker_fee_bps = Decimal(str(getattr(risk_config, "taker_fee_bps", "4")))
    slippage_bps = Decimal(str(getattr(risk_config, "slippage_bps", "5")))
    funding_buffer_bps = Decimal(str(getattr(risk_config, "funding_buffer_bps", "1")))
    holding_hours = max(Decimal("0"), Decimal(str(getattr(risk_config, "funding_impact_holding_hours", "8"))))
    funding_rate_raw = metadata.get("funding_rate")
    funding_cost_bps = funding_buffer_bps
    if funding_rate_raw not in (None, ""):
        try:
            funding_rate = Decimal(str(funding_rate_raw))
            signed_rate = funding_rate if signal.direction == Direction.LONG else -funding_rate
            signed_bps = signed_rate * Decimal("10000") * (holding_hours / Decimal("8"))
            funding_cost_bps = max(Decimal("0"), signed_bps)
        except Exception:
            funding_cost_bps = funding_buffer_bps
    return taker_fee_bps * Decimal("2") + slippage_bps + funding_cost_bps


def _strategy_reentry_policy_reason(
    signal: Signal,
    trades: list[dict[str, Any]],
    cooldown_minutes: int,
    winning_cooldown_minutes: int | None,
    scale_in_enabled: bool,
    max_scale_ins_per_symbol_strategy: int,
) -> str | None:
    strategy = _signal_strategy(signal)
    same_trades = [trade for trade in trades if _same_symbol_strategy(signal, trade)]
    active_trades = [trade for trade in same_trades if _trade_status(trade) in ACTIVE_TRADE_STATUSES]
    if active_trades:
        if not scale_in_enabled:
            active_id = active_trades[0].get("id", "?")
            return f"Active {signal.symbol} {strategy} trade #{active_id} already exists; re-entry is blocked."
        if len(active_trades) > max(0, max_scale_ins_per_symbol_strategy):
            return (
                f"{signal.symbol} {strategy} scale-in limit reached "
                f"({len(active_trades)}/{max_scale_ins_per_symbol_strategy})."
            )
        return (
            f"{signal.symbol} {strategy} scale-in mode is enabled, but live scale-in execution is still gated; "
            "new add-ons require a separate positive-position/independent-signal check."
        )

    if cooldown_minutes <= 0:
        return None

    now = datetime.now(timezone.utc)
    for trade in same_trades:
        if _trade_status(trade) != "CLOSED":
            continue
        closed_at = _parse_datetime_utc(trade.get("closed_at") or trade.get("created_at"))
        if closed_at is None:
            continue
        effective_cooldown = _strategy_reentry_cooldown_for_trade(
            trade,
            default_cooldown_minutes=cooldown_minutes,
            winning_cooldown_minutes=winning_cooldown_minutes,
        )
        if effective_cooldown <= 0:
            return None
        elapsed_min = (now - closed_at).total_seconds() / 60
        if elapsed_min < effective_cooldown:
            remaining = max(1, int(effective_cooldown - elapsed_min))
            return (
                f"{signal.symbol} {strategy} re-entry cooldown is active "
                f"for ~{remaining} more minutes."
            )
        return None
    return None


def _strategy_reentry_cooldown_for_trade(
    trade: dict[str, Any],
    *,
    default_cooldown_minutes: int,
    winning_cooldown_minutes: int | None,
) -> int:
    if winning_cooldown_minutes is None:
        return default_cooldown_minutes
    close_reason = str(trade.get("close_reason") or "").lower()
    r_multiple = _decimal_or_zero(trade.get("r_multiple"))
    pnl = _decimal_or_zero(trade.get("realized_pnl"))
    is_win = close_reason in {"take_profit", "tp", "partial_take_profit", "trailing_take_profit"} or r_multiple > 0 or pnl > 0
    if is_win:
        return max(0, winning_cooldown_minutes)
    return default_cooldown_minutes


def _trade_cluster_metadata(
    signal: Signal,
    trades: list[dict[str, Any]],
    window_minutes: int,
) -> dict[str, Any]:
    window_minutes = max(1, int(window_minutes or 60))
    now = datetime.now(timezone.utc)
    strategy = _signal_strategy(signal)
    same_cluster_context: list[dict[str, Any]] = []
    for trade in trades:
        if not _same_symbol_strategy(signal, trade):
            continue
        if str(trade.get("direction") or "").upper() != signal.direction.value:
            continue
        created_at = _parse_datetime_utc(trade.get("created_at"))
        if created_at is None:
            continue
        if (now - created_at).total_seconds() / 60 <= window_minutes:
            same_cluster_context.append(trade)

    if not same_cluster_context:
        cluster_id = f"{signal.symbol}:{strategy}:{signal.direction.value}:{now.strftime('%Y%m%d%H%M%S')}"
        return {
            "trade_cluster_id": cluster_id,
            "trade_cluster_sequence": 1,
            "trade_cluster_window_minutes": window_minutes,
            "scale_in": False,
        }

    newest = max(
        same_cluster_context,
        key=lambda trade: _parse_datetime_utc(trade.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    newest_metadata = _trade_metadata(newest)
    cluster_id = newest_metadata.get("trade_cluster_id")
    if not cluster_id:
        newest_dt = _parse_datetime_utc(newest.get("created_at")) or now
        cluster_id = f"{signal.symbol}:{strategy}:{signal.direction.value}:{newest_dt.strftime('%Y%m%d%H%M%S')}"
    same_cluster_id_count = sum(
        1
        for trade in same_cluster_context
        if _trade_metadata(trade).get("trade_cluster_id") == cluster_id
    )
    sequence = same_cluster_id_count + 1 if same_cluster_id_count else len(same_cluster_context) + 1
    return {
        "trade_cluster_id": str(cluster_id),
        "trade_cluster_sequence": sequence,
        "trade_cluster_window_minutes": window_minutes,
        "scale_in": sequence > 1,
    }


def _sqz_dynamic_upd_series_rejection_reason(
    *,
    signal: Signal,
    trades: list[dict[str, Any]],
    window_minutes: int,
    max_same_direction_trades: int,
) -> str | None:
    if _signal_strategy(signal) != "SQUEEZE_BREAKOUT_DYNAMIC_UPD":
        return None
    now = datetime.now(timezone.utc)
    window_minutes = max(1, int(window_minutes))
    max_same_direction_trades = max(1, int(max_same_direction_trades))
    recent_same_direction = 0
    for trade in trades:
        if str(trade.get("strategy") or "").upper() != "SQUEEZE_BREAKOUT_DYNAMIC_UPD":
            continue
        if str(trade.get("direction") or "").upper() != signal.direction.value:
            continue
        created_at = _parse_datetime_utc(trade.get("created_at"))
        if created_at is None:
            continue
        if (now - created_at).total_seconds() / 60 <= window_minutes:
            recent_same_direction += 1
    if recent_same_direction >= max_same_direction_trades:
        return (
            "SQZ-DYN-UPD shadow blocked: same-direction cluster cap "
            f"{recent_same_direction}/{max_same_direction_trades} within {window_minutes}m."
        )
    return None


def _shadow_strategy_loss_control_reason(
    *,
    signal: Signal,
    trades: list[dict[str, Any]],
    window_hours: int,
    min_closed_trades: int,
    max_total_r: Decimal,
    max_loss_count: int,
    loss_count_max_total_r: Decimal,
) -> str | None:
    strategy = _signal_strategy(signal)
    if not strategy:
        return None
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(1, int(window_hours)))
    closed: list[dict[str, Any]] = []
    for trade in trades:
        if str(trade.get("strategy") or "").upper() != strategy:
            continue
        if _trade_status(trade) != "CLOSED":
            continue
        closed_at = _parse_datetime_utc(trade.get("closed_at") or trade.get("created_at"))
        if closed_at is None or closed_at < cutoff:
            continue
        closed.append(trade)

    if len(closed) < max(1, int(min_closed_trades)):
        return None

    total_r = sum((_decimal_or_zero(trade.get("r_multiple")) for trade in closed), Decimal("0"))
    losses = sum(
        1
        for trade in closed
        if _decimal_or_zero(trade.get("r_multiple")) < 0 or _decimal_or_zero(trade.get("realized_pnl")) < 0
    )
    if total_r <= max_total_r:
        return (
            f"{strategy} shadow loss-control: {len(closed)} closed trades in {window_hours}h "
            f"total R {total_r:.2f} <= {max_total_r:.2f}; pausing virtual entries."
        )
    if losses >= max(1, int(max_loss_count)) and total_r <= loss_count_max_total_r:
        return (
            f"{strategy} shadow loss-control: {losses} losses in {window_hours}h "
            f"with total R {total_r:.2f}; pausing virtual entries."
        )
    return None


def _target_liquidity_distance_bps(signal: Signal, order_flow: dict[str, Any]) -> Decimal | None:
    key = "distance_to_upper_liquidity_bps" if signal.direction == Direction.LONG else "distance_to_lower_liquidity_bps"
    return _optional_decimal(order_flow.get(key))


def _trade_closed_today_utc(value: object) -> bool:
    if value is None:
        return False
    created = _parse_datetime_utc(value)
    if created is None:
        return False
    return created.astimezone(timezone.utc).date() == datetime.now(timezone.utc).date()


def _symbol_loss_cooldown_reason(symbol: str, trades: list[dict[str, Any]], cooldown_minutes: int) -> str | None:
    if cooldown_minutes <= 0:
        return None
    now = datetime.now(timezone.utc)
    for trade in trades:
        if trade.get("symbol") != symbol or trade.get("status") != "CLOSED":
            continue
        pnl = _decimal_or_zero(trade.get("realized_pnl"))
        if pnl >= 0:
            return None
        closed_at = _parse_datetime_utc(trade.get("closed_at") or trade.get("created_at"))
        if closed_at is None:
            return None
        elapsed_min = (now - closed_at).total_seconds() / 60
        if elapsed_min < cooldown_minutes:
            remaining = max(1, int(cooldown_minutes - elapsed_min))
            return f"{symbol} cooldown after loss is active for ~{remaining} more minutes."
        return None
    return None


def _parse_datetime_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _date_from_iso_like(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw[:10] if len(raw) >= 10 else None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")
