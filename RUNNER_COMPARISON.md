# Paired runner exit comparison — 2026-10-06

This study measures whether actual future SQZ paper entries leave more net profit
when their final 40% is followed by the existing stop instead of fixed targets.
It does not change the core bot, its positions, risk, exit profile or any frozen
mainnet pilot. No real orders, credentials, new entry filters or increased risk.

## Registered comparison

- Read `/root/bot_v2_1/data/trading_bot_v2_1.sqlite3` with SQLite `mode=ro` and
  `query_only`. Register the maximum source ID and UTC timestamp before collection.
  Existing trades, including positions already open, are excluded from this cohort.
- Admit only later `PAPER_TRADING` SQZ rows with the exact saved profile
  `TP1:1.0R@0.25/BE|TP2:1.6R@0.35/TR|RUNNER:2.2R@0.4/TR`.
  Retain refusal reasons for changed profiles, other strategies or malformed inputs.
  Preserve actual effective entry, initial stop, all original target quantities,
  risk budget, direction, regime, funding annotation and source identity.
- `CURRENT_PROFILE`: the saved targets and final whole-position target.
  `RUNNER_TRAILING`: the same TP1/TP2, BE, trailing and initial stop; remove the fixed
  RUNNER target and the final whole-position target so the remaining volume can run.
  Neither arm increases quantities or risk. Earlier BE is unchanged.
- Both arms use the same complete contiguous one-minute candles from the main
  monitor's venue (currently demo-fapi). These are public unsigned GET requests.
  Never substitute mainnet prices for demo inputs. Start at the first full minute
  after source entry, excluding the partially observed entry minute in both arms.
- Reuse only pure helpers from a frozen copy of `paper_monitor_v2.py`. No DB writers,
  order handlers or original monitor processes are invoked. Frozen fee/slippage,
  funding, BE and ATR-trailing inputs match the source monitor's configuration.
  Effective entry already includes entry slippage, which is not deducted again.
  Funding uses the source monitor's signed estimate/fallback convention exactly.
- The study is a common **minute execution model**, not an exact replay of the bot's
  15-second polling. Stop-first ordering is conservative; trailing uses the bar's
  favorable extreme before stop evaluation. Reached partial targets fill in order;
  BE/trailing newly activated by partials apply on the next minute. Gaps execute
  at the adverse opening price. Retain actual bot Net and the control-model
  discrepancy explicitly, including changes in main execution logic.
- Common finite 72-hour observation/administrative exit, not a new holding limit
  in the main bot. Record `HORIZON_72H` exits separately. Missing candle coverage
  never becomes an inferred fill or a zero result; keep the earliest missing cursor.
  Continue coverage beyond the original exit for the runner and for diagnostics up
  to 24h after actual closure, within the same 72h horizon.
- Diagnostic maximum price moves are not achievable profits. Summarize paired Net
  delta only after **both** arms close. Include costs, mean R, mean win/loss, holding
  time and chronological drawdown of closed paired observations. This drawdown is
  not portfolio mark-to-market drawdown. Same-coin overlap with a still-open runner
  is flagged; the fixed source stream does not simulate changed portfolio occupancy.
- Review after at least 14 elapsed days **and 50 completed pairs**, with model
  discrepancy, incomplete observations, administrative exits, directions/regimes,
  overlap and losses considered. No automatic promotion. The initial source limit
  is 200 admitted observations; reaching it is recorded without dropping rows.

## Operation, resources and rollback

One isolated serialized oneshot cycle every 300 seconds under `sqz-runner-comparison`.
Own code/data root `/root/bot_runner_comparison`; immutable protocol and code hashes
are stored before collecting. Source code/config drift pauses admissions. Existing
study positions retain their frozen inputs. No changes to other cohorts.

Limits: CPUQuota 5%, MemoryHigh 48MiB, MemoryMax 64MiB, MemorySwapMax 16MiB, Nice 19,
an execution budget of 45 seconds plus request timeout, eight requests per cycle,
500 bars per request, and a 256MiB own-database limit. A cheap resource condition
defers execution below 64MiB available RAM or 256MiB free swap. `flock` serializes
cycles and systemd permits writes only to this study's data directory. Open-source
processing lag is shown separately from the status file's freshness.

The dashboard reads the saved status via GET `/runner-comparison`, separately from
the six public-mainnet pilots and from main/shadow balances. It shows completed-pair
metrics, recent sources, control discrepancies, lag and the frozen protocol.

Rollback: stop/disable only this timer and stop its oneshot service; preserve its
database. Restore the saved dashboard/controller/read-only API files if needed and
restart only the dashboard controller. Old trading services need no restart.

Validation covers identical pre-runner paths and losses, long/short continuation,
gap fills, stop-first ambiguity, costs, future-only admission, source DB integrity,
coverage gaps, finite horizon and paired accounting. Historical observations are
diagnostic only; this initial implementation does not claim a measured advantage.
