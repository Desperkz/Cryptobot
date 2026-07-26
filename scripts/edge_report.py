"""Honest edge measurement: expectancy with confidence intervals.

The bot already produces plenty of aggregate numbers. What it never produced is
the only number that matters before risking real money: *is the measured edge
distinguishable from zero, and is the sample large enough to say so?*

Usage:
    python scripts/edge_report.py --db data/trading_bot_v2_1.sqlite3
    python scripts/edge_report.py --db data/... --epoch 2026-07-08T06:16:58+00:00
    python scripts/edge_report.py --db data/... --target-monthly 0.10

Outputs:
  1. Expectancy in R with a 95% confidence interval (bootstrap, no normality
     assumption -- R distributions are strongly bimodal).
  2. Required sample size to prove the observed edge.
  3. SQZ cohort comparison: strict control vs the isolated weak-mixed OF bucket.
  4. Parallel SQZ shadow gate cohorts against a strict virtual control.
  4. Feasibility check of the monthly return target against observed frequency.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev

Z95 = 1.959963985


def _bootstrap_ci(values: list[float], iterations: int = 20000, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean. Deterministic seed for reproducibility."""
    import random

    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(20260724)
    n = len(values)
    means = []
    for _ in range(iterations):
        means.append(sum(rng.choice(values) for _ in range(n)) / n)
    means.sort()
    lo = means[int(iterations * alpha / 2)]
    hi = means[int(iterations * (1 - alpha / 2)) - 1]
    return (lo, hi)


def _required_n(expectancy: float, sigma: float) -> int | None:
    """Sample size needed for the 95% CI to exclude zero at this effect size."""
    if expectancy <= 0 or sigma <= 0:
        return None
    return math.ceil((Z95 * sigma / expectancy) ** 2)


def _load_closed_rows(db_path: Path, table: str, epoch: str | None) -> list[dict]:
    if table not in {"trades", "shadow_trades"}:
        raise ValueError(f"unsupported table: {table}")
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    query = f"SELECT * FROM {table} WHERE status = 'CLOSED'"
    params: list = []
    if epoch:
        query += " AND closed_at >= ?"
        params.append(epoch.replace("T", " ").replace("+00:00", ""))
    rows = [dict(row) for row in con.execute(query, params)]
    con.close()
    return rows


def _load_trades(db_path: Path, epoch: str | None) -> list[dict]:
    return _load_closed_rows(db_path, "trades", epoch)


def _load_shadow_trades(db_path: Path, epoch: str | None) -> list[dict]:
    return _load_closed_rows(db_path, "shadow_trades", epoch)


def _r_values(trades: list[dict]) -> list[float]:
    out = []
    for trade in trades:
        raw = trade.get("r_multiple")
        if raw not in (None, ""):
            out.append(float(raw))
            continue
        risk = float(trade.get("risk_amount") or 0)
        if risk > 0:
            out.append(float(trade.get("realized_pnl") or 0) / risk)
    return out


