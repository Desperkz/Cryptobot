# Bot v2.1 Live Readiness Roadmap

Last updated: 2026-05-16

Status legend:
- `[x]` done and locally verified
- `[~]` in progress
- `[ ]` pending
- `[!]` blocked or requires manual/VPS action

## Policy

- Bot 2.0 stays online while v2.1 is in `PAPER_TRADING`.
- Before real money, v2.1 must be the only bot allowed to trade on the live account.
- No `MAINNET_LIVE` deployment is allowed without a fresh paper/testnet evidence pack and a human unlock file.
- `SQUEEZE_BREAKOUT` is the current champion strategy for v2.1.
- `MEAN_REVERSION` is a paper candidate because bot 2.0 evidence is strong; it still needs separate v2.1 paper/testnet evidence before any live promotion.
- All new strategies are candidates only until they pass separate paper/testnet evidence gates.
- New strategies must run with separate metrics and must not be mixed into live trading simply because they exist in code.

## P0 Release Gates

- `[x]` P0-01 Prevent accidental mainnet start while old bot services are active.
  - Done: `MAINNET_LIVE` validation now checks systemd and blocks if `trading-bot` or `trading-bot-v2` are active.
  - Bot 2.0 can still stay online while v2.1 is in `PAPER_TRADING`.
- `[x]` P0-02 Complete live order lifecycle: idempotent entry, SL, TP ladder, reduce-only exits, partial fills, and defensive cleanup.
  - Done: live orders now share one `trade_id`; entry, SL, TP, breakeven, trailing, and cleanup orders use unique client order IDs.
  - Done: order submission now queries Binance by `clientOrderId` after ambiguous Binance errors instead of blindly retrying.
  - Done: live entry fill quantity no longer treats `origQty` as executed quantity unless the recovered order is explicitly `FILLED`.
  - Pending verification is tracked under P3 testnet/chaos tests.
- `[x]` P0-03 Reconcile remote Binance positions/orders with local database on start and during runtime.
  - Done: live startup now requires a Binance position/order sync before trading can continue.
  - Done: every live cycle refreshes local positions and `trades` rows from Binance position risk/open orders.
  - Done: DB rows for live positions absent on Binance are marked `CLOSED`.
- `[x]` P0-04 Make user data stream a hard live dependency and add REST fallback when stale.
  - Done: live start already waits for websocket connection before trading.
  - Done: runtime entry gate now requires connected user stream, while stale event flow triggers REST reconciliation backup.
  - Done: quiet-but-connected user stream no longer blocks entries solely because no account/order events arrived.
- `[x]` P0-05 Handle unknown Binance order status without duplicate orders.
  - Done: order submission recovery queries Binance by `clientOrderId` after ambiguous API errors.
  - Done: duplicate prevention relies on stable per-order client IDs inside one `trade_id`.
- `[x]` P0-06 Upgrade emergency stop for live: block entries, cancel open orders, optional reduce-only close.
  - Done: emergency flag still blocks new cycles/entries.
  - Done: in live, first emergency detection cancels all open Binance orders.
  - Done: live position closing is controlled by `safety.emergency_close_positions_in_live` and defaults to `false`.
- `[x]` P0-07 Add rate-limit/backoff coverage for 429, 418, -1008, timeouts, and 503 edge cases.
  - Done: 429/418 respect `Retry-After` where provided.
  - Done: Binance `-1008` overload is treated as retryable with backoff.
  - Done: 503 unknown order status is surfaced to order recovery, not blindly retried.
- `[x]` P0-08 Lock down dashboard/control API before live.
  - Done: public dashboard remains allowed for paper via `BOT_ALLOW_UNSAFE_PUBLIC=1`.
  - Done: if config mode becomes `MAINNET_LIVE`, public control API refuses to start without `BOT_CONTROL_TOKEN`.
  - Done: control API now uses a threaded HTTP server with per-connection timeout so slow or malformed public connections cannot freeze the dashboard/API.

## P1 Strategy And Risk

