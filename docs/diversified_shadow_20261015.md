# Diversified shadow research: 2026-10-15

The main paper bot and its paired exit study remain unchanged. A separate
credential-free process compares five native signal generators with six new
rule versions on the same public-mainnet observations. There is no source DB
access, exchange execution, automatic promotion or aggregation into a balance.

## Findings from the currently deployed rules

| Approach | Why activity is low | New hypothesis |
|---|---|---|
| Mean reversion | Trend/momentum veto plus deviation from the 4h EMA200 and 1h RSI extreme. Since the previous deployment, observed MR evaluations were dominated by 5366 regime vetoes and 4320 missing extremes; evaluations repeat across scans. Nine reached the entry-candle rejection, 79 the volume rejection, two lacked edge/reversal confirmation. | MEAN_REVERSION_V2 uses the current 15m EMA20 deviation, a 1h efficiency/slope guard and an observed turn toward the mean. It does not blindly fade persistent trends. |
| VWAP | Safe mode requires 4.5–6 ATR deviation, 1.35 volume ratio, a reversal candle and 0.8 ATR progress. Throttled diagnostics since deployment: 764 insufficient deviation/RSI, 14 weak volume, seven missing reversal. The watch arm additionally requires 1.1 ATR progress. These are diagnostic rows, not unique opportunities. | VWAP_REVERSION_V2 requires a prior stretch, a subsequent turn, a trend guard and sufficient net target space. Volume compares against prior bars, not an average containing the candidate bar. |
| Range rebound | RANGE_GRID is explicitly diagnostic-only and is absent from shadow_revalidation_strategies. It could not open an original shadow position even if its generator signalled. Its generator also requires 4h RANGE, narrow width, an outer 10% entry zone, RSI extreme and flow checks. | RANGE_REBOUND_V2 uses prior boundaries excluding the two setup bars, separate non-adjacent level tests, a rebound and a target at the range midpoint. |
| False breakout | Same-bar sweep, reclaim >=0.9 ATR, follow-through body, volume >=1.25 and strict flow are required together. Mandatory sweep/absorption/aggressive-flow reasons sum to 0.62, below the minimum score of 0.65: an additional feature is implicitly necessary. | SWEEP_RECLAIM_V2 separates the sweep and confirmation across two closed bars. Price structure and costs remain required; same-bar microstructure coincidences are annotations of the baseline, not mandatory new-rule gates. |
| Momentum continuation | 4h regime/EMA stack, a 20-bar breakout, volume >=1.9, body <=1.4 ATR, extension <=0.8 ATR, strict directional flow and edge >=0.70 are stacked. Structure+flow+nearby-zone scores only 0.56, so further edge features are necessary. | MOMENTUM_CONTINUATION_V2 uses a first confirmed channel break in the 4h EMA50/200 direction, volume, bounded extension/body and explicit net-cost admission. |

There is insufficient recorded per-gate live telemetry to assign precise
historical failure percentages to Range/Sweep/Momentum. A separate retrospective
first-failure price funnel was run on six symbols; historical flow was marked
unavailable rather than reconstructed from today's snapshot. Native controls in
the new lab cover signal generation under common lab execution, NOT the main
bot's full policy/partial-exit pipeline.

## New session rule

SESSION_RANGE_BREAKOUT_V1 fixes the UTC 00:00–06:00 range before entries, evaluates
closed 15m breakouts during UTC 06:00–12:00, and requires volume, bounded extension
and cost-aware sizing. At most one entry per direction/symbol/UTC day. Default
stop 1.5 ATR, target 2.5R and 12h timeout. This rule is a research hypothesis.

## Retrospective evidence: no proven profitable candidate

Exploratory recent-period replay used six liquid symbols, 15m signals, the next
15m opening price, then 71,460 contiguous public minute bars for conservative
stop-first exits. Costs were 4bps each side, 5bps adverse entry and exit slippage,
and an adverse 1bp/8h funding buffer. Risk was fixed at 2 USDT including reserved
costs. No parameter sweep was used. This is not independently held-out evidence
or a replay of real executable bid/ask history; minute data were acquired later
than the signal snapshot and their acquisition times were retained.

| New rule | Closed simulations | Mean net R | Profit factor |
|---|---:|---:|---:|
| Mean reversion V2 | 30 | -0.579 | 0.24 |
| VWAP reversion V2 | 115 | -0.443 | 0.36 |
| Range rebound V2 | 6 | -0.580 | 0.29 |
| Sweep reclaim V2 | 132 | -0.373 | 0.41 |
| Momentum continuation V2 | 23 | -0.138 | 0.78 |
| Session range breakout V1 | 9 | -0.711 | 0.18 |

These results do not justify promotion to the main paper bot. More signals alone
did not create an edge. The separate future-only lab records high-resolution
execution evidence and full rejection funnels; a new version/cohort is required
for any tuning. Do not combine correlated rules, repeated symbols or overlapping
trades into a fictional portfolio.

## Prospective protocol and operations

- Fixed universe: BTC, BNB, XRP, SOL, DOGE, LTC perpetual USDT contracts.
- Native controls: MR, VWAP, Range, Sweep, Momentum. Six new versions above.
- Dedicated `/root/bot_diversified_shadow/code` and `data/shadow.sqlite3`.
- Fresh observed midpoint, 0.35R drift guard, cost-aware 2-USDT maximum budget;
  gaps can exceed that budget. At most one open position per rule/symbol,
  24 observations in total, and 45 minutes before re-entry.
- No retroactive admission: the signal bar must close after registration. Only
  bars already closed BEFORE their request are usable, even across network delay.
- Exits use contiguous closed 1m candles from the first FULL minute after entry.
  The partially observed entry minute is excluded. Stop precedes target if both
  occur; gaps use the adverse opening price. Open trades never count as realized
  profit. Missing minute data pause new admissions; no fabricated interpolation.
- Save raw snapshots and rejection reasons once per symbol/rule/closed bar.
  Freeze all Python source files, baseline config and settings by SHA-256.
- At 512MiB storage, stop admissions while continuing exits. Retain evidence.
- One-shot every >=300s, retry resource guards every 47s to avoid phase-lock with
  the existing 60s runner timer. CPU 10%, memory high
  64MiB/max 80MiB, swap 32MiB, timeout 150s. Defer during the promotion/runner worker
  or low memory/swap. A guard deferral is not a successful market scan.
- Dashboard Research tab reads saved JSON only. Missing/stale/degraded states
  stay explicit; no order or strategy promotion controls are added.

Rollback: disable only diversified-shadow.timer/service, retain this cohort's
code/data, restore the three presentation files from the deployment backup and
restart only bot-control-v2-1. Main strategies/config, the main monitor and all
existing frozen experiments must remain byte-identical.
