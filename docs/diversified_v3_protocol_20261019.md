# Six revised shadow strategies and a new statistics epoch

User request: investigate losses, implement corrections, then restart statistics.
The scope is the six diversified-shadow rules; main paper and SQZ studies retain
their histories and settings. This is a research launch, not a claim of profit.

## Evidence behind the changes

The 19 October 22:48 UTC+5 read-only audit reproduced 181 saved exits, including
one native control. All 183 V2-rule entries/fills, including three still open,
matched saved snapshots within floating-point tolerance. Six new rules had 180
closed trades. Five were negative even before modeled trading expenses.

| V2 rule | Closed | Price-only USDT | Modeled expenses | Net USDT |
| --- | ---: | ---: | ---: | ---: |
| Mean reversion | 31 | -25.5954 | 17.3359 | -42.9313 |
| VWAP reversion | 64 | -8.2284 | 33.9222 | -42.1506 |
| Range rebound | 8 | -4.8247 | 5.1335 | -9.9582 |
| Sweep reclaim | 47 | 16.5713 | 23.4442 | -6.8729 |
| Momentum continuation | 23 | -12.8232 | 12.5649 | -25.3881 |
| Session breakout | 7 | -9.3477 | 4.3291 | -13.6768 |

Mean reversion had 26 stops, 19 without even a 0.3R favorable upper candle bound;
VWAP had 42 stops, 29 without 0.3R. VWAP shorts contributed -41.9534 of the loss.
This short, correlated sample does not justify a permanent direction/symbol ban.
Momentum had 19 stops/23 closes, all shorts; session had seven stops/seven closes.
Doubling timeouts did not make any of the six positive. Sweep price-only gains
were concentrated in entries from 19 October; earlier entry-days totaled -26.63
net. The hypothesis needs more than one favorable market episode.

Saved entry depth covered all 47 closed sweep quantities and implied ~0.47bps
median instantaneous entry impact including half-spread, versus the model's
5bps. This is not an actual fill and says nothing about stop-exit liquidity or
latency. Fees/slippage are therefore NOT lowered to manufacture improvement.

## Fixed revised rules

Rules live in `strategy_engine/diversified_v3.py`. They are independent named
hypotheses; old V2 code on the server is not changed.

- MR V3 and VWAP V3 RECLAIM require a close through the stretch-setup extreme by
  0.05ATR, directional body >=0.15ATR, and close in the outer 35% of its candle.
  They reject a persistent opposing local hourly move (6h efficiency >0.60 and
  opposing EMA20/50 trend). Original RSI/deviation/24h-efficiency gates remain.
  Targets remain EMA20 / rolling 24h HLC3-volume VWAP; no rejected VWAP pullback
  candidate is silently reused.
- Range V3 trims the two most extreme highs/lows in its prior 48-bar range,
  requires separated boundary tests, rejects consecutive recent closes outside
  the boundaries, and confirms a bounce through the setup extreme. Target is
  still the midpoint. There were only eight V2 observations: no family-wide
  profitability conclusion is implied.
- Sweep V3 requires the sweep candle itself to close back inside the previous
  range, directional next-bar confirmation, and no late entry beyond 1.2ATR
  from the reclaimed boundary. Stop is structural with >=1.2ATR distance; target
  remains 2R, horizon remains six hours.
- Momentum V3 retains the original 4h-direction and breakout signal. It requires
  the breakout candle to close in its directional outer 35%, with directional
  body >=0.2ATR, and changes the target from 2.5R to 2R. The original 1.5ATR stop
  and twelve-hour horizon remain.
- Session V2 retains the original 00:00–06:00 UTC range / 06:00–12:00 entry window,
  adds the same strong-close check and changes 2.5R to 2R. The original 1.5ATR stop,
  twelve-hour horizon and one entry per symbol/direction/UTC day remain.

The first candidate used an hourly-direction/retest entry for momentum and a
retest entry for session. The fixed 90d replay made their average 5bps results
worse (-0.275R and -0.687R versus V2 -0.140R/-0.249R). Those candidates were rejected
before deployment. Their code/results are retained in the research artifacts.
The two final replacements were declared before a second replay; this is an
explicit second exploratory hypothesis, not an untouched out-of-sample test.

The second session candidate also failed: 94 closes, -0.385R average versus
V1 -0.249R; 10bps stress -0.413R. It is retained as a named **paused** row, with
no new admissions. Other five hypotheses start prospectively. Pausing a failed
candidate is not represented as positive performance or a profitable strategy.