- `[x]` P1-01 Run live only with `SQUEEZE_BREAKOUT` until fresh evidence supports more strategies.
  - Done: `MAINNET_LIVE` validation rejects strategy lists outside `safety.mainnet_allowed_strategies`.
  - Done: live allow-list currently contains only `SQUEEZE_BREAKOUT`.
- `[x]` P1-02 Add live-safe risk profile: 0.25% initial risk, 1-2x leverage, max 1 concurrent position.
  - Done: `MAINNET_LIVE` validation caps risk per trade, leverage, and concurrent positions.
  - Done: config live safety profile is set to 0.25% max risk, 2x max leverage, and 1 max live position.
- `[x]` P1-03 Add per-symbol cooldown after stop-loss.
  - Done: recently losing symbols are rejected before signal persistence/trade execution.
  - Done: default symbol loss cooldown is 120 minutes.
- `[x]` P1-04 Track fees, slippage, and partial TP behavior against paper assumptions.
  - Done: paper exits now apply taker fee and slippage assumptions to full and partial closes.
  - Done: partial close metadata stores paper cost assumptions for later review.
- `[x]` P1-05 Add symbol quality score and block noisy/low-liquidity symbols faster.
  - Done: universe builder now scores each symbol by spread, top-book liquidity, and 24h quote volume.
  - Done: symbols below `universe.min_symbol_quality_score` are skipped before strategy scans.
- `[x]` P1-06 Auto-reduce position size when the margin cap would otherwise reject a valid signal.
  - Done: risk sizing now caps quantity to remaining `risk.max_margin_usage_pct` capacity instead of rejecting the whole trade.
  - Done: capped plans carry a `Position size was capped by max margin usage.` warning and still honor min quantity/min notional checks.
- `[x]` P1-07 Block duplicate re-entries and cluster repeated trade series.
  - Done: regular entries are blocked when the same symbol+strategy already has an active trade.
  - Done: same symbol+strategy re-entry has a configurable cooldown, default `45` minutes.
  - Done: scale-in is an explicit config mode and remains disabled by default; add-ons cannot happen accidentally.
  - Done: accepted trades now carry cluster metadata, and scorecard gates count repeated series as one closed trade cluster.

## P2 Machine Learning

- `[x]` P2-01 Keep ML in shadow mode until at least 500 closed v2.1 trades.
  - Done: ML still defaults to disabled in config.
  - Done: even if enabled accidentally, ML decisions fail open until the loaded model has at least `ml.decision_min_trades` rows.
  - Done: default decision threshold is 500 rows/trades.
- `[x]` P2-02 Store feature snapshots for every accepted/rejected signal.
  - Done: new `ml_feature_snapshots` table records signal features, metadata, decision, and reason.
  - Done: entry-filter, ML, cooldown, risk, order, and accepted-trade outcomes now write snapshots.
- `[x]` P2-03 Add walk-forward ML validation report.
  - Done: `trading_bot.cli.app ml-validate` builds an expanding-window walk-forward report from closed trades.
  - Done: report is written to `data/ml/walk_forward_report.json` and printed as JSON.
- `[x]` P2-04 Compare baseline strategy vs ML-filtered strategy before enabling ML decisions.
  - Done: walk-forward report now includes baseline vs ML-filtered total R, average R, trade coverage, and improvement flag.
  - Done: `MAINNET_LIVE` refuses to start with `ml.enabled=true` unless the validation report shows ML is not worse than baseline and keeps enough trade coverage.

## P3 Verification

- `[~]` P3-01 Unit tests for live safety gates, order IDs, duplicate prevention, and ML shadow/validation gates.
  - Local syntax check passed with `compileall`.
  - VPS public health check passed after deploy: `/status` active and `/db` HTTP 200.
  - VPS migration check passed via downloaded DB: `ml_feature_snapshots` table exists.
  - VPS runtime check passed for one cycle: fresh universe scan logged after deploy.
  - Data restore corrected: v2.1 historical closed HBAR trade was restored from `/root/bot_v2_1_prev_20260512_113908`.
  - Pending: deploy paper-monitor fix that preserves original quantity on fully closed partial trades, then repair restored `quantity=0` row.
  - VPS verification script is prepared in `verify_bot_v2_1.cmd`; awaiting remote pytest result.
