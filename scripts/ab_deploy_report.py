"""A/B-отчёт: сравнение статистики сделок до и после даты деплоя.

Сравнивает закрытые сделки двух периодов (BEFORE / AFTER относительно
--deploy-date) по ключевым метрикам и проверяет статистическую значимость
разницы матожидания (avg R на сделку) бутстрап-тестом — без scipy.

Использование:
    python scripts/ab_deploy_report.py --deploy-date 2026-07-08
    python scripts/ab_deploy_report.py --deploy-date 2026-07-08 --db data/trading_bot_v2_1.sqlite3
    python scripts/ab_deploy_report.py --deploy-date 2026-07-08 --csv trades_enriched.csv
    python scripts/ab_deploy_report.py --deploy-date 2026-07-08 --table shadow_trades --strategy SQUEEZE_BREAKOUT

БД по умолчанию берётся из PAPER_DB_PATH (как в paper_monitor_v2).
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(os.getenv("PAPER_DB_PATH", "/root/bot_v2_1/data/trading_bot_v2_1.sqlite3"))
MIN_RELIABLE_N = 50
BOOTSTRAP_ITERATIONS = 20_000
DEPOSIT_USDT_DEFAULT = 1000.0


@dataclass
class Trade:
    closed_at: datetime
    direction: str
    strategy: str
    realized_pnl: float
    r_multiple: float


def _parse_dt(raw: object) -> datetime | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 7], fmt)
        except ValueError:
            continue
    return None


def _float(raw: object) -> float:
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return 0.0


def load_from_db(db_path: Path, table: str) -> list[Trade]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT closed_at, direction, strategy, realized_pnl, r_multiple "
        f"FROM {table} WHERE status = 'CLOSED' AND closed_at IS NOT NULL"
    ).fetchall()
    conn.close()
    return _rows_to_trades(rows)


def load_from_csv(csv_path: Path) -> list[Trade]:
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        rows = [row for row in csv.DictReader(handle) if str(row.get("status", "")).upper() == "CLOSED"]
    return _rows_to_trades(rows)


def _rows_to_trades(rows: list) -> list[Trade]:
    trades: list[Trade] = []
    for row in rows:
        closed_at = _parse_dt(row["closed_at"])
        if closed_at is None:
            continue
        trades.append(
            Trade(
                closed_at=closed_at,
                direction=str(row["direction"] or "").upper(),
                strategy=str(row["strategy"] or "").upper(),
                realized_pnl=_float(row["realized_pnl"]),
                r_multiple=_float(row["r_multiple"]),
            )
        )
    trades.sort(key=lambda item: item.closed_at)
    return trades


@dataclass
class GroupStats:
    n: int
    winrate: float
    avg_r: float
    total_r: float
    pnl: float
    avg_win_r: float
    avg_loss_r: float
    payoff: float
    short_share: float
    short_pnl: float
    long_pnl: float
    span_days: float


def compute_stats(trades: list[Trade]) -> GroupStats | None:
    if not trades:
        return None
    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl <= 0]
    shorts = [t for t in trades if t.direction == "SHORT"]
    longs = [t for t in trades if t.direction == "LONG"]
    avg_win_r = sum(t.r_multiple for t in wins) / len(wins) if wins else 0.0
    avg_loss_r = sum(t.r_multiple for t in losses) / len(losses) if losses else 0.0
    span = (trades[-1].closed_at - trades[0].closed_at).total_seconds() / 86400 if len(trades) > 1 else 0.0
    return GroupStats(
        n=len(trades),
        winrate=len(wins) / len(trades) * 100,
        avg_r=sum(t.r_multiple for t in trades) / len(trades),
        total_r=sum(t.r_multiple for t in trades),
        pnl=sum(t.realized_pnl for t in trades),
        avg_win_r=avg_win_r,
        avg_loss_r=avg_loss_r,
        payoff=abs(avg_win_r / avg_loss_r) if avg_loss_r else float("inf"),
        short_share=len(shorts) / len(trades) * 100,
        short_pnl=sum(t.realized_pnl for t in shorts),
        long_pnl=sum(t.realized_pnl for t in longs),
        span_days=span,
    )


def bootstrap_test(before_r: list[float], after_r: list[float], iterations: int = BOOTSTRAP_ITERATIONS) -> tuple[float, tuple[float, float]]:
    """Возвращает (p-value односторонний: after > before, 95% CI avg R after).

    Перестановочный бутстрап разницы средних: p-value — доля случайных
    перестановок, в которых разница средних >= наблюдаемой.
    """
    rng = random.Random(42)
    observed = (sum(after_r) / len(after_r)) - (sum(before_r) / len(before_r))
    pooled = before_r + after_r
    n_after = len(after_r)
    exceed = 0
    for _ in range(iterations):
        sample = rng.sample(pooled, n_after)
        rest_sum = sum(pooled) - sum(sample)
        diff = sum(sample) / n_after - rest_sum / (len(pooled) - n_after)
        if diff >= observed:
            exceed += 1
    p_value = exceed / iterations

    # Бутстрап 95% CI матожидания after-группы
    means: list[float] = []
    for _ in range(iterations // 4):
        resample = [rng.choice(after_r) for _ in range(n_after)]
        means.append(sum(resample) / n_after)
    means.sort()
    ci = (means[int(len(means) * 0.025)], means[int(len(means) * 0.975)])
    return p_value, ci


def monthly_projection(stats: GroupStats, deposit: float) -> tuple[float, float]:
    if stats.span_days <= 0:
        return 0.0, 0.0
    trades_per_month = stats.n / stats.span_days * 30.4
    pnl_per_month = stats.pnl / stats.span_days * 30.4
    return trades_per_month, pnl_per_month / deposit * 100


def fmt(value: float, digits: int = 2) -> str:
    return f"{value:+.{digits}f}" if not math.isnan(value) else "n/a"


def print_report(before: GroupStats | None, after: GroupStats | None, deploy: datetime, deposit: float, before_r: list[float], after_r: list[float]) -> None:
    line = "-" * 62
    print(line)
    print(f"A/B-ОТЧЁТ ПО ДЕПЛОЮ {deploy.date()}  (депозит {deposit:.0f} USDT)")
    print(line)
    header = f"{'метрика':<28}{'BEFORE':>15}{'AFTER':>15}"
    print(header)
    print(line)

    def row(name: str, getter, digits: int = 2, suffix: str = "") -> None:
        left = f"{getter(before):.{digits}f}{suffix}" if before else "—"
        right = f"{getter(after):.{digits}f}{suffix}" if after else "—"
        print(f"{name:<28}{left:>15}{right:>15}")

    row("сделок закрыто", lambda s: s.n, 0)
    row("winrate, %", lambda s: s.winrate, 1)
    row("avg R на сделку", lambda s: s.avg_r, 3)
    row("суммарный R", lambda s: s.total_r, 2)
    row("PnL, USD", lambda s: s.pnl, 2)
    row("avg win R", lambda s: s.avg_win_r, 2)
    row("avg loss R", lambda s: s.avg_loss_r, 2)
    row("payoff (|win/loss| R)", lambda s: s.payoff, 2)
    row("доля шортов, %", lambda s: s.short_share, 1)
    row("PnL шортов, USD", lambda s: s.short_pnl, 2)
    row("PnL лонгов, USD", lambda s: s.long_pnl, 2)
    print(line)

    if after:
        tpm, monthly_pct = monthly_projection(after, deposit)
        print(f"Темп AFTER: ~{tpm:.0f} сделок/мес, проекция {fmt(monthly_pct, 1)}% к депозиту в месяц")

    if before and after and before.n >= 5 and after.n >= 5:
        p_value, ci = bootstrap_test(before_r, after_r)
        print(f"Разница avg R: {fmt(after.avg_r - before.avg_r, 3)} | p-value (перестановочный тест): {p_value:.3f}")
        print(f"95% CI матожидания AFTER: [{ci[0]:+.3f} … {ci[1]:+.3f}] R")
        if p_value < 0.05 and ci[0] > 0:
            print("ВЫВОД: улучшение статистически значимо И матожидание положительно.")
        elif p_value < 0.05:
            print("ВЫВОД: улучшение значимо, но CI матожидания ещё захватывает ноль —")
            print("       система лучше прежней, но её прибыльность пока не доказана.")
        else:
            print("ВЫВОД: разница пока НЕ отличима от шума. Продолжай сбор данных,")
            print("       ничего не меняя в конфигурации.")
    else:
        print("Недостаточно данных для теста значимости (нужно >=5 сделок в каждой группе).")

    if after and after.n < MIN_RELIABLE_N:
        print(f"ВНИМАНИЕ: в AFTER-группе {after.n} сделок (< {MIN_RELIABLE_N}) — любые выводы предварительные.")
    print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="A/B-сравнение статистики сделок до/после деплоя.")
    parser.add_argument("--deploy-date", required=True, help="Дата деплоя фиксов, YYYY-MM-DD (граница групп)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"Путь к SQLite БД (по умолчанию {DEFAULT_DB})")
    parser.add_argument("--csv", type=Path, default=None, help="Читать из CSV-экспорта вместо БД")
    parser.add_argument("--table", default="trades", choices=["trades", "shadow_trades"], help="Таблица БД")
    parser.add_argument("--strategy", default=None, help="Фильтр по стратегии (например SQUEEZE_BREAKOUT)")
    parser.add_argument("--deposit", type=float, default=DEPOSIT_USDT_DEFAULT, help="Депозит USDT для проекции %/мес")
    parser.add_argument("--before-days", type=int, default=None, help="Ограничить BEFORE последними N днями до деплоя")
    args = parser.parse_args()

    deploy = _parse_dt(args.deploy_date)
    if deploy is None:
        print(f"Не удалось разобрать дату: {args.deploy_date}", file=sys.stderr)
        return 2

    if args.csv:
        trades = load_from_csv(args.csv)
    else:
        if not args.db.exists():
            print(f"БД не найдена: {args.db}. Укажи --db или --csv.", file=sys.stderr)
            return 2
        trades = load_from_db(args.db, args.table)

    if args.strategy:
        trades = [t for t in trades if t.strategy == args.strategy.upper()]

    before = [t for t in trades if t.closed_at < deploy]
    after = [t for t in trades if t.closed_at >= deploy]
    if args.before_days:
        cutoff = deploy.timestamp() - args.before_days * 86400
        before = [t for t in before if t.closed_at.timestamp() >= cutoff]

    print_report(
        compute_stats(before),
        compute_stats(after),
        deploy,
        args.deposit,
        [t.r_multiple for t in before],
        [t.r_multiple for t in after],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
