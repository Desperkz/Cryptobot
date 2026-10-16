# VWAP pullback V1 — 90-day result and no-deployment decision

Rule and replay implementation: `56b5095`. Fixed universe: BTC, BNB, XRP, SOL,
DOGE, LTC USDT perpetuals. Period: 18 July 2026 10:00 UTC through 16 October
2026 10:05:59 UTC. 777,636 contiguous one-minute bars, with closed 15m/1h/4h
warmup windows. Older saved overlap matches OHLCV exactly for all six symbols,
including 71,460 previously stored minute bars.

The source rule was declared before the first 7–15 October replay; its trading
parameters were not retuned after the three-trade negative initial result.
The larger dataset was then acquired, keeping identical rules and a new fixed
90-day cutoff. Added candle validation and grouped simultaneous-exit drawdown
are implementation checks, not changes to the entry hypothesis.

| Rule / adverse slippage per side | Closed | Open | Net USDT | Avg R | PF | Closed drawdown USDT |
|---|---:|---:|---:|---:|---:|---:|
| VWAP reversion V2 / 5bps | 1,214 | 4 | -728.05 | -0.300 | 0.537 | -728.05 |
| New trend pullback V1 / 5bps | 48 | 0 | -52.87 | -0.551 | 0.260 | -56.00 |
| VWAP reversion V2 / 10bps | 938 | 3 | -688.28 | -0.367 | 0.442 | -688.28 |
| New trend pullback V1 / 10bps | 36 | 0 | -42.15 | -0.585 | 0.221 | -42.15 |

All use a fixed 2 virtual USDT budget with modeled expenses. Stress recomputes
fills, sizing and admission; it is not a flat cost deduction. Lower total loss
on fewer trades is not evidence of improvement: the new rule is worse per trade.
Its 48 ordinary-cost closed entries include 35 stops, 7 targets and 6 timeouts.
Price-only PnL at original quantities is -30.18 USDT and expenses 22.70 USDT.
Every fixed symbol has negative net results. It is not solely a fee problem.

**Do not deploy this candidate.** The predeclared shadow-launch check fails net
profit, stress robustness and improvement in mean R. Main bot, V2 shadow state,
existing pilots, risk and their frozen code/configuration remain unchanged.
This is a retained research implementation and reproducible negative result,
not a promoted strategy or a claimed trading improvement.

These are exploratory historical results, not real bid/ask fills or registered
prospective validation. Reported drawdown includes only closed exit-minute events;
open exposure and intratrade drawdown are not portfolio equity. Do not add the
correlated strategies' PnL. No grid search, symbol cherry-picking or automatic
promotion was performed.