- `[ ]` P3-02 Integration tests on Binance demo/testnet for entry, SL, TP, cancel, and restart recovery.
- `[ ]` P3-03 Chaos tests: VPS reboot, Binance timeout, network loss, stale user stream.
- `[ ]` P3-04 14-day paper/testnet soak with zero technical incidents.
- `[~]` P3-05 A/B benchmark against bot v2.0 before live promotion.
  - Prepared: `D:\Codex\compare_bot_v2_vs_v2_1.cmd`.
  - Prepared: `D:\Codex\vps_downloads\compare_bot_v2_vs_v2_1.sh`.
  - Scope: read-only DB comparison, realized/unrealized PnL, winrate, profit factor, average R, max drawdown, trade frequency, symbol/strategy breakdown, open-position risk.
  - Purpose: v2.0 remains the champion benchmark; v2.1 must prove better risk-adjusted performance before MAINNET_LIVE.
  - Quick public API snapshot saved: `D:\Codex\outputs\v2_vs_v2_1_public_compare_report.txt`.
  - Snapshot result: v2.0 leads realized PnL and trade count; v2.1 has similar average R but lower trade frequency.
  - Adjustment prepared: high-confidence `SQUEEZE_BREAKOUT` release signals may override the UTC 0-1 avoided-session filter.
  - Adjustment prepared: universe widened from top 30/50 to top 40/80 and `min_symbol_quality_score` relaxed from 70 to 60 while preserving minimum volume, spread, and order-book liquidity filters.
  - Local verification: `22 passed`.
  - Deployed on VPS: 2026-05-13 10:57 UTC.
  - Post-deploy public checks: v2.1 `active`, paper monitor `active`, v2.0 still `active`.
  - Post-deploy universe includes broader candidates such as `ASTERUSDT`, `MUSDT`, `SKYUSDT`, `ETCUSDT`, `QNTUSDT`, `POLUSDT`, `KASUSDT`, and `RENDERUSDT`.
  - No new post-deploy UTC rejection observed yet; old `ZECUSDT` UTC rejections remain historical evidence from before deployment.
  - Daily public API report prepared: `D:\Codex\daily_ab_report_v2_vs_v2_1.cmd`.
  - Daily report outputs timestamped JSON/TXT under `D:\Codex\outputs\ab_reports`, updates `v2_vs_v2_1_daily_latest.*`, and appends `v2_vs_v2_1_daily_history.csv`.
  - Latest daily report result: v2.0 leads realized PnL and sample size; v2.1 has higher winrate but only 3 closed trades.

## P4 Strategy Expansion

- `[x]` P4-01 Per-strategy scorecard foundation.
  - Done: `/strategy-scorecard` returns closed trades, open trades/risk, realized/unrealized PnL, winrate, profit factor, average R, max drawdown, trade frequency, symbol/direction breakdown, and rejection reasons by strategy.
  - Done: dashboard now shows the strategy scorecard as a separate table.
  - Done: local calculation tests added; `24 passed`.
  - Done: automated per-strategy gates now report `PROMOTABLE`, `WATCH`, or `BLOCKED` from min trades, sample age, trade frequency, winrate, profit factor, average R, and drawdown.
  - Done: strategy runtime modes now split `live`, `paper`, `shadow`, and `disabled`; current paper execution is `SQUEEZE_BREAKOUT` plus `MEAN_REVERSION`, while `TREND_PULLBACK` is shadow-only.
  - Done: scorecard now reports raw trades plus cluster-adjusted counts/Win/PF/Avg R so repeated fast entries on one coin do not inflate strategy evidence.
  - Gate: no strategy can be promoted without its own scorecard and a passing gate.
- `[x]` P4-02 Improve `SQUEEZE_BREAKOUT` as the champion strategy.
  - Keep it as the live allow-list leader.
  - Continue tuning universe breadth, UTC override, squeeze release detection, volume confirmation, and partial exits.
  - Target: higher trade frequency without reducing positive average R.
  - Done: added an SQZ champion quality gate: release follow-through can be caught for more bars, but entries must break out of the compression range, avoid late overextension, and use the strongest volume confirmation in the release window.
  - Done: early `build` entries remain possible only as stricter `early_breakout` setups with longer squeeze duration, stronger volume, and structural breakout confirmation.
  - Done: SQZ signals now carry `squeeze_entry_timing`, `breakout_atr`, `compression_high/low`, and `squeeze_release_offset` metadata so scorecard/log review can separate clean releases from early breakouts.
  - Verification: `62 passed`.
