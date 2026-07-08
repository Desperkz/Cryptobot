"""Отчёт по MFE/MAE: как далеко сделки ходят до разворота.

Читает mfe_r / mae_r из metadata закрытых сделок (пишутся paper_monitor_v2
после деплоя июль-2026) и отвечает на главный вопрос проектирования выходов:
какую долю победителей и на каком уровне R реально можно забрать.

Использование:
    python scripts/mfe_mae_report.py
    python scripts/mfe_mae_report.py --db data/trading_bot_v2_1.sqlite3 --table shadow_trades
    python scripts/mfe_mae_report.py --since 2026-07-10 --strategy SQUEEZE_BREAKOUT
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path(os.getenv("PAPER_DB_PATH", "/root/bot_v2_1/data/trading_bot_v2_1.sqlite3"))
R_LEVELS = [0.5, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0]


def _f(raw: object) -> float | None:
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return None


def load_rows(db: Path, table: str, since: str | None, strategy: str | None) -> list[dict]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT closed_at, strategy, direction, realized_pnl, r_multiple, metadata "
        f"FROM {table} WHERE status='CLOSED' AND closed_at IS NOT NULL"
    ).fetchall()
    conn.close()
    out = []
    for row in rows:
        if since and str(row["closed_at"])[:10] < since:
            continue
        if strategy and str(row["strategy"] or "").upper() != strategy.upper():
            continue
        try:
            meta = json.loads(row["metadata"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        mfe, mae = _f(meta.get("mfe_r")), _f(meta.get("mae_r"))
        if mfe is None and mae is None:
            continue
        out.append({
            "strategy": str(row["strategy"] or "").upper(),
            "r": _f(row["r_multiple"]) or 0.0,
            "win": (_f(row["realized_pnl"]) or 0.0) > 0,
            "mfe": mfe if mfe is not None else 0.0,
            "mae": mae if mae is not None else 0.0,
        })
    return out


def quantiles(values: list[float], qs: tuple[float, ...] = (0.25, 0.5, 0.75, 0.9)) -> list[float]:
    if not values:
        return [0.0] * len(qs)
    data = sorted(values)
    result = []
    for q in qs:
        idx = min(len(data) - 1, max(0, int(round(q * (len(data) - 1)))))
        result.append(data[idx])
    return result


def report(rows: list[dict]) -> None:
    if not rows:
        print("Нет сделок с записанными mfe_r/mae_r. Данные пишутся paper_monitor_v2")
        print("после деплоя фиксов июль-2026 — дай системе поторговать.")
        return
    line = "-" * 66
    print(line)
    print(f"MFE/MAE-ОТЧЁТ: {len(rows)} закрытых сделок с данными экскурсий")
    print(line)

    mfe_all = [r["mfe"] for r in rows]
    mae_all = [r["mae"] for r in rows]
    q_labels = "p25 / p50 / p75 / p90"
    print(f"MFE (макс. ход в плюс), R  [{q_labels}]: " + " / ".join(f"{v:.2f}" for v in quantiles(mfe_all)))
    print(f"MAE (макс. ход в минус), R [{q_labels}]: " + " / ".join(f"{v:.2f}" for v in quantiles(mae_all)))
    print(line)

    print("Доля сделок, чей MFE достиг уровня (потолок winrate для TP на уровне):")
    for level in R_LEVELS:
        share = sum(1 for r in rows if r["mfe"] >= level) / len(rows) * 100
        print(f"  >= {level:.1f}R : {share:5.1f}%")
    print(line)

    losers = [r for r in rows if not r["win"]]
    if losers:
        near_miss = sum(1 for r in losers if r["mfe"] >= 0.8)
        print(f"Лузеры, которые сходили в +0.8R и вернулись в минус: {near_miss}/{len(losers)}")
        print("  (высокая доля -> ранний BE/частичный тейк оправдан; низкая -> вреден)")
    winners = [r for r in rows if r["win"]]
    if winners:
        giveback = [max(0.0, r["mfe"] - r["r"]) for r in winners]
        print(f"Победители: медиана отданного от пика (MFE - итоговый R): {quantiles(giveback)[1]:.2f}R")
        print("  (много отдают -> трейлинг слишком широкий; почти ничего -> слишком узкий)")
    print(line)

    strategies = sorted({r["strategy"] for r in rows})
    if len(strategies) > 1:
        print(f"{'стратегия':<32}{'n':>5}{'MFE p50':>10}{'MAE p50':>10}")
        for name in strategies:
            grp = [r for r in rows if r["strategy"] == name]
            print(f"{name:<32}{len(grp):>5}{quantiles([r['mfe'] for r in grp])[1]:>10.2f}{quantiles([r['mae'] for r in grp])[1]:>10.2f}")
        print(line)
    if len(rows) < 50:
        print(f"ВНИМАНИЕ: всего {len(rows)} сделок (< 50) — выводы предварительные.")
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="Отчёт по MFE/MAE закрытых сделок.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--table", default="trades", choices=["trades", "shadow_trades"])
    parser.add_argument("--since", default=None, help="Учитывать сделки, закрытые не раньше даты YYYY-MM-DD")
    parser.add_argument("--strategy", default=None)
    args = parser.parse_args()
    if args.since:
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print(f"Неверный формат --since: {args.since}", file=sys.stderr)
            return 2
    if not args.db.exists():
        print(f"БД не найдена: {args.db}", file=sys.stderr)
        return 2
    report(load_rows(args.db, args.table, args.since, args.strategy))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
