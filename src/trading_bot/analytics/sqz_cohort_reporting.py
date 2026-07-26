"""P7-17 milestone reporting for isolated SQZ research cohorts.

This module observes closed paper and virtual-shadow trades only. It never
changes admission, risk, position sizing, or trade management.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import Any


logger = logging.getLogger(__name__)

SQZ_STRICT_CONTROL_SHADOW = "SQZ_STRICT_CONTROL_SHADOW"
SQZ_SHADOW_COHORTS = (
    SQZ_STRICT_CONTROL_SHADOW,
    "SQZ_OF_AGAINST_SHADOW",
    "SQZ_OF_HOSTILE_SHADOW",
    "SQZ_OF_ABSORPTION_SHADOW",
    "SQZ_RS_NEUTRAL_SHADOW",
    "SQZ_NO_RETEST_SHADOW",
)
SQZ_PAPER_MEASUREMENT = "SQUEEZE_BREAKOUT_OF_MEASURE"


@dataclass(frozen=True)
class CohortStats:
    strategy: str
    closed_trades: int
    avg_r: float
    ci95_low: float | None
    ci95_high: float | None
    profit_factor: float | None
    max_drawdown_r: float
    full_stop_rate: float


class SQZCohortMilestoneReporter:
    """Send one Telegram report per evidence milestone, persisted across restarts."""

    def __init__(
        self,
        database: Any,
        notifier: Any,
        *,
        stats_epoch: str | None,
        enabled: bool = True,
        check_interval_sec: int = 900,
    ) -> None:
        self._database = database
        self._notifier = notifier
        self._stats_epoch = stats_epoch
        self._enabled = enabled
        self._check_interval_sec = max(int(check_interval_sec), 60)
        self._next_check_at = 0.0
        self._lock = asyncio.Lock()

    async def maybe_report(self, paper_rows: list[dict[str, Any]] | None = None) -> None:
        if not self._enabled or not getattr(self._notifier, "enabled", False):
            return
        now = time.monotonic()
        if now < self._next_check_at:
            return
        async with self._lock:
            now = time.monotonic()
            if now < self._next_check_at:
                return
            self._next_check_at = now + self._check_interval_sec
            await self._report_due_milestones(paper_rows or [])

    async def _report_due_milestones(self, paper_rows: list[dict[str, Any]]) -> None:
        shadow_rows = await self._database.closed_shadow_trades_by_strategies(
            list(SQZ_SHADOW_COHORTS),
            self._stats_epoch,
        )
        paper_rows = [
            row
            for row in paper_rows
            if row.get("status") == "CLOSED"
            and _strategy_from_trade(row) == SQZ_PAPER_MEASUREMENT
            and _is_after_epoch(row.get("closed_at"), self._stats_epoch)
        ]

        by_shadow_strategy = {strategy: [] for strategy in SQZ_SHADOW_COHORTS}
        for row in shadow_rows:
            strategy = str(row.get("strategy") or "")
            if strategy in by_shadow_strategy:
                by_shadow_strategy[strategy].append(row)

        control = _summarize(SQZ_STRICT_CONTROL_SHADOW, by_shadow_strategy[SQZ_STRICT_CONTROL_SHADOW])
        for strategy, rows in by_shadow_strategy.items():
            await self._report_strategy_milestone(
                _summarize(strategy, rows),
                control=control,
                milestones=(20, 50, 100),
                source="SHADOW",
            )
        await self._report_strategy_milestone(
            _summarize(SQZ_PAPER_MEASUREMENT, paper_rows),
            control=None,
            milestones=(20, 150),
            source="PAPER",
        )

    async def _report_strategy_milestone(
        self,
        stats: CohortStats,
        *,
        control: CohortStats | None,
        milestones: tuple[int, ...],
        source: str,
    ) -> None:
        due = [milestone for milestone in milestones if stats.closed_trades >= milestone]
        if not due:
            return
        highest_due = max(due)
        state_key = self._state_key(stats.strategy, highest_due)
        if await self._database.load_operational_state(state_key):
            return

        status = _milestone_status(stats, highest_due, source)
        await self._notifier.send(
            _format_report(stats, control=control, milestone=highest_due, source=source, status=status),
            label=f"P7-17 {stats.strategy} {highest_due}",
        )

        # When the process was offline across several thresholds, send only the
        # most informative one and record earlier milestones as observed.
        payload = json.dumps(
            {
                "strategy": stats.strategy,
                "source": source,
                "closed_trades": stats.closed_trades,
                "status": status,
                "reported_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        )
        for milestone in due:
            await self._database.save_operational_state(self._state_key(stats.strategy, milestone), payload)
        logger.info(
            "P7-17 cohort milestone reported: strategy=%s source=%s threshold=%s closed=%s status=%s",
            stats.strategy,
            source,
            highest_due,
            stats.closed_trades,
            status,
        )

    def _state_key(self, strategy: str, milestone: int) -> str:
        epoch = self._stats_epoch or "all-history"
        return f"p7-17:{epoch}:{strategy}:{milestone}"


def _summarize(strategy: str, rows: list[dict[str, Any]]) -> CohortStats:
    values = [_r_value(row) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return CohortStats(strategy, 0, 0.0, None, None, None, 0.0, 0.0)
    positive = sum(value for value in values if value > 0)
    negative = abs(sum(value for value in values if value < 0))
    profit_factor = positive / negative if negative else None
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    low, high = _normal_ci(values)
    return CohortStats(
        strategy=strategy,
        closed_trades=len(values),
        avg_r=mean(values),
        ci95_low=low,
        ci95_high=high,
        profit_factor=profit_factor,
        max_drawdown_r=max_drawdown,
        full_stop_rate=sum(value <= -0.9 for value in values) / len(values),
    )


def _normal_ci(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    margin = 1.96 * stdev(values) / math.sqrt(len(values))
    average = mean(values)
    return average - margin, average + margin


def _r_value(row: dict[str, Any]) -> float | None:
    raw = row.get("r_multiple")
    if raw not in (None, ""):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    try:
        risk = float(row.get("risk_amount") or 0)
        return float(row.get("realized_pnl") or 0) / risk if risk > 0 else None
    except (TypeError, ValueError):
        return None


def _strategy_from_trade(row: dict[str, Any]) -> str:
    raw = row.get("metadata") or "{}"
    try:
        metadata = raw if isinstance(raw, dict) else json.loads(raw)
    except json.JSONDecodeError:
        return "UNKNOWN"
    if not isinstance(metadata, dict):
        return "UNKNOWN"
    signal_metadata = metadata.get("signal_metadata")
    if isinstance(signal_metadata, dict):
        metadata = signal_metadata
    return str(metadata.get("strategy") or "UNKNOWN").upper()


def _is_after_epoch(value: Any, epoch: str | None) -> bool:
    if not epoch:
        return True
    parsed_value = _parse_timestamp(value)
    parsed_epoch = _parse_timestamp(epoch)
    return bool(parsed_value and parsed_epoch and parsed_value >= parsed_epoch)


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _milestone_status(stats: CohortStats, milestone: int, source: str) -> str:
    if source == "PAPER" and milestone == 20 and (stats.avg_r <= -0.20 or stats.max_drawdown_r <= -3.0):
        return "STOP_REVIEW"
    if milestone >= 150:
        return "HUMAN_REVIEW"
    if milestone >= 100:
        return "HUMAN_REVIEW"
    if milestone >= 50:
        return "INTERIM_REVIEW"
    return "EARLY_SIGNAL"


def _format_report(
    stats: CohortStats,
    *,
    control: CohortStats | None,
    milestone: int,
    source: str,
    status: str,
) -> str:
    ci = "n/a" if stats.ci95_low is None else f"[{stats.ci95_low:+.3f}; {stats.ci95_high:+.3f}]"
    pf = "n/a" if stats.profit_factor is None else f"{stats.profit_factor:.2f}"
    lines = [
        "<b>P7-17: SQZ evidence checkpoint</b>",
        f"Cohort: <code>{stats.strategy}</code> ({source})",
        f"Closed: <b>{stats.closed_trades}</b> | threshold: {milestone} | status: <b>{status}</b>",
        f"Avg R: <b>{stats.avg_r:+.3f}</b> | 95% CI: <code>{ci}</code>",
        f"PF: {pf} | DD: {stats.max_drawdown_r:+.2f}R | full stops: {stats.full_stop_rate:.0%}",
    ]
    if control and control.closed_trades:
        lines.append(
            f"Virtual control: n={control.closed_trades}, Avg R={control.avg_r:+.3f}, "
            f"PF={_format_pf(control.profit_factor)}"
        )
    elif control:
        lines.append("Virtual control: no closed trades yet")
    lines.append("Research alert only: no automatic promotion, sizing, or gate change.")
    return "\n".join(lines)


def _format_pf(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"
