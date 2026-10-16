# VWAP trend pullback V1 — fixed exploratory protocol

Declared 16 October 2026 before the first replay. Separate from VWAP reversion V2
and the rejected V3 local-trend admission filter. No parameter grid or symbol
selection from PnL. Fixed universe BTC, BNB, XRP, SOL, DOGE, LTC USDT perpetuals.

- Direction comes only from closed 1h bars: price above EMA20 above EMA50 and
  rising EMA20 over three hours for LONG; exact inverse for SHORT.
- 24h rolling VWAP from 96 closed 15m typical-price/base-volume bars, same anchor
  definition as the existing laboratory. This is not an exchange session VWAP.
- Previous 15m candle intersects VWAP ±0.15 ATR and closes no more than 0.35 ATR
  through it. Last closed candle reclaims VWAP by 0.1–0.8 ATR, closes beyond the
  previous candle's extreme and has a body in the trend direction.
- Confirmation volume at least its prior 20-bar average. ATR/price at least 0.15%.
- Stop beyond the last three bars with 0.15 ATR buffer, at least 1.2 ATR and at
  most 2.5 ATR. Single 2R target, eight-hour limit. No partial exits/trailing.
- Same cost-aware 2 USDT risk, 4bps fees each side, 5bps adverse entry/exit,
  adverse funding buffer 1bp/8h, drift ceiling 0.35R, net target-space check,
  one position per symbol/strategy and 45-minute post-close cooldown as V2.
- Replay V2 and the new mechanism independently on the same closed bars and
  minute execution coverage. Open results excluded. Report net R/trade, closed
  drawdown and frequency, not just aggregate loss reduction. Repeat at 10bps
  adverse slippage with recomputed fills and sizing, not a flat PnL subtraction.
- First replay uses the previously saved 7–15 October snapshot (exploratory).
  Later data must be labelled separately; none is independently held out after
  the prior diagnostic. Do not tune the declared rules against either result.
- Deployment, if warranted by the check, is a new prospective shadow cohort
  alongside unchanged V2, with no orders or automatic promotion. Any code or
  data-quality failure is repaired and the same fixed test rerun; a negative
  strategy result is reported as negative rather than optimized away.
