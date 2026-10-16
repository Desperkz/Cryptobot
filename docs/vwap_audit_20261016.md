# VWAP diagnostic workflow — 16 October 2026

Use `scripts/vwap_trade_audit.py --data-dir EVIDENCE --output audit.json` with a
read-only export (`evidence.json.gz`) containing diversified shadow positions,
their original compressed/decoded snapshots and observations, plus supplemental
`SYMBOL_1m.json.gz` data. Source cohorts, executable files and settings stay frozen.

The audit replays all recorded closed exits and refuses mismatches. It separates
price movement from both slippages, fees and adverse funding. The original
`gross_before_exit_slippage` includes entry slippage: add entry slippage back
before subtracting all expenses, otherwise it would be charged twice.

MFE and MAE are bounds from minute OHLC. Entry's partial minute is excluded like
the original lab. Last-bar extrema can occur after an exit; do not infer exact
intrabar ordering. Both the MFE before the exit minute and its upper bound are
provided. Open marks are never included in realized performance.

`strategy_engine/vwap_v3.py` is a **rejected-for-deployment diagnostic candidate**,
not an enabled strategy. Its single proposed gate rejects when a closed 1h
EMA20's three-hour slope and price location both oppose a V2 entry. It has no
router registration, strategy mode, worker, timer or production deployment.
Entry, stop, target and quantity remain the source V2 values for this diagnostic.

No threshold sweep was used. The fixed gate reduces activity strongly and did
not produce positive net results in either explored window. Lower total losses
alone do not establish a better strategy when most entries disappear. Other
simple hypotheses (short efficiency window, two directional closes) also failed
the observed diagnostic. These are exploratory, selected-after-observation
checks and must never be described as held-out validation.

The report is evidence for redesigning the entry mechanism, not authorization
to promote a filtered subset. Preserve original V2 histories. A future new
mechanism must receive a separate cohort and prospective assessment, including
activity, costs and common-source comparisons. Do not block diagnosis on a
promotion sample-size threshold.