The four reversal/range rules use structural stops with 0.15ATR buffer, minimum
1.2ATR and maximum 3ATR. MR/VWAP/range horizons stay eight hours, momentum/session twelve. Revisions
retain full validation of closed, finite, aligned, contiguous inputs. Historical
replay validates full series once; time/length alignment is still checked for
each window and the default production validation is unchanged.

Execution retains the original stop-plus-expenses 2 USDT budget, 4bps each-side
fees, adverse 5bps each-side slippage, adverse 1bp/8h funding reserve, 0.35R drift
limit and 0.8 minimum net reward/loss ratio. A new admission rejects full-horizon
expenses above 35% of the stop budget. It reduces expensive observations; it
does not rescale risk or turn missing trades into profitable ones. Replay also
recalculates the original 10bps stress scenario, including sizing/admissions.

## Prospective epoch and source isolation

New root `/root/bot_diversified_v3`, own code, `data/shadow.sqlite3`, status and
timer. `diversified_v3_shadow` reads only the frozen V2 **snapshots** and manifest
through query-only SQLite. It never reads source trade outcomes. Snapshot IDs
are watermarked at registration; signal bar and observation must be after the
new registration, with publication delay <=120s. Expired snapshots are skipped,
not backfilled. Entry decisions are committed before requesting outcome minutes.
These are timestamped virtual feed entries, not real exchange fills.

The unchanged V2 timer remains a control and public-market feed, avoiding a
second copy of expensive candle/depth scans. V3 requests only exit minutes.
Missing-minute backlog, stale/unhealthy feed, memory/storage limits or manifest
changes stop new admissions. Monitoring can continue while admissions are paused.

New unit: Nice19, CPU5%, memory64/80MiB, swap32MiB, timeout100s. Guard requires
80MiB available RAM, 256MiB free swap, original source/runner/promotion inactive.
The 61-second timer avoids phase locking and does not wait for a two-minute
upstream systemd job via an `After=` dependency.

## Reset, archive and rollback

The reset creates a new database; it never truncates/deletes old trades or marks
existing positions closed at a fabricated price. Before switching the dashboard,
SQLite backup saves a consistent V2 database, old code/status, SHA256, counts and
protected-service inventory in `/root/diversified_v3_deploy_20261019/backup_*`.
Zero initial entries/closes/open positions are asserted. Only V3 is shown by
`/diversified-shadow`; the UI displays its new start date and names.

Only `paper_pilots_api.py` and `dashboard_v2.html` change under the main root.
Only the controller restarts. All protected code/config hashes, frozen manifests,
main bot/monitor and other permanent pilot PIDs must match before/after.
Rollback stops/disables only V3, restores those two UI files, restarts only the
controller and retains the failed V3 directory. The V2 control keeps its history.

No real orders, automatic promotion, global paper reset or SQZ reset are enabled.
Use the exact registration timestamp for forward results; historical and old
cohort statistics must never be mixed into the new zero-start series.

## Historical comparison of final candidates (18 July–16 October)

Identical six-symbol data, next-15m-open model entries and contiguous 1m exits.
Five-bps primary; four-bps fees per side and adverse funding reserve unchanged.

| Rule | V2 closes | New closes | V2 avg R | New avg R | New net USDT | New 10bps avg R |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Mean reversion | 293 | 13 | -0.252 | +0.058 | +1.52 | +0.415 (6 closes) |
| VWAP | 1214 | 268 | -0.300 | -0.215 | -115.37 | -0.302 |
| Range | 143 | 152 | -0.422 | -0.254 | -77.09 | -0.358 |
| Sweep | 1348 | 633 | -0.244 | -0.234 | -296.39 | -0.270 |
| Momentum | 398 | 237 | -0.140 | -0.074 | -35.28 | -0.051 |
| Session (paused) | 182 | 94 | -0.249 | -0.385 | -72.33 | -0.413 |

Most candidates remain negative. Activity fell substantially for several rules,
especially mean reversion: 13 trades cannot substantiate profitability. Sweep's
per-trade improvement is small; its lower aggregate loss mostly reflects fewer
entries. The launch is five revised shadow hypotheses and a visibly paused
session row, not a profitable-system release. No improvement is claimed from
pausing the losing session strategy. Primary and stress trade sets can differ.
