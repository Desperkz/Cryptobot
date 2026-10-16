# Prospective SQZ v2 HIGH paper check — 16 October 2026

The user approved adding v2 HIGH to an isolated paper check. Existing v1 HIGH,
v1 MID and v2 MID transfer experiments continue unchanged. This is research
admission, not promotion to the main paper portfolio or real trading.

## Protocol

- New cohort `sqz-c2-high-forward-v1-20261016`, separate directory
  `/root/bot_sqz_c2_high_paper`, registration timestamp and observation watermark.
  Never import pre-registration sources, even when their outcomes are positive.
- Read the existing frozen `sqz-promotion-transfer-v1-20261005` feed through
  `mode=ro` / `query_only`. Pin its manifest SHA-256. No network client, orders,
  credentials, new market scanner, or writes to the source experiment.
- Use only the first recorded observation of a newly observed hourly source.
  Preserve its original validated v2 bucket. Never reconsider a later score.
  Keep original source quality and shared symbol occupancy refusals. A corrupt
  first record stops admission progression and is reported as degraded.
- `C2_HIGH_SOURCE_2R` follows all admitted source-control entries;
  `C2_HIGH_2R` is their v2 HIGH subset. These are correlated paired observations,
  not two additive portfolios. This frozen transfer feed is narrower than all
  embedded main-bot shadow signals and can produce different sample sizes.
- Commit selection before reading execution evidence. Use the source's original
  next-minute entry timestamp, original stop, single 2R target and 24h limit.
  The follower can publish later; this is a prospectively registered simulation
  from timestamped source decisions, not a claim of real executable fills.
- Primary sizing: 2 virtual USDT stop-plus-expenses budget. Entry slippage is
  included once in stop distance; reserve exit slippage, both 4bps fees, and
  adverse funding. Gaps and actual execution can exceed this estimate.
- Replay stored contiguous, closed one-minute bars using unchanged `compare_sizing`.
  Report stop-only and stop-plus-expenses sizing at 5/10bps adverse slippage.
  Continue stress monitoring after the primary scenario closes. Missing minutes
  are errors/delays, never inferred candles or realized zeroes.
- At least 14 elapsed days AND 50 closed control entries AND 50 closed HIGH
  entries before formal review; include drawdown and direction/time/regime
  concentration in that review. No automatic paper or live promotion. Statistical
  independence is not implied by multiple coins or overlapping arms.

## Operation

`sqz-c2-high-paper.timer` polls every ~61 seconds, after the original service
when both are scheduled. The worker has CPU quota 5%, memory high/max 32/48 MiB,
swap max 16 MiB, 60-second timeout, a local lock and no permanent process. It
does not acquire new exchange data. Source scan and monitor timestamps are
preserved in the dashboard, so polling cannot make stale evidence look fresh.
Admission pauses at 128 MiB or under 512 MiB free disk; existing exits continue.

Dashboard: separate `SQZ · v2 HIGH · paper-проверка` pilot with control and HIGH
groups. Existing histories and main bot settings stay unchanged.

Rollback: disable only `sqz-c2-high-paper.timer` and stop its service; retain
the new data directory. Restore the backed-up `paper_pilots_api.py` and
`dashboard_v2.html`, restart only the controller. Never reset source/pilot DBs.