def _signal_metadata(trade: dict) -> dict:
    try:
        metadata = json.loads(trade.get("metadata") or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(metadata, dict):
        return {}
    signal_metadata = metadata.get("signal_metadata")
    return signal_metadata if isinstance(signal_metadata, dict) else metadata


def _trade_strategy(trade: dict) -> str:
    metadata = _signal_metadata(trade)
    return str(metadata.get("strategy") or "UNKNOWN").upper()


def _describe(label: str, values: list[float]) -> dict:
    if not values:
        return {"label": label, "trades": 0}
    n = len(values)
    avg = mean(values)
    sigma = pstdev(values) if n > 1 else 0.0
    lo, hi = _bootstrap_ci(values)
    wins = [v for v in values if v > 0]
    losses = [abs(v) for v in values if v < 0]
    return {
        "label": label,
        "trades": n,
        "expectancy_r": round(avg, 4),
        "sigma_r": round(sigma, 4),
        "ci95_low": round(lo, 4),
        "ci95_high": round(hi, 4),
        "edge_proven": lo > 0,
        "winrate": round(len(wins) / n, 4),
        "payoff": round(mean(wins) / mean(losses), 3) if wins and losses else None,
        "required_n_to_prove": _required_n(avg, sigma),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--epoch", default=None, help="ISO timestamp; ignore trades closed before it")
    parser.add_argument("--target-monthly", type=float, default=0.10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    trades = _load_trades(args.db, args.epoch)
    shadow_trades = _load_shadow_trades(args.db, args.epoch)
    if not trades and not shadow_trades:
        print("No closed trades found for the requested window.")
        return

    overall = _describe("ALL", _r_values(trades))

    # --- SQZ control vs experiment ---------------------------------------
    # The experiment is a deliberately narrow weak-mixed OF cohort, not a
    # global order-flow bypass and not a randomized A/B test. Keep it separate
    # from the strict control until it has enough post-cost closed clusters.
    strict_sqz, weak_mixed_sqz = [], []
    for trade in trades:
        r_list = _r_values([trade])
        if not r_list:
            continue
        strategy = _trade_strategy(trade)
        if strategy == "SQUEEZE_BREAKOUT":
            strict_sqz.append(r_list[0])
        elif strategy == "SQUEEZE_BREAKOUT_OF_MEASURE":
            weak_mixed_sqz.append(r_list[0])

    sqz_cohorts = [
        _describe("SQZ_STRICT_CONTROL", strict_sqz),
        _describe("SQZ_WEAK_MIXED_MEASURE", weak_mixed_sqz),
    ]

    shadow_cohort_names = [
        "SQZ_STRICT_CONTROL_SHADOW",
        "SQZ_OF_AGAINST_SHADOW",
        "SQZ_OF_HOSTILE_SHADOW",
        "SQZ_OF_ABSORPTION_SHADOW",
        "SQZ_RS_NEUTRAL_SHADOW",
        "SQZ_NO_RETEST_SHADOW",
    ]
    shadow_sqz_gate_cohorts = []
    for cohort_name in shadow_cohort_names:
        values = [
            r_value[0]
            for trade in shadow_trades
            if _trade_strategy(trade) == cohort_name
            for r_value in [_r_values([trade])]
            if r_value
        ]
        shadow_sqz_gate_cohorts.append(_describe(cohort_name, values))

    # --- Target feasibility ----------------------------------------------
    stamps = []
    for trade in trades:
        raw = trade.get("created_at")
        if raw:
            try:
                stamps.append(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
            except ValueError:
                pass
    span_days = max((max(stamps) - min(stamps)).days, 1) if len(stamps) > 1 else 1
    per_month = len(trades) / span_days * 30.0

    risks = [float(t.get("risk_amount") or 0) for t in trades if float(t.get("risk_amount") or 0) > 0]
    # risk_amount is absolute; express it as a fraction using observed equity proxy
    avg_risk = mean(risks) if risks else 0.0

    feasibility = {
        "observed_trades_per_month": round(per_month, 2),
        "avg_risk_amount_usdt": round(avg_risk, 2),
        "target_monthly_return": args.target_monthly,
        "required_expectancy_r_by_risk_pct": {
            f"{pct:.2%}": round(args.target_monthly / (per_month * pct), 3)
            for pct in (0.0025, 0.005, 0.01, 0.02, 0.03)
            if per_month > 0
        },
    }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": str(args.db),
        "epoch": args.epoch,
        "overall": overall,
        "sqz_control_vs_measurement": sqz_cohorts,
        "shadow_sqz_gate_cohorts": shadow_sqz_gate_cohorts,
        "target_feasibility": feasibility,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    print(f"=== EDGE REPORT ({overall['trades']} closed paper trades) ===")
    if overall["trades"]:
        print(f"  E[R]            : {overall['expectancy_r']:+.3f}")
        print(f"  95% CI          : [{overall['ci95_low']:+.3f}, {overall['ci95_high']:+.3f}]")
        print(f"  Winrate/payoff  : {overall['winrate']:.1%} / {overall['payoff']}")
        verdict = "PROVEN (CI excludes zero)" if overall["edge_proven"] else "NOT PROVEN (CI includes zero)"
        print(f"  Verdict         : {verdict}")
        if overall["required_n_to_prove"]:
            print(f"  Trades needed   : {overall['required_n_to_prove']} (have {overall['trades']})")
    else:
        print("  No closed paper trades in this window; shadow cohorts are reported below.")
    print()
    print("=== SQZ CONTROL VS MEASUREMENT COHORT ===")
    for block in sqz_cohorts:
        if block["trades"] == 0:
            print(f"  {block['label']:<14}: no data")
            continue
        print(
            f"  {block['label']:<14}: n={block['trades']:<4} E[R]={block['expectancy_r']:+.3f} "
            f"CI=[{block['ci95_low']:+.3f}, {block['ci95_high']:+.3f}]"
        )
    print("  -> The weak-mixed bucket is paper-only; compare it to strict SQZ only on adequate n.")
    print()
    print("=== SQZ SHADOW GATE COHORTS ===")
    for block in shadow_sqz_gate_cohorts:
        if block["trades"] == 0:
            print(f"  {block['label']:<29}: no data")
            continue
        print(
            f"  {block['label']:<29}: n={block['trades']:<4} E[R]={block['expectancy_r']:+.3f} "
            f"CI=[{block['ci95_low']:+.3f}, {block['ci95_high']:+.3f}]"
        )
    print("  -> Virtual-execution research only: compare each relaxed gate with SQZ_STRICT_CONTROL_SHADOW.")
    print()
    print("=== TARGET FEASIBILITY ===")
    print(f"  Observed frequency : {feasibility['observed_trades_per_month']} trades/month")
    print(f"  Target             : {args.target_monthly:.0%}/month")
    print("  Required E[R] per risk level:")
    for pct, needed in feasibility["required_expectancy_r_by_risk_pct"].items():
        flag = "  <-- not achievable" if needed > 0.35 else ""
        print(f"    risk {pct:>7} -> E[R] = {needed:+.2f} R{flag}")


if __name__ == "__main__":
    main()
