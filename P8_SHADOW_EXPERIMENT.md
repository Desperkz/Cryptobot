# P8 shadow experiment — 2026-09-14

P8 runs only as virtual shadow positions. The deployed paper configuration retains measure mode, the existing measurement bucket, four concurrent positions and the previous SQZ exits: 25% at 1R, 35% at 1.6R, 40% at 2.2R. Structural danger flags now reject production SQZ entries independently of OF score.

## Controls and evidence

Existing conditional V1/V2 keep their cohort names, scoring formula and legacy liquidity annotation. Historical rows are not rewritten. The old annotator is intentionally retained for this control; the corrected liquidity geometry and removal of the proximity bonus are applied only to P8 candidates.

Two new cohorts, `2026-09-14-p8-v1:control` and `2026-09-14-p8-v1:observe`, evaluate the same pre-context SQZ or SQZ-DYNAMIC-UPD source candidates. Rejection decisions are recorded as `P8_SHADOW_EVALUATED`. Admitted variants go through the existing virtual shadow writer, with the source exit profile and a 0.20% maximum risk per virtual trade. Neither variant has production admission authority.

The control arm uses the original OF annotation and strict entry rules with strengthened structural checks. For UPD, its existing strict shadow checks also apply. The observe arm uses corrected OF annotation, neutral directional OF weights, the new context gate and the proposed strong-release rule. Score versions are `p8_control_score_v1` and `p8_neutral_of_v1`. The existing V2 is a separate historical/formula control, not an identically filtered P8 admission arm.

Both arms record all admitted score buckets. Compare identical source IDs and admission/rejection counts, not the sum of all virtual copies. `/conditional-edge` lists the cohorts separately. As with existing measurement shadow cohorts, portfolio slot competition and all downstream paper filters are not fully simulated; virtual PnL alone cannot authorize paper promotion.

## Guardrails and verification

- Structural liquidation, adverse-liquidity, structure-break and absorption flags reject SQZ entries at every score.
- Without finite positive 4h compression, only TREND_UP, TREND_DOWN or MOMENTUM satisfies the context gate.
- Global observe mode cannot be enabled for executing strategies; existing V1/V2 neutralization and P8 paper/live configuration are rejected.
- P8 signals explicitly reject production entry routing.
- V1 directional neutralization, where evaluated experimentally, retains structural penalties and uses a distinct version.
- `python scripts/check_p8_release.py` checks configuration and synthetic routing without network requests, orders or database writes.

Deployment requires a SQLite online backup and copies of replaced files. Code rollback restores those files and restarts the affected services. Never automatically restore the database during code rollback: doing so would erase trades recorded since the backup.
