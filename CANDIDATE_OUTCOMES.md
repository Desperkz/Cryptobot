# Candidate outcome labels v1

Isolated analysis of every first hourly source in the existing candidate observer.
No source/observer writes, no trading configuration changes and no new orders or
paper portfolio. All overlapping labels are hypothetical, not deployable returns.

## Frozen protocol

- First recorded observation per source; never replace refusals with later passes.
  Verify compressed payload checksum and identity before import. Persist the first
  liquidity/gate decisions, missing data and book measurements with its checksum.
- Entry: first minute open AFTER `finished_ms`, plus 5 bps adverse slippage.
  `observed_ms` precedes metrics/depth acquisition and must not set an earlier
  entry. This can differ from entries in existing paper cohorts.
- Original stop, single 2R exit, risk 2 hypothetical USDT before costs, 24h timeout.
  Reuse the existing conservative OHLC executor: stop first when ambiguous;
  4 bps fees and 5 bps exit slippage; funding uses the first observed rate as an
  estimate, or the executor's adverse buffer if unavailable. No tuning to PnL.
- Entry can be INVALID_ENTRY if the next-minute price crosses the original stop.
  Missing/invalid/revised minute data causes a visible retryable error, never an
  invented candle, shifted entry or a silently realized partial result.
- Earlier observations are labelled BACKFILL relative to this evaluator's first
  start; subsequent ones are PROSPECTIVE. Never describe backfill as unseen proof.
- All source labels and first labels in fixed 24h windows per symbol/direction
  are reported separately. Windows are anchored at the first entry, independent
  of outcome. This reduces repetitions but does not establish independence across
  correlated symbols/directions or implement portfolio exposure constraints.
- Report OPEN marks separately from CLOSED results. Filter refusals overlap;
  blocked wins/losses and non-blocked outcomes are descriptive, not causal filter
  effects or a recommendation to remove a filter. Early-only checks on release
  sources are not applicable; missing checks remain unknown.
  Overall liquidity, CURRENT_GATE, P8_OBSERVE and EARLY_RULE decisions are also
  compared using the SAME uniform 2R label, not their original exit profiles.

The book snapshot is retained as evidence, not substituted into a later-minute
fill. Fixed-slippage labels do not certify real execution, lot/minimum-order
compliance or capacity. A deep snapshot covering signal-size quantity does not
guarantee the quantity calculated from next-minute entry and original stop.

## Operation

`python -m trading_bot.research.candidate_outcomes --source /path/to/observer.sqlite3
--data-dir /absolute/separate/directory`

Each invocation runs one bounded cycle and exits. A separate systemd timer invokes
it 15 minutes after the previous run exits; overlapping writers are locked out.
Only public unsigned Binance GET requests via the existing restricted client.
No credentials, private APIs, external notifications or listening ports.

Deployment: `/root/bot_candidate_outcomes`; service/timer `sqz-candidate-outcomes`.
Source read-only: `/root/bot_candidate_observer/data/candidate_observer.sqlite3`.
Own SQLite, candle cache, `status.json`, and `report.json` under its `data/`.
Code/protocol and observer manifest are frozen. Changes require a new directory.
128 sources per cycle; up to 1440 validated cached minutes per source; 512 MiB
database guard and 512 MiB minimum free disk. Old candle data is retained for audit.

Service controls: CPUQuota 5%, Nice 19, MemoryHigh 80 MiB, MemoryMax 128 MiB,
MemorySwapMax 32 MiB, runtime limit 10 minutes; only its own data directory writable.
Probe uses an independent snapshot and data directory before production activation.
Rollback: stop/disable its timer and stop its service; keep both databases.
Existing bot and observer services need no restart.
