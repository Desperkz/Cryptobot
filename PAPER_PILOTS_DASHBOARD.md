# Isolated mainnet paper pilots in the dashboard

The **Paper-пилоты** tab reads six isolated experiments through `GET /paper-pilots`.
The main paper account, its shadow research, and these pilots retain separate statistics.
There is no combined pilot PnL: policies and arms can reuse the same source observation.
Each result belongs to one pilot, policy and arm. Empty arms remain visible without a
profitability claim; profit factor is undefined when there are no losses.

The fixed database allowlist is `/root/<directory>/data/mainnet_paper_lab.sqlite3`:

| Pilot | Directory |
| --- | --- |
| Mainnet baseline | `bot_mainnet_paper` |
| Early entry | `bot_mainnet_early` |
| BTC / relative strength | `bot_btc_rs_paper` |
| SQZ direction | `bot_sqz_direction_paper` |
| SQZ confirmation | `bot_sqz_confirmation_paper` |
| Candidate promotion | `bot_sqz_promotion_paper` |

`paper_pilots_api.py` requires a saved `LOCAL_PAPER_ONLY` manifest and the public
mainnet futures data venue. SQLite connections use `mode=ro` and `query_only`,
with a read transaction and explicit close. Active WAL entries remain visible.
The reader never loads candles, invokes order functions, fetches market data,
sets up a schema, writes to the databases or changes frozen cohort settings.

Closed statistics use all saved position results, including saved costs, without
deducting them a second time. Invalid results make that group's metrics unavailable.
The recent list is bounded to 50 records; the open/pending list is independently
bounded to 128 records and labels any truncation. Open PnL is the saved minute-bar
valuation, whose timestamp appears in the expanded details. A promotion record can
show four paired cost scenarios; they are explicitly described as one observation.

Data freshness comes from the saved scan and monitor timestamps, not process-state
assumptions. Missing, stale and damaged sources have distinct warnings. One failed
pilot does not hide the other five. Reads have a 0.6-second SQLite progress budget
per database and a 0.25-second lock timeout. A serialized 15-second response cache
coalesces browser refreshes. Only the selected dashboard tab requests pilot data.

Deploy `paper_pilots_api.py`, `bot_control_v2.py` and `dashboard_v2.html` together.
Back up the existing controller and page before replacing them. Restart only
`bot-control-v2-1` to register the GET route. Trading, position monitoring and
research services require no restart or configuration changes. For rollback,
restore the saved controller and page, remove the new reader and restart only
the controller. Verify the served page hash, GET response, service PIDs, frozen
manifests and protected code before considering the deployment complete.

Validation includes read-only integrity, active WAL visibility, separate correlated
arms/policies, missing/corrupt/nonfinite inputs, caching, and the absence of pilot
mutation routes. Browser checks cover all six tabs, filters, cost details, freshness
warnings, and unchanged main-account/shadow behavior at five viewport sizes.
