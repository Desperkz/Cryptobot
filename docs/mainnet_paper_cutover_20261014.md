# Public mainnet paper cutover, 2026-10-14

The submitted patch moves the main bot's public market data and paper exit
monitor from demo-fapi to fapi. PAPER_TRADING remains local simulation, risk
stays at 0.5%, and mainnet live remains disabled. Non-live clients receive no
exchange credentials. Entry prices are rebased to a fresh book midpoint with
a 0.35R drift guard before sizing; costs still apply once during simulation.

The main SQZ admission profile is edge_v1. It requires 4h compression or a
directional regime, preserves structural danger vetoes, and blocks adverse or
unknown relative strength for altcoins. Directional order-flow remains recorded.
Legacy admission functions remain available to the isolated frozen experiments.
TREND_BREAKOUT_4H starts in shadow, with a 25% exit at 2R, a trailing remainder,
and an 8R cap. Historical demo results do not establish a mainnet edge.

Integration safeguards beyond the supplied patch:

- Signal metadata records market_data_base_url and market_data_epoch.
- New embedded research cohort identifiers and analytics.stats_epoch separate
  future mainnet observations from the unchanged historical demo rows.
- Deployment must assert zero open main and embedded shadow positions while
  both entry and exit services are stopped; otherwise abort and restore service.
- Preserve server-specific database URL, log path, web port, secrets and state.
- Back up changed files and closed trade rows; retain a rollback file list.
- Leave eight isolated experiment code roots, manifests and running processes
  unchanged. Do not auto-promote strategies or enable exchange orders.
- Archive the empty demo runner comparison directory and settings/monitor
  snapshot. Register a new comparison after installing main code/config, with
  the current source ID watermark, public mainnet venue and matching hashes.
  Restart the 14-day/50-pair review clock; preserve the old outage archive.

Rollback must check for positions opened since cutover before returning to demo
prices. With such positions, stop admissions and retain the mainnet exit monitor
until they close; never reprice mainnet positions on demo or rewrite results.
Changing the YAML profile to legacy alone restores admission semantics, not
the old market venue or frozen study hashes.
