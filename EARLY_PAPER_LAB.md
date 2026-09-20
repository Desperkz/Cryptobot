# Early SQZ paper experiment — v1, 2026-09-20

Hypothesis: an early build-stage breakout needs its own reachable admission
rule. Its source generator cannot yet observe a post-release retest. This is a
prospective experiment, not a claim that four profitable historical early
episodes establish an edge. Those observations informed the hypothesis and
must not be counted as its validation.

## Frozen rule and comparison

Only unchanged SQZ sources tagged `build / early_breakout` are considered.
The same 32-symbol universe, liquidity floors and source thresholds are used.
There is no new symbol selection or threshold optimization using historical PnL.

Three virtual arms receive the same observed source:

- `EARLY_BASELINE_2R`: valid early source, no additional admission filters.
- `EARLY_STRICT_2R`: original strict admission on corrected order-flow inputs.
- `EARLY_RULE_2R`: the same strict gate vector, except the requirement for a
  post-release retest or strong release is removed for early sources only.

All three recheck existing early source quality: compression duration (6 bars
in non-trend regimes, 10 in TREND_UP/DOWN with the frozen configuration),
breakout distance 0.10–2.40 ATR and confirmation volume at least the greater
of configured general and early volume floors (1.50 in this configuration).
Inputs must be finite and corrected flow present. Treatment retains adverse
flow/score checks, structural risk vetoes, relative strength aligned with the
trade, and 15m structure-break confirmation. Context follows the frozen source
configuration; there is no additional P8 4h context rule in these strict arms.
The baseline is purely virtual and may enter when admission safeguards reject
the other two arms. No synthetic retest flag is written.

The strict arm is expected to reject build-stage entries; it checks the
logical change. Baseline versus EARLY_RULE measures selection. This does not
separately test every possible filter or claim an exact production portfolio.

## Source identity and execution

Identity is symbol + direction + actual closed 1h candle timestamp. Only the
first observation of that source is eligible. Later 15m observations do not
add samples or retry a prior rejection. No other source of the same symbol can
enter until all active arms of its previous source close; this gives the arms
a common opportunity schedule. Occupancy skips are recorded, not retried.

When admitted, baseline and treatment share the next minute's open, original
stop, single 2R target and 24-hour timeout. Risk is 2 virtual USDT before costs;
fees are 4bps each side and slippage 5bps each side. Funding remains an estimate
from the entry rate, not historical settlements. The existing conservative
minute-OHLC executor is reused. No position is sent to an exchange. Structural
guards, duplicate-hour behavior, equal entries, shared occupancy, restart
recovery and both LONG/SHORT gate reachability have regression tests.

## Isolation and registration

Settings: `early_paper_lab_settings.json`.
Entry point: `python -m trading_bot.research.early_paper --config config.yaml
--settings early_paper_lab_settings.json --data-dir /absolute/new/directory`.
The settings must explicitly name `early-sqz-v1`; the original entry point
rejects early settings and vice versa. Each cohort freezes code/config/settings
hashes and this protocol's rule metadata. `--once` is for a separate probe only.

Deployment uses `/root/bot_mainnet_early`, service `mainnet-early-paper.service`,
and a separate SQLite database. Existing mainnet v1/demo deployments keep their
own unchanged code and data. The current source tree contains shared research
hooks; do not copy it onto the running frozen v1 deployment. Changing code or
settings in either frozen cohort requires a new cohort/directory.

Service limits: 15% CPU, 180MiB memory ceiling, 64MiB swap ceiling. Acquisition
every 300s, monitoring every 30s; public unsigned GET allowlist only. No keys,
private account endpoints, orders, Telegram posts or new dashboard ports.
The 1GiB database/512MiB free-disk guard is retained. To roll back, stop/disable
only `mainnet-early-paper`; preserve its data for inspection.

## Evaluation fixed before deployment

Use new observations and executable entries after actual cohort start. Keep
all early candidates, refusals, missing data and occupancy skips. Report hourly
sources, matched baseline/treatment entries, realized cost-adjusted R, losses,
drawdown, symbol/direction concentration and overlapping market episodes.
Copies across arms are correlated; never add their PnL or sample counts.

First 20 different hourly sources are an operational review, not a profitability
or live-trading gate. If too few treatment entries occur, report the remaining
blocking conditions instead of altering thresholds inside this cohort. Retain
the original experiment as a separate reference and make no live promotion
decision from the four historical early wins or the first small new sample.
