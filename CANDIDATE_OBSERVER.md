# SQZ candidate observer v1 — 2026-09-22

Purpose: measure which unchanged SQZ sources are hidden by preliminary liquidity
filters. Observation only: no virtual positions, orders, account access or PnL
claims. Existing paper cohorts retain their code, settings and databases.

## Prospective protocol

- Fixed 62 symbols: original 32 plus the 30 highest-volume external instruments
  from the 2026-09-22 05:03 UTC research snapshot. The list was selected by volume,
  not profits. This is a frozen prospective coverage sample, not a historically
  unbiased universe. New listings will require a separately versioned study.
- Each 900-second scan records the registered list and currently tradable USDT
  perpetual membership, including exclusions. No volume/spread/depth filter runs
  before the unchanged OHLCV SQZ source generator.
- One cutoff at scan start across symbols and BTC reference; 499 closed candles
  per 15m/1h/4h frame. Reject missing, stale, misaligned, gapped or invalid candles.
  The 15-minute schedule can miss shorter-lived candidates between scans and is
  not identical to the existing five-minute experiments.
- Preserve every generated candidate, including liquidity rejects and failures
  of subsequent metrics/book requests. Missing data stays unknown. NO_SIGNAL
  means the unchanged generator returned no candidate; it does not identify its
  internal reason. Coverage includes frame hashes and closed-through timestamps.
- Candidate evidence includes frames, BTC reference, request timestamps/raw
  responses, all three liquidity checks and existing original/P8 gate vectors.
  Early sources additionally get the already registered EARLY_RULE gate vector.
  Gate allowances here are diagnostic, never orders or admissions to paper arms.
- Hourly identity: symbol + direction + actual closed 1h candle. Repeated scans
  remain separate observations of the same source. Release and early observations
  in that hour share identity; adjacent hours can still be correlated episodes.

## Order book measurements

Fetch 100 levels per side for candidates. For theoretical 2 USDT risk before
costs, base quantity is risk / absolute(signal entry - original stop). Walk asks
for LONG and bids for SHORT. Save VWAP versus mid, best quote and signal price.
Insufficient visible depth yields no full-size VWAP. Signal stop direction,
nonfinite/invalid values, ordering and crossed books are checked.

Save each side's visible notional inside 5/10/20 bps of mid and whether the last
returned level reaches the boundary. An uncovered boundary means a lower bound,
not the entire range. Save top-5 as a comparison, without fitting a new threshold.
The new depth snapshot is later than the old top-5 metric, with separate request
timestamps; differences can include market movement.

No guarantee of future fills: no latency model, fees/funding, lot rounding or
exchange minimum-order constraints in these measurements. A fillable snapshot
does not prove a profitable or executable trading policy. Evaluate future
source clusters and failure coverage before designing a new admission cohort.

## Operation

`python -m trading_bot.research.candidate_observer --config config.yaml
--settings candidate_observer_settings.json --data-dir /absolute/new/directory`

`--once` uses a separate probe directory. Config, all package source files and
settings are hashed in the database manifest; changes require a new directory.
One writer lock, WAL/FULL SQLite, 1 GiB database limit and 512 MiB minimum free
disk. PublicDataClient rejects credentials, private paths, signing and non-GET.

Deployment: `/root/bot_candidate_observer`, `sqz-candidate-observer.service`.
CPU 10%, MemoryHigh 96 MiB, MemoryMax 160 MiB, MemorySwapMax 64 MiB, Nice 15.
Only its own data directory is writable. `status.json` reports last completed
scan, sources and observations. No inbound ports or notifications are added.
Rollback: stop/disable only this service; retain the database for review.

Report read-only with `python scripts/candidate_observer_report.py --database
/root/bot_candidate_observer/data/candidate_observer.sqlite3`.
