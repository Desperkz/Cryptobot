# Prospective demo-to-mainnet conditional transfer and cost sizing — 2026-10-05

The PROMOTE_TO_PAPER dashboard badge is advisory. Its generic gate counts shadow
rows, while strategy-specific policy and independent-source review remain
separate. Existing positive conditional buckets were generated on demo-fapi data.
They are not a validated public-mainnet edge. Their original shadow modes and
frozen cohorts remain unchanged. This is an isolated transfer-validation pilot,
not formal promotion into the core paper portfolio or permission for real orders.

## Frozen protocol

- Cohort sqz-promotion-transfer-v1-20261005; original fixed 32 public-mainnet
  instruments and unchanged liquidity floors. Do not select coins using profits.
- Source: unchanged SqueezeBreakoutStrategy wrapped with the existing router's
  SQUEEZE_BREAKOUT_DYNAMIC_UPD metadata. Retain original legacy OF annotation for
  conditional score rules, corrected OF and relative strength as separate inputs.
  Reuse existing v1/v2 score functions and thresholds without retuning. Public
  OI history is available here; missing-OI demo evidence does not transfer directly.
- PROMOTE_SOURCE_2R admits all valid sources; PROMOTE_V1_HIGH_2R and
  PROMOTE_V1_MID_2R select their existing v1 buckets; PROMOTE_C2_MID_2R selects
  the existing v2 MID bucket. C2 HIGH stays in its original shadow study and is
  not promoted by this pilot. LOW is retained in source evidence, not selected
  as a prospective candidate. Reject malformed inputs in all arms.
- All price inputs must be closed, contiguous and fresh. Actual closed 1h
  identity; first observation only, no retries/backdating or later bucket changes.
  Shared symbol occupancy across all arms. Store every decision/refusal/skip.
- Common next-minute execution, original stop, single 2R, 24h maximum and risk
  budget 2 virtual USDT. This common exit differs from original shadow partial
  exits; results measure transferable source/bucket selection under a declared
  model. They cannot reproduce or be merged with old shadow portfolio PnL.
  No new direction/retest condition from the other isolated studies is combined.
- Third step: for each source compare STOP_ONLY versus STOP_PLUS_EXPENSES sizing
  at the same fills/targets. The latter denominator includes actual modeled
  entry-stop distance, exit slippage at the original stop, 4bps entry/exit fees
  and 24h reserve for nonnegative adverse entry-time funding (3 eight-hour
  periods). Missing funding uses the established adverse 1bp/8h buffer. Favorable
  funding does not enlarge risk. Entry slippage is already in stop distance;
  do not add it twice. No quantity rounding/leverage/real execution is modeled.
- Execute both sizing policies at 5bps adverse slippage per side and repeat
  full same-minute-bar simulation at 10bps, recomputing entry, stop distance,
  targets and sizing. No flat PnL deduction. Conservative minute-OHLC ordering
  remains identical to the original execution model. Gaps can exceed the estimated
  budget; changing actual fees/funding/fills are not guaranteed by this reserve.
- Before/after quantities differ only in sizing. Store separate cost comparisons
  in each position result. They are paired counterfactuals, not four independent
  trades. Do not sum them or sum overlapping v1/v2 arms. Retain minute coverage
  after an original exit when stress execution still needs it, until 24h.
- Historical source/cost audits are diagnostic only, without refitting. The
  earlier original baseline, direction and confirmation protocols remain frozen.
- Review after at least 14 days AND 50 closed prospective source-control entries;
  formal candidate discussion additionally requires at least 50 closed entries
  in that candidate arm. Report all refusals, opens and 24h-aged entries, net
  results, drawdown, cost stress and LONG/SHORT/regime/time slices. Common events
  and repeated hours/coins can still be correlated. No automatic promotion.

## Operation and isolation

python -m trading_bot.research.promotion_paper --config config.yaml
--settings promotion_paper_settings.json --data-dir /absolute/new/directory --once

Use one new oneshot service/timer sqz-promotion-paper and /root/bot_sqz_promotion_paper, including
all cost comparisons; no extra service per arm. Public unsigned GET only, no
credentials or order capability. Use a frozen copy of the existing mainnet-lab
config, whose original score profiles were replay-verified against saved demo
metadata; no live config is changed. Own DB, frozen package/config/settings,
CPUQuota 5%, MemoryHigh 64MiB, MemoryMax 96MiB, MemorySwapMax 32MiB, Nice 19,
write only own data. A serialized --once cycle starts approximately every 300s;
scan and monitor run once per cycle, releasing memory after completion. Monitor
cadence is 300s rather than the earlier permanent labs' 30s and is explicit in
the manifest; conservative minute-bar execution remains common to all new arms.
Cycles can be delayed by runtime/resource pressure. ExecCondition defers a start
if MemAvailable <64MiB or SwapFree <256MiB; it never stops an old process.
Stop/disable only this timer and stop its service for rollback; preserve its DB.
Before/after audit prior nine services and all old research hashes; probe a
separate directory before activation. No scheduled chat notifications.