- `[x]` P4-02A Add retest confirmation to late `SQUEEZE_BREAKOUT` follow-through entries.
  - Done: late release follow-through entries now require a retest/rejection or absorption touch of the compression breakout level.
  - Done: immediate release entries are not forced to wait for a retest, so the champion strategy is not silenced.
  - Done: SQZ metadata now includes `squeeze_retest_required`, `squeeze_retest_confirmed`, retest level, bars ago, rejection type, and body size in ATR.
  - Verification: SQZ candidate tests cover late follow-through with and without retest.
- `[x]` P4-03 Rework `MEAN_REVERSION` as a paper candidate, not a live strategy.
  - Done: promoted from `shadow` to `paper` for v2.1 paper trading after bot 2.0 evidence showed strong MR contribution.
  - Done: `MAINNET_LIVE` remains protected because paper-mode strategies do not execute in mainnet and the live allow-list still contains only `SQUEEZE_BREAKOUT`.
  - Done: added stricter exhaustion rules: RSI extreme, ATR deviation, divergence, volume confirmation, reversal wick/absorption/sweep confirmation, and confluence scoring.
  - Done: added `MR_CONTEXT` gate before paper entry; MR is blocked when BTC 4h impulse is strongly against the trade or order-flow/liquidation context is hostile.
  - Done: `/strategy-scorecard` now summarizes candidate evidence from shadow signals: average confidence, average confluence, divergence, volume, edge, and reversal counts.
  - Require separate v2.1 paper/testnet evidence before enabling beyond paper observation.
- `[x]` P4-04 Add `TREND_PULLBACK` candidate.
  - Goal: enter on pullbacks inside a strong trend, not at random highs/lows.
  - Expected use: catch moves that squeeze misses after the trend is already active.
  - Done: added as `shadow`; requires higher-timeframe trend, controlled pullback depth, continuation candle, volume, order-flow alignment, and confluence evidence.
- `[x]` P4-04A Add shadow-paper simulation for candidate strategies.
  - Done: shadow strategies now open virtual trades in a separate `shadow_trades` table with entry, SL, TP, risk, PnL, and R.
  - Done: `paper_monitor_v2.py` closes shadow-paper trades by SL/TP without touching regular paper trades, balance, or SQZ PnL.
  - Done: `/strategy-scorecard` and `/shadow-trades` expose separate shadow-paper metrics.
  - Done: shadow-paper sizing now uses compounding paper equity and the same margin-cap sizing rules as regular paper entries when symbol filters are available.
- `[x]` P4-05 Add shadow candidate pack for strategy discovery.
  - Done: added `LIQUIDITY_SWEEP_REVERSAL`, `VWAP_REVERSION`, `MOMENTUM_CONTINUATION`, and cautious `RANGE_GRID` as `shadow` strategies.
  - Done: all new strategies write regular shadow signals and shadow-paper trades only; they do not affect main paper/live PnL.
  - Done: `RANGE_GRID` is explicitly marked as cautious shadow-only research.
- `[x]` P4-06 Add funding/carry filter as an enhancer, not a standalone strategy.
  - Goal: avoid entries against extreme funding pressure and optionally prefer direction with favorable carry.
  - Gate: cannot override risk rules or force entries alone.
  - Done: funding carry can penalize or block candidate signals when funding is strongly adverse to direction.
- `[x]` P4-07 Research order-flow/liquidation strategy candidate.
  - Use liquidation zones, open interest change, taker flow, and sweep/absorption features.
  - Start as signal annotation and rejection/acceptance evidence, not live execution.
  - Done: added research-only `ORDER_FLOW_ANNOTATION` snapshots for paper and shadow signals.
  - Done: annotations score flow alignment, liquidation-zone proximity, taker flow, order-book imbalance, aggressive delta, open-interest expansion/drop, funding crowding, sweep, absorption, and structure-break context.
  - Done: `/order-flow` exposes recent annotations and aggregate alignment/risk summaries.
  - Done: `/strategy-scorecard` and dashboard rows include compact order-flow evidence without changing execution allow-lists.
