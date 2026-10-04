# SQZ direction / 4h-regime prospective experiment — v1, 2026-10-05

Compare the unchanged SQZ source with a single direction gate in a new cohort.
SHORT in TREND_UP and LONG in TREND_DOWN are rejected only in treatment.
Every other valid regime label retains baseline admission (including UNKNOWN).
Missing or malformed labels/directions and non-SQZ sources fail closed in both.
Source metadata is never rewritten. This is a hypothesis, not a profitability fix.

## Frozen protocol

- Cohort: sqz-direction-v1-20261005; original fixed 32 symbols, no PnL selection.
- DIRECTION_BASELINE_2R: all valid sources after the existing liquidity floors.
- DIRECTION_ALIGNED_2R: same source baseline with only COUNTERTREND_4H rejection.
- Neither arm uses additional strict OF/RS/retest admission; those annotations
  are retained as evidence. This explicitly compares source baseline admission,
  not production admission. No earlier BTC RS or early-retest fix is combined.
- Source generator, stop/targets, costs and exits unchanged; hourly first source
  from the actual closed 1h frame, no retry after refusal/occupancy. Shared symbol
  occupancy pairs entries and prevents reusing capacity freed by one arm.
- Same next-minute entry, single 2R, risk 2 virtual USDT before costs, 24h maximum.
  4bps fees and 5bps adverse slippage per side; funding remains an estimate.
  Identical accepted sources have identical plans. Risk after costs is not capped
  at 2 USDT; a separate cost-sizing change is not part of this experiment.
- Record all first decisions, including excluded sources and occupancy skips.
  Entry counts, net outcomes, drawdown, LONG/SHORT and regime slices; hourly
  sources and different coins can remain correlated. Do not sum paired copies.
- First descriptive review: at least 50 closed control sources AND 14 days.
  This is not statistical proof or approval for real-money execution. Keep open
  outcomes separate and include entries aged 24h to avoid early-exit bias.
- At review, stress the same retained minute bars offline with 10bps adverse
  slippage per side instead of 5bps, recomputing fills/targets/sizing. Do not
  merely subtract a flat amount or refit rules. Report insufficient coverage.
- No new retest/structure, RANGE-only, exit, sizing or threshold selection.
  Historical positive and negative countertrend examples motivated this test;
  neither is treated as prospective evidence.

## Operation

python -m trading_bot.research.direction_paper --config config.yaml
--settings direction_paper_settings.json --data-dir /absolute/new/directory

Own service sqz-direction-paper, directory /root/bot_sqz_direction_paper.
Public unsigned GET only, local paper execution, no credentials/orders/ports.
Code/config/settings hashes frozen; original cohorts never receive this package.
Scan every 300s, monitor every 30s. CPUQuota 5%, MemoryHigh 80MiB,
MemoryMax 128MiB, MemorySwapMax 32MiB; existing storage guards retained.
Probe uses --once and a separate directory. Rollback: stop/disable only
sqz-direction-paper and preserve its database. Monitoring is provided by the
service; automatic chat notifications are not configured.

The earlier baseline evaluation protocol remains separate. Confirmation logic
and cost-aware risk sizing will require separate registered experiments, rather
than changing this already frozen single-factor comparison.
