# SQZ same-level confirmation prospective experiment — v1, 2026-10-05

Compare confirmation policies on the unchanged SQZ source in a new cohort.
An expansion of Bollinger bands outside Keltner channels is not necessarily a
price breakout of the compression range. A saved hourly retest flag can therefore
coincide with the first actual price breakout. This is a mechanical discrepancy;
correcting its interpretation does not establish profitability.

## Frozen protocol

- Cohort: sqz-confirmation-v1-20261005; original fixed 32 symbols, no PnL selection.
- CONFIRM_SOURCE_2R: all valid sources after unchanged generator/liquidity floors.
  A diagnostic source control supplies paired outcomes for policy refusals.
- CONFIRM_LEGACY_2R: (saved 1h retest OR unchanged strict strong-release exception)
  AND corrected 15m structure_break_aligned reason. This compares the former
  confirmation pair, not the full production admission stack.
- CONFIRM_LEVEL_2R: latest closed 1h and 15m closes must hold the same compression
  level; require an earlier closed 15m price breakout followed by a distinct
  closed 15m retest, OR the same strict strong-release exception.
- Level is recomputed with the unchanged generator's compression range from 1h
  bars before the release/build anchor. Require exact match to source metadata.
  A build anchor is the current hour; no release is fabricated. Retest search
  starts at the anchor's opening time, with the configured five-hour lookback.
- Price breakout is a close beyond that level in the source direction. A later
  bar must touch the level's configured 0.25 ATR band and close on the breakout
  side with directional body at least 0.10 ATR OR rejection wick at least 1.5
  times max(body, 0.10 ATR). The first price breakout cannot be its own retest.
  A later close at or through the level resets the sequence; a fresh price
  breakout needs a fresh later retest. Thresholds are not fitted to outcomes.
- The strong-release exception is identical in both policies: release state,
  release_followthrough timing, breakout >=1.50 ATR, corrected flow aligned,
  score >=0.72, no risk flags. Treatment still requires latest closes holding
  the level. No separate structure reason is required by treatment.
- ATR is the current closed 1h ATR known at observation. Sequence evaluation is
  retrospective only within already closed frames available at that observation;
  neither ATR nor any future bar is used to enter on a historical breakout bar.
  Non-finite prices, incoherent OHLC, gaps, partial/future/stale bars, malformed
  flow or unmatched level fail closed in every arm; keep failures as evidence.
- Source identity uses actual closed 1h time. First observation only; no retry
  after refusal, occupancy or a retest appearing later in that same hour.
  A new closed hour is a different source. Keep all refusals and capacity skips.
- No direction gate, RS bypass or additional OF admission is added. Record
  existing annotations unchanged. Source, stop, exits and execution are common:
  next-minute open, single 2R, 2 virtual USDT risk before costs, 24h maximum;
  4bps fees and 5bps adverse slippage per side, estimated funding. Cost-aware
  sizing is not part of this experiment. Shared symbol occupancy pairs sources;
  identical accepted sources have identical plans. Do not sum paired copies.
- First descriptive review: at least 50 closed source-control entries AND 14
  days. Report net outcomes, drawdown, entry counts, prevented losses and missed
  profits on the same sources; LONG/SHORT and regime slices. Count open sources
  separately and include entries aged 24h. Coins/hourly sources can be correlated;
  this is neither statistical proof nor approval to trade real money.
- At review, replay retained minute bars offline with 10bps adverse slippage per
  side instead of 5bps, recomputing fills, targets and sizing. No flat deduction
  or threshold refit. Report insufficient coverage. Historical replay diagnoses
  policy disagreements only; it is not prospective portfolio performance.

## Operation

python -m trading_bot.research.confirmation_paper --config config.yaml
--settings confirmation_paper_settings.json --data-dir /absolute/new/directory

Own service sqz-confirmation-paper, directory /root/bot_sqz_confirmation_paper.
Unsigned public GET only, local paper execution, no credentials/orders/ports.
Freeze code/config/settings hashes. Preserve all earlier cohorts, including
the separate direction test; do not combine their conditions in this test.
Scan every 300s and monitor every 30s. CPUQuota 5%, MemoryHigh 80MiB,
MemoryMax 128MiB, MemorySwapMax 32MiB; existing disk/database guards retained.
Probe runs --once in a separate data directory before activation. Compare prior
services' PIDs/restarts, code/config hashes and timer state before/after.
Rollback stops/disables only sqz-confirmation-paper and preserves its database.
Automatic chat notifications are not configured. Cost-aware sizing is a later,
separately registered experiment.