- `[x]` P4-08 Strategy selector / portfolio allocator.
  - Once several candidates have evidence, allocate by recent strategy expectancy, drawdown, and market regime.
  - Until then, no automatic switching in live.
  - Done: added advisory-only `/strategy-allocator`; it ranks paper/shadow strategies by expectancy, PF, drawdown, and maturity.
  - Done: dashboard scorecard now shows `Alloc` with suggested risk weight and action (`CORE`, `CHAMP`, `PROMOTE`, `WATCH`, `RESEARCH`, `REDUCE`).
  - Done: allocator is explicitly `ADVISORY_ONLY`; it does not switch strategy modes or enable live execution.
- `[ ]` P4-09 ML meta-filter remains shadow-only.
  - ML should filter/score strategy signals, not invent trades.
  - Mainnet ML requires at least 500 validated rows and a walk-forward report not worse than baseline.
- `[x]` P4-10 Shadow promotion assistant.
  - Done: scorecard now includes `Shadow Gate` with `TESTING`, `WATCH`, and `PROMOTE`.
  - Done: dashboard shows a `PROMOTE TO PAPER` badge when a shadow strategy passes the shadow-paper gate.
  - Done: `/strategy-promotions` returns promotion candidates for human review.
- `[x]` P4-11 Retune losing shadow candidates and restart evidence collection.
  - Done: first shadow-paper drawdown scan found `LIQUIDITY_SWEEP_REVERSAL`, old `VWAP_REVERSION`, `MOMENTUM_CONTINUATION`, and `TREND_FOLLOWING` had negative early expectancy.
  - Done: those strategies are back in `shadow` after retuning; `SQUEEZE_BREAKOUT` and `MEAN_REVERSION` paper modes are unchanged.
  - Done: `LIQUIDITY_SWEEP_REVERSAL` now requires stronger sweep reclaim, volume, edge, absorption, and directional flow.
  - Done: `VWAP_REVERSION` now requires a larger VWAP stretch, real progress back toward VWAP, adequate ATR, and stronger volume.
  - Done: `MOMENTUM_CONTINUATION` now blocks overextended/too-volatile breakouts and requires stronger edge/volume/flow.
  - Done: `TREND_FOLLOWING` now requires stronger edge, volume, and minimum ATR before shadow entries.
  - Prepared: `D:\Codex\archive_quarantined_shadow_stats.cmd` archives old losing shadow rows on VPS so the retuned strategies restart from a clean baseline.
- `[x]` P4-12 Expose shadow-paper rejects in the dashboard rejection journal.
  - Done: `/rejections` now merges hard `filter_rejections` with shadow-paper `SHADOW_PAPER_REJECTED_RISK` and `SHADOW_PAPER_REJECTED_COOLDOWN` snapshots.
  - Done: `/rejection-stats` counts `SHADOW_RISK` and `SHADOW_COOLDOWN` so the journal no longer looks silent while shadow strategies are being filtered.
  - Done: dashboard badges/styles now render shadow rejection types separately.

## Evidence Required Before MAINNET_LIVE

- `[ ]` At least 500 closed v2.1 paper/testnet trades.
- `[ ]` At least 100 closed paper/testnet trades for every strategy enabled in live.
- `[ ]` Profit factor >= 1.25.
- `[ ]` Winrate >= 40%.
- `[ ]` Positive average R.
- `[ ]` Max drawdown better than -10%.
- `[ ]` v2.1 beats or clearly matches v2.0 on risk-adjusted A/B metrics, not only headline PnL.
- `[ ]` No enabled live strategy has negative average R or materially worsens portfolio drawdown.
- `[ ]` Zero duplicate orders/positions.
- `[ ]` Zero unprotected live/testnet positions after restart.
- `[ ]` Human-reviewed `data/production_unlock.json`.
