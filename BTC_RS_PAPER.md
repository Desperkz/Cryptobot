# BTC self-benchmark RS applicability — v1, 2026-09-24

BTC compared with the same BTC 4h candles has zero relative strength, including
Decimal rounding noise. Requiring aligned RS in that case is unreachable. This
study marks that particular check not applicable; it does not invent alignment,
change source annotations or replace RS with a new momentum/market threshold.

## Frozen prospective experiment

- BTCUSDT only; unchanged SQZ source generator and preliminary liquidity floors.
- BTC_RS_STRICT_2R: existing strict gate vector using corrected order flow,
  with structural safety and configured context checks retained.
- BTC_RS_NOT_APPLICABLE_2R: identical vector, removing ONLY RS_NEUTRAL after
  verifying finite self-benchmark values and neutral annotation. Difference
  tolerance 1e-24 accommodates Decimal arithmetic, not market movements.
- Missing, nonfinite, inconsistent or non-neutral RS fails closed in both arms.
  A scan crossing a 4h boundary can compare different BTC frames: no bypass then.
  Non-BTC sources cannot use this experiment. Alts retain their existing rules.
- Both arms preserve hostile/weak/adverse flow, absorption, structural risks,
  structure-break, retest/strong-release and context checks. No early-retest
  exception from the separate early cohort is combined with this change.
- Source metadata remains neutral; applicability and the removed requirement
  appear in the separate decision evidence. The corrected flow control is an
  explicit comparator, not the legacy production path with its paper overrides.

## Counting and execution

First observation per symbol/direction/actual closed hour, no later retry after
rejection or occupancy. Shared symbol occupancy across arms. Both use the existing
next-minute entry, original stop, single 2R target, 2 virtual USDT risk before
costs, 4bps fees and 5bps slippage each side, maximum 24h. Funding is an estimate.
Strict control is expected to have no entries for a valid self-comparison; it
checks reachability. The treatment may still have none due to other conditions.
More entries do not establish edge; overlapping hourly sources remain correlated.

## Isolation and operation

`python -m trading_bot.research.btc_rs_paper --config config.yaml
--settings btc_rs_paper_settings.json --data-dir /absolute/new/directory`

`--once` uses a separate probe directory. Frozen code/config/settings hashes;
existing cohorts never receive these files. Separate service `btc-rs-paper`,
directory `/root/bot_btc_rs_paper`, own database/status.json. Public unsigned
GET client only; no keys, orders, account requests, notifications or ports.
Scan every 300s; monitor every 30s. CPUQuota 5%, MemoryHigh 80MiB,
MemoryMax 128MiB, MemorySwapMax 32MiB. Existing database/disk guards retained.
Rollback: stop/disable only btc-rs-paper; preserve its evidence database.

Historical replays are diagnostics, not prospective validation. Evaluate new
sources after actual start, keeping every refusal and occupancy skip. Do not
relax remaining gates just to make the historical BTC SHORT pass. Any later
flow/structure or exit change requires a separately registered experiment.
