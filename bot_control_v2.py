"""
Bot v2 Control API — порт 8890
Отдаёт данные для дашборда: позиции, статистику, сигналы, squeeze статус.
"""
from __future__ import annotations

import http.server
import json
import os
import secrets
import socketserver
import sqlite3
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.operational import start_watchdog_thread

HOST = os.getenv("BOT_CONTROL_HOST", "127.0.0.1")
PORT = int(os.getenv("BOT_CONTROL_PORT", "8890"))
CONTROL_TOKEN = os.getenv("BOT_CONTROL_TOKEN", "")
CORS_ORIGIN = os.getenv("BOT_CONTROL_CORS_ORIGIN", "http://127.0.0.1")
ALLOW_UNSAFE_PUBLIC = os.getenv("BOT_ALLOW_UNSAFE_PUBLIC", "0") == "1"
BOT_ROOT = os.getenv("BOT_ROOT", "/root/bot_v2")
DB_PATH = os.getenv("BOT_DB_PATH", f"{BOT_ROOT}/data/trading_bot.sqlite3")
LOG_PATH = os.getenv("BOT_LOG_PATH", f"{BOT_ROOT}/logs/trading_bot.log")
EMERGENCY_STOP_FILE = os.getenv("BOT_EMERGENCY_STOP_FILE", f"{BOT_ROOT}/data/emergency_stop.flag")
DASHBOARD_PATH = os.getenv("BOT_DASHBOARD_PATH", f"{BOT_ROOT}/dashboard_v2.html")
BOT_SERVICE_NAME = os.getenv("BOT_SERVICE_NAME", "trading-bot-v2")
PAPER_MONITOR_SERVICE_NAME = os.getenv("PAPER_MONITOR_SERVICE_NAME", "paper-monitor-v2")
BINANCE_URL = "https://fapi.binance.com/fapi/v1/ticker/price?symbol="
OPEN_TRADE_STATUSES = {"ACCEPTED", "OPEN", "ACTIVE"}
CONTROL_REQUEST_TIMEOUT_SECONDS = float(os.getenv("BOT_CONTROL_REQUEST_TIMEOUT_SECONDS", "5"))
SERVICE_STATUS_TIMEOUT_SECONDS = float(os.getenv("BOT_SERVICE_STATUS_TIMEOUT_SECONDS", "1.5"))
SERVICE_STATUS_CACHE_SECONDS = float(os.getenv("BOT_SERVICE_STATUS_CACHE_SECONDS", "2.0"))
PRICE_REQUEST_TIMEOUT_SECONDS = float(os.getenv("BOT_PRICE_REQUEST_TIMEOUT_SECONDS", "0.8"))
STRATEGY_GATE_DEFAULTS = {
    "min_closed_trades": int(os.getenv("STRATEGY_GATE_MIN_CLOSED_TRADES", "100")),
    "min_sample_age_days": float(os.getenv("STRATEGY_GATE_MIN_SAMPLE_AGE_DAYS", "7")),
    "min_closed_trades_per_day": float(os.getenv("STRATEGY_GATE_MIN_CLOSED_TRADES_PER_DAY", "0.10")),
    "min_winrate": float(os.getenv("STRATEGY_GATE_MIN_WINRATE", "40")),
    "min_profit_factor": float(os.getenv("STRATEGY_GATE_MIN_PROFIT_FACTOR", "1.25")),
    "min_avg_r": float(os.getenv("STRATEGY_GATE_MIN_AVG_R", "0")),
    "max_drawdown": float(os.getenv("STRATEGY_GATE_MAX_DRAWDOWN", "-10")),
}
SCORECARD_CLUSTER_WINDOW_MINUTES = int(os.getenv("SCORECARD_CLUSTER_WINDOW_MINUTES", "60"))
SCORECARD_CACHE_SECONDS = float(os.getenv("SCORECARD_CACHE_SECONDS", "30"))
SCORECARD_DIAGNOSTIC_LIMIT = int(os.getenv("SCORECARD_DIAGNOSTIC_LIMIT", "1500"))
SHADOW_GATE_DEFAULTS = {
    "min_closed_trades": int(os.getenv("SHADOW_GATE_MIN_CLOSED_TRADES", "30")),
    "min_sample_age_days": float(os.getenv("SHADOW_GATE_MIN_SAMPLE_AGE_DAYS", "3")),
    "min_winrate": float(os.getenv("SHADOW_GATE_MIN_WINRATE", "45")),
    "min_profit_factor": float(os.getenv("SHADOW_GATE_MIN_PROFIT_FACTOR", "1.25")),
    "min_avg_r": float(os.getenv("SHADOW_GATE_MIN_AVG_R", "0")),
    "max_drawdown": float(os.getenv("SHADOW_GATE_MAX_DRAWDOWN", "-10")),
}
_SCORECARD_CACHE_CONDITION = threading.Condition()
_SCORECARD_CACHE: dict[str, Any] = {"expires_at": 0.0, "data": None}
_SCORECARD_BUILDING = False
ALLOCATOR_WEIGHT_CAPS = {
    "CORE_CANDIDATE": float(os.getenv("ALLOCATOR_CORE_CAP_PCT", "60")),
    "CHAMPION_WATCH": float(os.getenv("ALLOCATOR_CHAMPION_WATCH_CAP_PCT", "45")),
    "KEEP_LIMITED_PAPER": float(os.getenv("ALLOCATOR_LIMITED_PAPER_CAP_PCT", "30")),
    "PROMOTION_REVIEW": float(os.getenv("ALLOCATOR_PROMOTION_REVIEW_CAP_PCT", "10")),
    "WATCH_SHADOW": float(os.getenv("ALLOCATOR_SHADOW_WATCH_CAP_PCT", "5")),
}
SQZ_GATE_COHORT_SHADOW_STRATEGIES = {
    "SQZ_STRICT_CONTROL_SHADOW",
    "SQZ_OF_AGAINST_SHADOW",
    "SQZ_OF_HOSTILE_SHADOW",
    "SQZ_OF_ABSORPTION_SHADOW",
    "SQZ_RS_NEUTRAL_SHADOW",
    "SQZ_NO_RETEST_SHADOW",
}

SHADOW_GATE_COUNTERFACTUAL_SUFFIXES = (
    "_RS_NEUTRAL_SHADOW",
    "_RS_AGAINST_SHADOW",
    "_MISSING_OI_SHADOW",
    "_NO_RETEST_SHADOW",
    "_NEAR_LIQUIDITY_SHADOW",
)


def _is_parallel_shadow_lab_strategy(strategy: str) -> bool:
    strategy_key = str(strategy or "").upper()
    return "_LAB_" in strategy_key and strategy_key.endswith("_SHADOW")
STRATEGY_PROMOTION_POLICIES = {
    "SQUEEZE_BREAKOUT": {
        "tier": "CHAMPION",
        "allowed_modes": ["paper", "live"],
        "paper_promotion": "HUMAN_REVIEW_AFTER_POST_P5_GATE",
        "live_promotion": "HUMAN_REVIEW_AFTER_TESTNET_AND_POST_P5_GATE",
        "min_post_p5_clusters": STRATEGY_GATE_DEFAULTS["min_closed_trades"],
        "notes": [
            "Champion strategy, but live sizing still depends on post-P5 realistic evidence.",
            "Do not replace the current SQZ line with challenger variants without separate evidence.",
        ],
    },
    "SQUEEZE_BREAKOUT_OF_MEASURE": {
        "tier": "MEASUREMENT",
        "allowed_modes": ["paper"],
        "paper_promotion": "ACTIVE_COHORT_COMPARISON_150_CLOSED_TRADES",
        "live_promotion": "BLOCKED_MEASUREMENT_BUCKET",
        "min_post_p5_clusters": 150,
        "notes": [
            "Separate weak-mixed order-flow SQZ bucket; strict SQZ remains the control.",
            "Only retest, structure-break and relative-strength-confirmed entries are admitted.",
            "Do not merge or promote this evidence until the 150-trade cohort review.",
        ],
    },
    "SQUEEZE_BREAKOUT_DYNAMIC": {
        "tier": "CHALLENGER",
        "allowed_modes": ["shadow"],
        "paper_promotion": "HUMAN_REVIEW_AFTER_SHADOW_GATE",
        "live_promotion": "BLOCKED_UNTIL_PAPER_PROVEN",
        "min_post_p5_clusters": STRATEGY_GATE_DEFAULTS["min_closed_trades"],
        "notes": ["Separate SQZ challenger; never auto-promote over the champion."],
    },
    "SQUEEZE_BREAKOUT_DYNAMIC_NEUTRAL_RS": {
        "tier": "RESEARCH",
        "allowed_modes": ["shadow"],
        "paper_promotion": "REQUIRE_SEPARATE_SHADOW_BUCKET_REVIEW",
        "live_promotion": "BLOCKED_UNTIL_PAPER_PROVEN",
        "min_post_p5_clusters": SHADOW_GATE_DEFAULTS["min_closed_trades"],
        "notes": [
            "Neutral relative-strength SQZ-DYN retests are a separate 0.25% shadow bucket.",
            "Never merge its evidence with base SQZ-DYN or the paper champion.",
        ],
    },
    "SQUEEZE_BREAKOUT_DYNAMIC_UPD": {
        "tier": "CHALLENGER",
        "allowed_modes": ["shadow"],
        "paper_promotion": "HUMAN_REVIEW_AFTER_SHADOW_GATE",
        "live_promotion": "BLOCKED_UNTIL_PAPER_PROVEN",
        "min_post_p5_clusters": STRATEGY_GATE_DEFAULTS["min_closed_trades"],
        "notes": [
            "Updated SQZ-DYN challenger with no-retest near-liquidity guard and same-direction cluster cap.",
            "Compare against SQZ-DYN before any paper discussion.",
        ],
    },
    "MEAN_REVERSION": {
        "tier": "PAPER_ONLY",
        "allowed_modes": ["paper", "shadow"],
        "paper_promotion": "COLLECT_200_CLOSED_TRADES_AND_WALK_FORWARD",
        "live_promotion": "BLOCKED_UNTIL_WALK_FORWARD",
        "review_milestone_clusters": 30,
        "min_post_p5_clusters": 200,
        "notes": [
            "MR can die against liquidation cascades and squeeze trends.",
            "At 30 post-P5 clusters, review MR allocation; do not promote to live from that sample.",
            "Require positive walk-forward before any live discussion.",
        ],
    },
    "TREND_PULLBACK": {
        "tier": "SHADOW_REVIEW",
        "allowed_modes": ["shadow", "paper"],
        "paper_promotion": "HUMAN_REVIEW_AFTER_SHADOW_GATE",
        "live_promotion": "BLOCKED_UNTIL_PAPER_PROVEN",
        "min_shadow_trades": SHADOW_GATE_DEFAULTS["min_closed_trades"],
        "paper_trial": {
            "duration_days": 30,
            "max_allocation_pct": 5,
            "max_strategy_risk_pct": 0.016,
            "requires_human_review": True,
            "requires_shadow_gate": True,
        },
        "notes": [
            "Can be considered for limited paper only after shadow gate plus human review.",
            "Paper trial is capped and does not grant live permission.",
        ],
    },
    "LIQUIDITY_SWEEP_REVERSAL": {
        "tier": "RESEARCH",
        "allowed_modes": ["shadow"],
        "paper_promotion": "RESEARCH_RETEST_REQUIRED",
        "live_promotion": "BLOCKED",
        "min_shadow_trades": SHADOW_GATE_DEFAULTS["min_closed_trades"],
        "notes": ["Keep shadow until positive post-cost evidence survives retest."],
    },
    "VWAP_REVERSION": {
        "tier": "RESEARCH",
        "allowed_modes": ["shadow"],
        "paper_promotion": "RESEARCH_RETEST_REQUIRED",
        "live_promotion": "BLOCKED",
        "min_shadow_trades": SHADOW_GATE_DEFAULTS["min_closed_trades"],
        "notes": ["Keep shadow until reversion edge is positive after costs."],
    },
    "VWAP_REVERSION_WATCH": {
        "tier": "RESEARCH",
        "allowed_modes": ["shadow"],
        "paper_promotion": "RESEARCH_RETEST_REQUIRED",
        "live_promotion": "BLOCKED",
        "min_shadow_trades": SHADOW_GATE_DEFAULTS["min_closed_trades"],
        "notes": ["Watch variant stays research-only until a clean retest passes."],
    },
    "MOMENTUM_CONTINUATION": {
        "tier": "RESEARCH",
        "allowed_modes": ["shadow"],
        "paper_promotion": "RESEARCH_RETEST_REQUIRED",
        "live_promotion": "BLOCKED",
        "min_shadow_trades": SHADOW_GATE_DEFAULTS["min_closed_trades"],
        "notes": ["Needs independent post-cost trend-following evidence."],
    },
    "RANGE_GRID": {
        "tier": "RESEARCH",
        "allowed_modes": ["shadow"],
        "paper_promotion": "RESEARCH_RETEST_REQUIRED",
        "live_promotion": "BLOCKED",
        "min_shadow_trades": SHADOW_GATE_DEFAULTS["min_closed_trades"],
        "notes": ["Grid stays research-only because small TP can be erased by costs."],
    },
    "TREND_FOLLOWING": {
        "tier": "RESEARCH",
        "allowed_modes": ["shadow"],
        "paper_promotion": "RESEARCH_RETEST_REQUIRED",
        "live_promotion": "BLOCKED",
        "min_shadow_trades": SHADOW_GATE_DEFAULTS["min_closed_trades"],
        "notes": ["Collect shadow evidence on the current universe first."],
    },
}


class ControlHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


_SERVICE_STATUS_CACHE: dict[str, tuple[float, str]] = {}


def get_price(symbol: str) -> float | None:
    try:
        with urllib.request.urlopen(BINANCE_URL + symbol, timeout=PRICE_REQUEST_TIMEOUT_SECONDS) as r:
            return float(json.loads(r.read())["price"])
    except Exception:
        return None


def get_prices(symbols: list[str]) -> dict[str, float | None]:
    unique_symbols = sorted({symbol for symbol in symbols if symbol})
    if not unique_symbols:
        return {}
    prices: dict[str, float | None] = {}
    workers = min(8, len(unique_symbols))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(get_price, symbol): symbol for symbol in unique_symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                prices[symbol] = future.result()
            except Exception:
                prices[symbol] = None
    return prices


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_shadow_trades_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            strategy TEXT NOT NULL,
            quantity TEXT NOT NULL,
            entry_price TEXT NOT NULL,
            stop_loss TEXT NOT NULL,
            take_profit TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'SHADOW_PAPER',
            status TEXT NOT NULL,
            risk_amount TEXT DEFAULT '0',
            r_multiple TEXT DEFAULT '0',
            realized_pnl TEXT DEFAULT '0',
            close_reason TEXT,
            metadata TEXT NOT NULL
        )
    """)


def ensure_dashboard_indexes(conn: sqlite3.Connection) -> None:
    """Create the index used by bounded dashboard diagnostics queries."""
    if not table_exists(conn, "ml_feature_snapshots"):
        return
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ml_feature_snapshots_decision_id
        ON ml_feature_snapshots(decision, id DESC)
    """)
    conn.commit()


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _to_float(value: Any, default: float | None = 0.0) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    parsed = _to_float(value, None)
    return default if parsed is None else int(parsed)


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _execution_cost_summary(metadata: Any) -> dict[str, Any]:
    data = _parse_metadata(metadata)
    summary = data.get("paper_execution_summary")
    is_pre_p5 = not bool(summary)
    if not isinstance(summary, dict):
        return {
            "realistic_execution": False,
            "pre_p5_ideal_fill": is_pre_p5,
            "gross_pnl": 0.0,
            "fees": 0.0,
            "slippage_cost": 0.0,
            "funding_cost": 0.0,
            "total_cost": 0.0,
            "net_pnl": 0.0,
        }
    fees = _to_float(summary.get("fees"), 0.0) or 0.0
    slippage = _to_float(summary.get("slippage_cost"), 0.0) or 0.0
    funding = _to_float(summary.get("funding_cost"), 0.0) or 0.0
    return {
        "realistic_execution": True,
        "pre_p5_ideal_fill": False,
        "gross_pnl": round(_to_float(summary.get("gross_pnl"), 0.0) or 0.0, 4),
        "fees": round(fees, 4),
        "slippage_cost": round(slippage, 4),
        "funding_cost": round(funding, 4),
        "total_cost": round(fees + slippage + funding, 4),
        "net_pnl": round(_to_float(summary.get("net_pnl"), 0.0) or 0.0, 4),
    }


def _is_realistic_execution_trade(row: Any) -> bool:
    return bool(_execution_cost_summary(_row_get(row, "metadata", {})).get("realistic_execution"))


def _with_execution_costs(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["execution_costs"] = _execution_cost_summary(item.get("metadata"))
    return item


def _signal_metadata_from_row(row: Any) -> dict[str, Any]:
    metadata = _parse_metadata(_row_get(row, "metadata", {}))
    signal_metadata = metadata.get("signal_metadata")
    if isinstance(signal_metadata, dict):
        return signal_metadata
    return metadata


def _truthy_metadata(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _increment(mapping: dict[str, int], key: str, amount: int = 1) -> None:
    mapping[key] = mapping.get(key, 0) + amount


def _strategy_from_trade(row: Any) -> str:
    signal_metadata = _signal_metadata_from_row(row)
    strategy = signal_metadata.get("strategy")
    return str(strategy or "UNKNOWN")


def _strategy_mode_from_row(row: Any, strategy_modes: dict[str, str] | None = None) -> str:
    strategy_modes = strategy_modes or {}
    signal_metadata = _signal_metadata_from_row(row)
    mode = signal_metadata.get("strategy_mode")
    if mode:
        return str(mode).lower()
    return str(strategy_modes.get(_strategy_from_trade(row), "unknown")).lower()


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fmt_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _max_drawdown(closed_trades: list[Any], initial_equity: float) -> tuple[float, float]:
    balance = initial_equity
    peak = balance
    max_dd_pct = 0.0
    max_dd_usdt = 0.0
    for trade in sorted(closed_trades, key=lambda t: _parse_datetime(_row_get(t, "closed_at") or _row_get(t, "created_at")) or datetime.min.replace(tzinfo=timezone.utc)):
        balance += _to_float(_row_get(trade, "realized_pnl"), 0.0) or 0.0
        if balance > peak:
            peak = balance
        drawdown = balance - peak
        drawdown_pct = drawdown / peak * 100 if peak > 0 else 0.0
        if drawdown_pct < max_dd_pct:
            max_dd_pct = drawdown_pct
            max_dd_usdt = drawdown
    return round(max_dd_pct, 2), round(max_dd_usdt, 4)


def _trade_cluster_id(row: Any) -> str | None:
    metadata = _parse_metadata(_row_get(row, "metadata", {}))
    cluster_id = metadata.get("trade_cluster_id")
    return str(cluster_id) if cluster_id else None


def _trade_cluster_context(row: Any) -> tuple[str, str, str]:
    return (
        _strategy_from_trade(row),
        str(_row_get(row, "symbol", "UNKNOWN") or "UNKNOWN"),
        str(_row_get(row, "direction", "UNKNOWN") or "UNKNOWN").upper(),
    )


def _closed_trade_clusters(closed_trades: list[Any], window_minutes: int = 60) -> list[list[Any]]:
    window_minutes = max(1, int(window_minutes or 60))
    metadata_clusters: dict[str, list[Any]] = {}
    fallback_clusters: list[list[Any]] = []
    latest_by_context: dict[tuple[str, str, str], list[Any]] = {}

    ordered = sorted(
        closed_trades,
        key=lambda t: _parse_datetime(_row_get(t, "created_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    for trade in ordered:
        explicit_id = _trade_cluster_id(trade)
        if explicit_id:
            metadata_clusters.setdefault(explicit_id, []).append(trade)
            continue

        context = _trade_cluster_context(trade)
        created_at = _parse_datetime(_row_get(trade, "created_at"))
        previous = latest_by_context.get(context)
        previous_dt = (
            _parse_datetime(_row_get(previous[-1], "created_at"))
            if previous
            else None
        )
        if previous and created_at and previous_dt:
            elapsed_min = (created_at - previous_dt).total_seconds() / 60
            if 0 <= elapsed_min <= window_minutes:
                previous.append(trade)
                continue

        cluster = [trade]
        fallback_clusters.append(cluster)
        latest_by_context[context] = cluster

    return [*metadata_clusters.values(), *fallback_clusters]


def _cluster_metrics(closed_trades: list[Any], window_minutes: int = 60) -> dict[str, Any]:
    clusters = _closed_trade_clusters(closed_trades, window_minutes)
    pnl_values = [
        sum(_to_float(_row_get(trade, "realized_pnl"), 0.0) or 0.0 for trade in cluster)
        for cluster in clusters
    ]
    r_values = [
        sum(_to_float(_row_get(trade, "r_multiple"), 0.0) or 0.0 for trade in cluster)
        for cluster in clusters
    ]
    count = len(clusters)
    wins = sum(1 for value in pnl_values if value > 0)
    losses = sum(1 for value in pnl_values if value < 0)
    gross_profit = sum(value for value in pnl_values if value > 0)
    gross_loss = abs(sum(value for value in pnl_values if value < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit
    return {
        "closed_clusters": count,
        "wins": wins,
        "losses": losses,
        "winrate": round(wins / count * 100, 1) if count else 0,
        "profit_factor": round(profit_factor, 2),
        "avg_r": round(sum(r_values) / count, 3) if count else 0,
        "largest_size": max((len(cluster) for cluster in clusters), default=0),
        "multi_trade_clusters": sum(1 for cluster in clusters if len(cluster) > 1),
    }


def _add_breakdown_item(target: dict[str, dict[str, Any]], key: str, pnl: float, r_value: float) -> None:
    item = target.setdefault(key, {
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "realized_pnl": 0.0,
        "r_sum": 0.0,
    })
    item["closed_trades"] += 1
    item["wins"] += 1 if pnl > 0 else 0
    item["losses"] += 1 if pnl < 0 else 0
    item["realized_pnl"] += pnl
    item["r_sum"] += r_value


def _finalize_breakdown(target: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key, item in sorted(target.items()):
        closed = item["closed_trades"]
        result[key] = {
            "closed_trades": closed,
            "wins": item["wins"],
            "losses": item["losses"],
            "winrate": round(item["wins"] / closed * 100, 1) if closed else 0,
            "realized_pnl": round(item["realized_pnl"], 4),
            "avg_r": round(item["r_sum"] / closed, 3) if closed else 0,
        }
    return result


def _exit_profile_breakdown(closed_trades: list[Any]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for trade in closed_trades:
        signal_metadata = _signal_metadata_from_row(trade)
        signature = str(signal_metadata.get("exit_profile_signature") or "unknown")
        item = buckets.setdefault(
            signature,
            {
                "exit_profile_signature": signature,
                "closed_trades": 0,
                "wins": 0,
                "losses": 0,
                "realized_pnl": 0.0,
                "r_sum": 0.0,
                "first_target_net_r_sum": 0.0,
                "first_target_net_r_count": 0,
            },
        )
        pnl = _to_float(_row_get(trade, "realized_pnl"), 0.0) or 0.0
        r_value = _to_float(_row_get(trade, "r_multiple"), 0.0) or 0.0
        first_target_net_r = _to_float(signal_metadata.get("first_target_net_reward_risk"), None)
        item["closed_trades"] += 1
        item["wins"] += 1 if pnl > 0 else 0
        item["losses"] += 1 if pnl < 0 else 0
        item["realized_pnl"] += pnl
        item["r_sum"] += r_value
        if first_target_net_r is not None:
            item["first_target_net_r_sum"] += first_target_net_r
            item["first_target_net_r_count"] += 1

    breakdown = []
    for item in buckets.values():
        closed = item["closed_trades"]
        first_count = item["first_target_net_r_count"]
        breakdown.append({
            "exit_profile_signature": item["exit_profile_signature"],
            "closed_trades": closed,
            "wins": item["wins"],
            "losses": item["losses"],
            "winrate": round(item["wins"] / closed * 100, 1) if closed else 0,
            "realized_pnl": round(item["realized_pnl"], 4),
            "avg_r": round(item["r_sum"] / closed, 3) if closed else 0,
            "avg_first_target_net_r": round(item["first_target_net_r_sum"] / first_count, 3) if first_count else None,
        })
    return sorted(breakdown, key=lambda row: (row["realized_pnl"], row["closed_trades"]), reverse=True)


def _strategy_logic_version_breakdown(closed_trades: list[Any]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for trade in closed_trades:
        signal_metadata = _signal_metadata_from_row(trade)
        version = str(signal_metadata.get("strategy_logic_version") or "legacy")
        item = buckets.setdefault(
            version,
            {
                "strategy_logic_version": version,
                "closed_trades": 0,
                "wins": 0,
                "losses": 0,
                "realized_pnl": 0.0,
                "r_sum": 0.0,
            },
        )
        pnl = _to_float(_row_get(trade, "realized_pnl"), 0.0) or 0.0
        r_value = _to_float(_row_get(trade, "r_multiple"), 0.0) or 0.0
        item["closed_trades"] += 1
        item["wins"] += 1 if pnl > 0 else 0
        item["losses"] += 1 if pnl < 0 else 0
        item["realized_pnl"] += pnl
        item["r_sum"] += r_value

    breakdown = []
    for item in buckets.values():
        closed = item["closed_trades"]
        breakdown.append({
            "strategy_logic_version": item["strategy_logic_version"],
            "closed_trades": closed,
            "wins": item["wins"],
            "losses": item["losses"],
            "winrate": round(item["wins"] / closed * 100, 1) if closed else 0,
            "realized_pnl": round(item["realized_pnl"], 4),
            "avg_r": round(item["r_sum"] / closed, 3) if closed else 0,
        })
    return sorted(breakdown, key=lambda row: (row["realized_pnl"], row["closed_trades"]), reverse=True)


def evaluate_strategy_gate(metrics: dict[str, Any], thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    limits = {**STRATEGY_GATE_DEFAULTS, **(thresholds or {})}
    checks: list[dict[str, Any]] = []

    def add_check(
        check_id: str,
        label: str,
        value: Any,
        threshold: Any,
        passed: bool,
        severity: str,
    ) -> None:
        checks.append({
            "id": check_id,
            "label": label,
            "value": value,
            "threshold": threshold,
            "passed": bool(passed),
            "severity": severity,
        })

    closed = int(_to_float(metrics.get("closed_trade_clusters", metrics.get("closed_trades")), 0) or 0)
    sample_age_days = _to_float(metrics.get("sample_age_days"), 0.0) or 0.0
    trades_per_day = _to_float(
        metrics.get("closed_trade_clusters_per_day", metrics.get("closed_trades_per_day")),
        0.0,
    ) or 0.0

    add_check(
        "min_closed_trades",
        "minimum closed trade clusters",
        closed,
        limits["min_closed_trades"],
        closed >= limits["min_closed_trades"],
        "maturity",
    )
    add_check(
        "min_sample_age_days",
        "minimum sample age",
        round(sample_age_days, 2),
        limits["min_sample_age_days"],
        sample_age_days >= limits["min_sample_age_days"],
        "maturity",
    )

    if closed > 0:
        winrate = _to_float(metrics.get("cluster_winrate", metrics.get("winrate")), 0.0) or 0.0
        profit_factor = _to_float(metrics.get("cluster_profit_factor", metrics.get("profit_factor")), 0.0) or 0.0
        avg_r = _to_float(metrics.get("cluster_avg_r", metrics.get("avg_r")), 0.0) or 0.0
        max_drawdown = _to_float(metrics.get("max_drawdown"), 0.0) or 0.0
        add_check(
            "min_closed_trades_per_day",
            "minimum trade frequency",
            round(trades_per_day, 3),
            limits["min_closed_trades_per_day"],
            trades_per_day >= limits["min_closed_trades_per_day"],
            "performance",
        )
        add_check(
            "min_winrate",
            "minimum winrate",
            round(winrate, 2),
            limits["min_winrate"],
            winrate >= limits["min_winrate"],
            "performance",
        )
        add_check(
            "min_profit_factor",
            "minimum profit factor",
            round(profit_factor, 3),
            limits["min_profit_factor"],
            profit_factor >= limits["min_profit_factor"],
            "performance",
        )
        add_check(
            "min_avg_r",
            "positive average R",
            round(avg_r, 3),
            limits["min_avg_r"],
            avg_r > limits["min_avg_r"],
            "performance",
        )
        add_check(
            "max_drawdown",
            "maximum drawdown",
            round(max_drawdown, 2),
            limits["max_drawdown"],
            max_drawdown >= limits["max_drawdown"],
            "risk",
        )
    else:
        checks.append({
            "id": "no_closed_trades",
            "label": "no closed trades yet",
            "value": 0,
            "threshold": 1,
            "passed": False,
            "severity": "maturity",
        })

    failed = [check for check in checks if not check["passed"]]
    hard_failed = [check for check in failed if check["severity"] in {"performance", "risk"}]
    if not failed:
        status = "PROMOTABLE"
    elif hard_failed:
        status = "BLOCKED"
    else:
        status = "WATCH"

    return {
        "status": status,
        "promotion_allowed": status == "PROMOTABLE",
        "failed_checks": [check["id"] for check in failed],
        "passed_checks": [check["id"] for check in checks if check["passed"]],
        "thresholds": limits,
        "checks": checks,
    }


def evaluate_shadow_gate(metrics: dict[str, Any], thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    limits = {**SHADOW_GATE_DEFAULTS, **(thresholds or {})}
    checks: list[dict[str, Any]] = []

    def add_check(check_id: str, value: Any, threshold: Any, passed: bool, severity: str) -> None:
        checks.append({
            "id": check_id,
            "value": value,
            "threshold": threshold,
            "passed": bool(passed),
            "severity": severity,
        })

    closed = int(_to_float(metrics.get("closed_trades"), 0) or 0)
    age_days = _to_float(metrics.get("sample_age_days"), 0.0) or 0.0
    winrate = _to_float(metrics.get("winrate"), 0.0) or 0.0
    profit_factor = _to_float(metrics.get("profit_factor"), 0.0) or 0.0
    avg_r = _to_float(metrics.get("avg_r"), 0.0) or 0.0
    max_drawdown = _to_float(metrics.get("max_drawdown"), 0.0) or 0.0

    add_check("min_closed_trades", closed, limits["min_closed_trades"], closed >= limits["min_closed_trades"], "maturity")
    add_check("min_sample_age_days", round(age_days, 2), limits["min_sample_age_days"], age_days >= limits["min_sample_age_days"], "maturity")
    if closed > 0:
        add_check("min_winrate", round(winrate, 2), limits["min_winrate"], winrate >= limits["min_winrate"], "performance")
        add_check("min_profit_factor", round(profit_factor, 3), limits["min_profit_factor"], profit_factor >= limits["min_profit_factor"], "performance")
        add_check("min_avg_r", round(avg_r, 3), limits["min_avg_r"], avg_r > limits["min_avg_r"], "performance")
        add_check("max_drawdown", round(max_drawdown, 2), limits["max_drawdown"], max_drawdown >= limits["max_drawdown"], "risk")

    failed = [check for check in checks if not check["passed"]]
    maturity_failed = [check for check in failed if check["severity"] == "maturity"]
    hard_failed = [check for check in failed if check["severity"] in {"performance", "risk"}]
    if not failed:
        status = "PROMOTE"
        recommendation = "PROMOTE_TO_PAPER"
    elif maturity_failed:
        status = "TESTING"
        recommendation = "KEEP_SHADOW"
    elif hard_failed:
        status = "WATCH"
        recommendation = "KEEP_SHADOW"
    else:
        status = "WATCH"
        recommendation = "KEEP_SHADOW"

    return {
        "status": status,
        "recommendation": recommendation,
        "promotion_candidate": status == "PROMOTE",
        "failed_checks": [check["id"] for check in failed],
        "passed_checks": [check["id"] for check in checks if check["passed"]],
        "thresholds": limits,
        "checks": checks,
    }


def strategy_promotion_policy(strategy: str) -> dict[str, Any]:
    strategy_key = str(strategy or "UNKNOWN").upper()
    policy = STRATEGY_PROMOTION_POLICIES.get(strategy_key)
    if policy is None and strategy_key in SQZ_GATE_COHORT_SHADOW_STRATEGIES:
        policy = {
            "tier": "MEASUREMENT_SHADOW",
            "allowed_modes": ["shadow"],
            "paper_promotion": "HUMAN_REVIEW_AFTER_100_CLOSED_COHORT_TRADES",
            "live_promotion": "BLOCKED_MEASUREMENT_COHORT",
            "min_shadow_trades": 100,
            "review_milestone_trades": 50,
            "notes": [
                "Counterfactual SQZ cohort: only one gate is relaxed for each virtual entry.",
                "Compare with SQZ_STRICT_CONTROL_SHADOW in the same virtual execution model.",
                "No automatic paper or live promotion is allowed.",
            ],
        }
    if policy is None and (
        strategy_key.endswith(SHADOW_GATE_COUNTERFACTUAL_SUFFIXES)
        or _is_parallel_shadow_lab_strategy(strategy_key)
    ):
        policy = {
            "tier": "MEASUREMENT_SHADOW",
            "allowed_modes": ["shadow"],
            "paper_promotion": "HUMAN_REVIEW_AFTER_50_CLOSED_COHORT_TRADES",
            "live_promotion": "BLOCKED_MEASUREMENT_COHORT",
            "min_shadow_trades": 50,
            "review_milestone_trades": 20,
            "notes": [
                "Counterfactual cohort: one fixed context-gate policy is replayed virtually.",
                "Compare each arm with the strict lab control over the same source clusters.",
                "No automatic paper or live promotion is allowed.",
            ],
        }
    if policy is None:
        policy = {
            "tier": "UNKNOWN",
            "allowed_modes": ["shadow"],
            "paper_promotion": "MANUAL_POLICY_REQUIRED",
            "live_promotion": "BLOCKED",
            "min_shadow_trades": SHADOW_GATE_DEFAULTS["min_closed_trades"],
            "notes": ["No explicit promotion policy is defined for this strategy."],
        }
    return {"strategy": strategy_key, **policy}


def apply_strategy_promotion_policy(row: dict[str, Any]) -> dict[str, Any]:
    strategy = str(row.get("strategy") or "UNKNOWN").upper()
    mode = str(row.get("strategy_mode") or "unknown").lower()
    policy = strategy_promotion_policy(strategy)
    gate = row.get("gate") or {}
    shadow_gate = row.get("shadow_gate") or {}
    shadow_paper = row.get("shadow_paper") or {}
    post_p5 = row.get("post_p5_evidence") or {}
    tier = policy.get("tier")
    reasons: list[str] = []

    post_p5_clusters = int(_to_float(post_p5.get("closed_trade_clusters"), 0) or 0)
    shadow_closed = int(_to_float(shadow_paper.get("closed_trades"), 0) or 0)
    gate_promotable = gate.get("status") == "PROMOTABLE"
    shadow_promotable = bool(shadow_gate.get("promotion_candidate"))
    paper_review_allowed = False
    live_review_allowed = False
    action = "KEEP_COLLECTING_EVIDENCE"

    if mode not in set(policy.get("allowed_modes", [])):
        reasons.append(f"mode {mode or 'unknown'} is outside policy allowed modes")

    if tier == "CHAMPION":
        if gate_promotable:
            action = "REVIEW_FOR_CORE_OR_LIVE"
            paper_review_allowed = True
            live_review_allowed = True
            reasons.append("champion post-P5 gate passed")
        else:
            action = "KEEP_CHAMPION_UNDER_REVIEW"
            reasons.append("champion still needs post-P5 realistic evidence before promotion")
    elif tier == "CHALLENGER":
        if shadow_promotable:
            action = "REVIEW_CHALLENGER_FOR_LIMITED_PAPER"
            paper_review_allowed = True
            reasons.append("challenger shadow gate passed; human review required")
        else:
            action = "KEEP_CHALLENGER_IN_SHADOW"
            reasons.append("challenger is not ready to challenge the SQZ champion")
    elif tier == "PAPER_ONLY":
        required = int(policy.get("min_post_p5_clusters", 200))
        milestone = int(policy.get("review_milestone_clusters", required))
        if post_p5_clusters < milestone:
            action = "COLLECT_PAPER_EVIDENCE"
            reasons.append(f"paper-only strategy needs {milestone} post-P5 clusters before allocation review")
        elif post_p5_clusters < required:
            action = "PAPER_ALLOCATION_REVIEW_REQUIRED"
            paper_review_allowed = True
            reasons.append(
                f"paper-only strategy reached {milestone} cluster review milestone; "
                f"{required} clusters and walk-forward are still required before live review"
            )
        elif gate_promotable:
            action = "WALK_FORWARD_REVIEW_REQUIRED"
            paper_review_allowed = True
            reasons.append("paper gate passed, but walk-forward proof is still required")
        else:
            action = "KEEP_LIMITED_PAPER_OR_DEMOTE_REVIEW"
            paper_review_allowed = True
            reasons.append("paper-only strategy gate has not passed")
    elif tier == "MEASUREMENT":
        required = int(policy.get("min_post_p5_clusters", 150))
        if post_p5_clusters < required:
            action = "COLLECT_MEASUREMENT_EVIDENCE"
            reasons.append(
                f"isolated paper cohort needs {required} closed clusters before comparison with strict SQZ"
            )
        else:
            action = "MEASUREMENT_REVIEW_REQUIRED"
            reasons.append(
                "measurement cohort reached its review size; compare post-cost metrics with strict SQZ before any change"
            )
    elif tier == "MEASUREMENT_SHADOW":
        milestone = int(policy.get("review_milestone_trades", 50))
        required = int(policy.get("min_shadow_trades", 100))
        if shadow_closed < milestone:
            action = "COLLECT_SHADOW_COHORT_EVIDENCE"
            reasons.append(
                f"counterfactual shadow cohort needs {milestone} closed trades before its interim review"
            )
        elif shadow_closed < required:
            action = "SHADOW_COHORT_INTERIM_REVIEW"
            reasons.append(
                f"cohort reached {milestone} trades; continue to {required} before any paper discussion"
            )
        else:
            action = "SHADOW_COHORT_REVIEW_REQUIRED"
            paper_review_allowed = True
            reasons.append(
                "cohort reached its review size; compare only against the strict virtual SQZ control"
            )
    elif tier == "SHADOW_REVIEW":
        if shadow_promotable:
            action = "HUMAN_REVIEW_FOR_PAPER"
            paper_review_allowed = True
            trial = policy.get("paper_trial") or {}
            if trial:
                reasons.append(
                    f"shadow gate passed; review limited paper trial "
                    f"({trial.get('duration_days')}d, max {trial.get('max_allocation_pct')}% allocation)"
                )
            else:
                reasons.append("shadow gate passed; paper promotion still requires human review")
        elif shadow_closed <= 0:
            action = "COLLECT_SHADOW_EVIDENCE"
            reasons.append("no closed shadow-paper trades yet")
        else:
            action = "KEEP_SHADOW"
            reasons.append("shadow evidence exists but promotion gate has not passed")
    elif tier == "RESEARCH":
        if shadow_promotable:
            action = "RESEARCH_RETEST_BEFORE_PAPER"
            reasons.append("positive shadow gate found, but policy requires retest before paper")
        elif shadow_closed <= 0:
            action = "COLLECT_RESEARCH_EVIDENCE"
            reasons.append("research strategy has no closed shadow-paper trades yet")
        else:
            action = "KEEP_RESEARCH"
            reasons.append("research strategy remains shadow-only until post-cost retest is positive")
    else:
        action = "MANUAL_POLICY_REQUIRED"
        reasons.append("strategy has no explicit promotion/demotion policy")

    if policy.get("live_promotion", "").startswith("BLOCKED"):
        live_review_allowed = False

    return {
        **policy,
        "current_mode": mode,
        "action": action,
        "paper_review_allowed": bool(paper_review_allowed),
        "live_review_allowed": bool(live_review_allowed),
        "human_review_required": True,
        "post_p5_clusters": post_p5_clusters,
        "shadow_closed_trades": shadow_closed,
        "reasons": reasons,
    }


def build_strategy_scorecard(
    trades: list[Any],
    rejections: list[Any],
    signals: list[Any] | None = None,
    shadow_trades: list[Any] | None = None,
    diagnostics: list[Any] | None = None,
    initial_equity: float = 1000.0,
    prices: dict[str, Any] | None = None,
    gate_thresholds: dict[str, Any] | None = None,
    strategy_modes: dict[str, str] | None = None,
) -> dict[str, Any]:
    prices = prices or {}
    signals = signals or []
    shadow_trades = shadow_trades or []
    diagnostics = diagnostics or []
    strategy_modes = {str(k).upper(): str(v).lower() for k, v in (strategy_modes or {}).items()}
    # Revalidation buckets are created by the bot only for virtual research.
    # They are not part of config.strategy_modes, but the dashboard must never
    # render them as a disabled/executable strategy.
    for trade in shadow_trades:
        strategy = str(_row_get(trade, "strategy", "") or "").upper()
        if (
            strategy.endswith("_REVALIDATION")
            or strategy.endswith(SHADOW_GATE_COUNTERFACTUAL_SUFFIXES)
            or _is_parallel_shadow_lab_strategy(strategy)
        ):
            strategy_modes.setdefault(strategy, "shadow")
    buckets: dict[str, dict[str, Any]] = {}

    def bucket(strategy: str) -> dict[str, Any]:
        return buckets.setdefault(strategy, {
            "closed": [],
            "post_p5_closed": [],
            "open": [],
            "shadow_closed": [],
            "shadow_open": [],
            "signals_total": 0,
            "shadow_signals": 0,
            "last_signal_at": None,
            "signal_confidences": [],
            "signal_confluences": [],
            "evidence_counts": {},
            "diagnostics_total": 0,
            "diagnostics_by_reason": {},
            "diagnostic_symbols": {},
            "last_diagnostic_at": None,
            "order_flow_total": 0,
            "order_flow_score_sum": 0.0,
            "order_flow_by_alignment": {},
            "order_flow_risk_flags": {},
            "order_flow_symbols": {},
            "last_order_flow_at": None,
            "relative_strength_total": 0,
            "relative_strength_score_sum": 0.0,
            "relative_strength_by_alignment": {},
            "relative_strength_symbols": {},
            "last_relative_strength_at": None,
            "shadow_funnel": {
                "signals": 0,
                "context_rejections": 0,
                "cooldown_rejections": 0,
                "risk_rejections": 0,
                "opened": 0,
                "rejected_by_reason": {},
                "last_signal_at": None,
                "last_context_rejection_at": None,
                "last_opened_at": None,
            },
            "rejections_total": 0,
            "rejections_by_type": {},
        })

    for strategy in strategy_modes:
        bucket(strategy)

    for trade in trades:
        strategy = _strategy_from_trade(trade)
        status = str(_row_get(trade, "status", "") or "").upper()
        if status == "CLOSED":
            bucket(strategy)["closed"].append(trade)
            if _is_realistic_execution_trade(trade):
                bucket(strategy)["post_p5_closed"].append(trade)
        elif status in OPEN_TRADE_STATUSES:
            bucket(strategy)["open"].append(trade)

    for trade in shadow_trades:
        strategy = str(_row_get(trade, "strategy", "") or _strategy_from_trade(trade))
        status = str(_row_get(trade, "status", "") or "").upper()
        if status == "CLOSED":
            bucket(strategy)["shadow_closed"].append(trade)
        elif status in OPEN_TRADE_STATUSES:
            bucket(strategy)["shadow_open"].append(trade)

    for rejection in rejections:
        strategy = str(_row_get(rejection, "strategy", "UNKNOWN") or "UNKNOWN")
        item = bucket(strategy)
        filter_type = str(_row_get(rejection, "filter_type", "OTHER") or "OTHER")
        item["rejections_total"] += 1
        item["rejections_by_type"][filter_type] = item["rejections_by_type"].get(filter_type, 0) + 1

    for signal in signals:
        # Parse the signal envelope once.  The scorecard can contain tens of
        # thousands of signals, so repeatedly decoding identical JSON dominates
        # a cold dashboard refresh.
        metadata = _parse_metadata(_row_get(signal, "metadata", {}))
        signal_metadata = metadata.get("signal_metadata")
        if not isinstance(signal_metadata, dict):
            signal_metadata = metadata
        strategy = str(signal_metadata.get("strategy") or "UNKNOWN")
        item = bucket(strategy)
        item["signals_total"] += 1
        mode = str(signal_metadata.get("strategy_mode") or strategy_modes.get(strategy, "unknown")).lower()
        if mode == "shadow":
            item["shadow_signals"] += 1
        confidence = _to_float(_row_get(signal, "confidence"), None)
        if confidence is not None:
            item["signal_confidences"].append(confidence)
        confluence = _to_float(metadata.get("mr_confluence"), None)
        if confluence is None:
            confluence = _to_float(metadata.get("trend_pullback_confluence"), None)
        if confluence is not None:
            item["signal_confluences"].append(confluence)
        evidence_counts = item["evidence_counts"]
        for metadata_key, evidence_key in (
            ("divergence", "divergence"),
            ("volume_ok", "volume_confirmed"),
            ("edge_confirms", "edge_confirmed"),
            ("reversal_candle", "reversal_candle"),
        ):
            if _truthy_metadata(metadata.get(metadata_key)):
                _increment(evidence_counts, evidence_key)
        flags = metadata.get("mr_confirmation_flags")
        if not isinstance(flags, list):
            flags = metadata.get("trend_pullback_flags")
        if isinstance(flags, list):
            for flag in flags:
                _increment(evidence_counts, f"flag:{flag}")
        signal_dt = _parse_datetime(_row_get(signal, "created_at"))
        if signal_dt and (item["last_signal_at"] is None or signal_dt > item["last_signal_at"]):
            item["last_signal_at"] = signal_dt

    for diagnostic in diagnostics:
        decision = str(_row_get(diagnostic, "decision", "") or "")
        metadata = _parse_metadata(_row_get(diagnostic, "metadata", {}))
        strategy = str(_row_get(diagnostic, "strategy", "") or metadata.get("strategy") or "UNKNOWN")
        item = bucket(strategy)
        if decision == "ORDER_FLOW_ANNOTATION":
            payload = _order_flow_payload(diagnostic)
            alignment = str(payload.get("alignment") or "unknown")
            score = _to_float(payload.get("score"), 0.0) or 0.0
            symbol = str(_row_get(diagnostic, "symbol", "") or metadata.get("symbol") or "UNKNOWN")
            item["order_flow_total"] += 1
            item["order_flow_score_sum"] += score
            _increment(item["order_flow_by_alignment"], alignment)
            _increment(item["order_flow_symbols"], symbol)
            for flag in payload.get("risk_flags") or []:
                _increment(item["order_flow_risk_flags"], str(flag))
            order_flow_dt = _parse_datetime(_row_get(diagnostic, "created_at"))
            if order_flow_dt and (item["last_order_flow_at"] is None or order_flow_dt > item["last_order_flow_at"]):
                item["last_order_flow_at"] = order_flow_dt
            continue
        if decision == "RELATIVE_STRENGTH_ANNOTATION":
            payload = _relative_strength_payload(diagnostic)
            alignment = str(payload.get("alignment") or "unknown")
            score = _to_float(payload.get("score"), 0.0) or 0.0
            symbol = str(_row_get(diagnostic, "symbol", "") or metadata.get("symbol") or "UNKNOWN")
            item["relative_strength_total"] += 1
            item["relative_strength_score_sum"] += score
            _increment(item["relative_strength_by_alignment"], alignment)
            _increment(item["relative_strength_symbols"], symbol)
            relative_strength_dt = _parse_datetime(_row_get(diagnostic, "created_at"))
            if relative_strength_dt and (
                item["last_relative_strength_at"] is None
                or relative_strength_dt > item["last_relative_strength_at"]
            ):
                item["last_relative_strength_at"] = relative_strength_dt
            continue
        if decision.startswith("SHADOW_"):
            reason = str(_row_get(diagnostic, "reason", "") or "unknown")
            shadow_funnel = item["shadow_funnel"]
            event_dt = _parse_datetime(_row_get(diagnostic, "created_at"))
            if decision == "SHADOW_SIGNAL":
                shadow_funnel["signals"] += 1
                if event_dt and (
                    shadow_funnel["last_signal_at"] is None or event_dt > shadow_funnel["last_signal_at"]
                ):
                    shadow_funnel["last_signal_at"] = event_dt
            elif decision == "SHADOW_PAPER_REJECTED_CONTEXT":
                shadow_funnel["context_rejections"] += 1
                _increment(shadow_funnel["rejected_by_reason"], reason)
                if event_dt and (
                    shadow_funnel["last_context_rejection_at"] is None
                    or event_dt > shadow_funnel["last_context_rejection_at"]
                ):
                    shadow_funnel["last_context_rejection_at"] = event_dt
            elif decision == "SHADOW_PAPER_REJECTED_COOLDOWN":
                shadow_funnel["cooldown_rejections"] += 1
                _increment(shadow_funnel["rejected_by_reason"], reason)
            elif decision == "SHADOW_PAPER_REJECTED_RISK":
                shadow_funnel["risk_rejections"] += 1
                _increment(shadow_funnel["rejected_by_reason"], reason)
            elif decision == "SHADOW_PAPER_OPENED":
                shadow_funnel["opened"] += 1
                if event_dt and (
                    shadow_funnel["last_opened_at"] is None or event_dt > shadow_funnel["last_opened_at"]
                ):
                    shadow_funnel["last_opened_at"] = event_dt
            continue
        if decision != "STRATEGY_DIAGNOSTIC":
            continue
        if metadata and not _truthy_metadata(metadata.get("diagnostic")):
            continue
        reason = str(_row_get(diagnostic, "reason", "") or metadata.get("block_reason") or "unknown")
        symbol = str(_row_get(diagnostic, "symbol", "") or metadata.get("symbol") or "UNKNOWN")
        item["diagnostics_total"] += 1
        _increment(item["diagnostics_by_reason"], reason)
        _increment(item["diagnostic_symbols"], symbol)
        diagnostic_dt = _parse_datetime(_row_get(diagnostic, "created_at"))
        if diagnostic_dt and (item["last_diagnostic_at"] is None or diagnostic_dt > item["last_diagnostic_at"]):
            item["last_diagnostic_at"] = diagnostic_dt

    strategies = []
    summary = {
        "strategies": 0,
        "closed_trades": 0,
        "closed_trade_clusters": 0,
        "post_p5_closed_trades": 0,
        "post_p5_closed_trade_clusters": 0,
        "pre_p5_closed_trades": 0,
        "open_trades": 0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "post_p5_realized_pnl": 0.0,
        "shadow_closed_trades": 0,
        "shadow_open_trades": 0,
        "shadow_realized_pnl": 0.0,
        "shadow_unrealized_pnl": 0.0,
        "rejections_total": 0,
    }

    for strategy, item in buckets.items():
        closed = item["closed"]
        post_p5_closed = item["post_p5_closed"]
        open_trades = item["open"]
        shadow_closed = item["shadow_closed"]
        shadow_open = item["shadow_open"]
        pnl_values = [_to_float(_row_get(t, "realized_pnl"), 0.0) or 0.0 for t in closed]
        r_values = [_to_float(_row_get(t, "r_multiple"), 0.0) or 0.0 for t in closed]
        post_p5_pnl_values = [_to_float(_row_get(t, "realized_pnl"), 0.0) or 0.0 for t in post_p5_closed]
        post_p5_r_values = [_to_float(_row_get(t, "r_multiple"), 0.0) or 0.0 for t in post_p5_closed]
        shadow_pnl_values = [_to_float(_row_get(t, "realized_pnl"), 0.0) or 0.0 for t in shadow_closed]
        shadow_r_values = [_to_float(_row_get(t, "r_multiple"), 0.0) or 0.0 for t in shadow_closed]
        gross_profit = sum(v for v in pnl_values if v > 0)
        gross_loss = abs(sum(v for v in pnl_values if v < 0))
        closed_count = len(closed)
        post_p5_closed_count = len(post_p5_closed)
        pre_p5_closed_count = max(closed_count - post_p5_closed_count, 0)
        wins = sum(1 for v in pnl_values if v > 0)
        losses = sum(1 for v in pnl_values if v < 0)
        realized_pnl = sum(pnl_values)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit
        post_p5_wins = sum(1 for v in post_p5_pnl_values if v > 0)
        post_p5_losses = sum(1 for v in post_p5_pnl_values if v < 0)
        post_p5_realized_pnl = sum(post_p5_pnl_values)
        post_p5_gross_profit = sum(v for v in post_p5_pnl_values if v > 0)
        post_p5_gross_loss = abs(sum(v for v in post_p5_pnl_values if v < 0))
        post_p5_profit_factor = (
            post_p5_gross_profit / post_p5_gross_loss if post_p5_gross_loss > 0 else post_p5_gross_profit
        )
        shadow_closed_count = len(shadow_closed)
        shadow_wins = sum(1 for v in shadow_pnl_values if v > 0)
        shadow_losses = sum(1 for v in shadow_pnl_values if v < 0)
        shadow_realized_pnl = sum(shadow_pnl_values)
        shadow_gross_profit = sum(v for v in shadow_pnl_values if v > 0)
        shadow_gross_loss = abs(sum(v for v in shadow_pnl_values if v < 0))
        shadow_profit_factor = (
            shadow_gross_profit / shadow_gross_loss if shadow_gross_loss > 0 else shadow_gross_profit
        )
        max_dd_pct, max_dd_usdt = _max_drawdown(closed, initial_equity)
        post_p5_max_dd_pct, post_p5_max_dd_usdt = _max_drawdown(post_p5_closed, initial_equity)
        shadow_max_dd_pct, shadow_max_dd_usdt = _max_drawdown(shadow_closed, initial_equity)
        clusters = _cluster_metrics(closed, SCORECARD_CLUSTER_WINDOW_MINUTES)
        post_p5_clusters = _cluster_metrics(post_p5_closed, SCORECARD_CLUSTER_WINDOW_MINUTES)

        dates = [
            dt for dt in (
                _parse_datetime(_row_get(t, "closed_at") or _row_get(t, "created_at"))
                for t in [*closed, *open_trades]
            )
            if dt is not None
        ]
        first_dt = min(dates) if dates else None
        last_dt = max(dates) if dates else None
        span_days = 0.0
        if first_dt and last_dt:
            span_days = max(1.0, (last_dt - first_dt).total_seconds() / 86400)
        sample_age_days = 0.0
        if first_dt and last_dt:
            sample_age_days = max(0.0, (last_dt - first_dt).total_seconds() / 86400)
        post_p5_dates = [
            dt for dt in (
                _parse_datetime(_row_get(t, "closed_at") or _row_get(t, "created_at"))
                for t in post_p5_closed
            )
            if dt is not None
        ]
        post_p5_first_dt = min(post_p5_dates) if post_p5_dates else None
        post_p5_last_dt = max(post_p5_dates) if post_p5_dates else None
        post_p5_span_days = 0.0
        if post_p5_first_dt and post_p5_last_dt:
            post_p5_span_days = max(1.0, (post_p5_last_dt - post_p5_first_dt).total_seconds() / 86400)
        post_p5_sample_age_days = 0.0
        if post_p5_first_dt and post_p5_last_dt:
            post_p5_sample_age_days = max(
                0.0,
                (post_p5_last_dt - post_p5_first_dt).total_seconds() / 86400,
            )
        shadow_dates = [
            dt for dt in (
                _parse_datetime(_row_get(t, "closed_at") or _row_get(t, "created_at"))
                for t in [*shadow_closed, *shadow_open]
            )
            if dt is not None
        ]
        shadow_first_dt = min(shadow_dates) if shadow_dates else None
        shadow_last_dt = max(shadow_dates) if shadow_dates else None
        shadow_sample_age_days = 0.0
        if shadow_first_dt and shadow_last_dt:
            shadow_sample_age_days = max(0.0, (shadow_last_dt - shadow_first_dt).total_seconds() / 86400)

        by_symbol: dict[str, dict[str, Any]] = {}
        by_direction: dict[str, dict[str, Any]] = {}
        for trade, pnl, r_value in zip(closed, pnl_values, r_values):
            _add_breakdown_item(by_symbol, str(_row_get(trade, "symbol", "UNKNOWN") or "UNKNOWN"), pnl, r_value)
            _add_breakdown_item(by_direction, str(_row_get(trade, "direction", "UNKNOWN") or "UNKNOWN"), pnl, r_value)

        open_positions = []
        open_risk = 0.0
        unrealized_pnl = 0.0
        for trade in open_trades:
            symbol = str(_row_get(trade, "symbol", "") or "")
            direction = str(_row_get(trade, "direction", "") or "").upper()
            entry = _to_float(_row_get(trade, "entry_price"), 0.0) or 0.0
            qty = _to_float(_row_get(trade, "quantity"), 0.0) or 0.0
            risk = _to_float(_row_get(trade, "risk_amount"), 0.0) or 0.0
            price = _to_float(prices.get(symbol), None)
            pnl = None
            pnl_r = None
            if price is not None and entry and qty:
                pnl = (price - entry) * qty if direction == "LONG" else (entry - price) * qty
                unrealized_pnl += pnl
                pnl_r = pnl / risk if risk > 0 else None
            open_risk += risk
            open_positions.append({
                "id": _row_get(trade, "id"),
                "symbol": symbol,
                "direction": direction,
                "quantity": _row_get(trade, "quantity"),
                "entry_price": _row_get(trade, "entry_price"),
                "current_price": round(price, 8) if price is not None else None,
                "risk_amount": round(risk, 4),
                "unrealized_pnl": round(pnl, 4) if pnl is not None else None,
                "pnl_r": round(pnl_r, 3) if pnl_r is not None else None,
                "created_at": _row_get(trade, "created_at"),
            })

        shadow_open_positions = []
        shadow_open_risk = 0.0
        shadow_unrealized_pnl = 0.0
        for trade in shadow_open:
            symbol = str(_row_get(trade, "symbol", "") or "")
            direction = str(_row_get(trade, "direction", "") or "").upper()
            entry = _to_float(_row_get(trade, "entry_price"), 0.0) or 0.0
            qty = _to_float(_row_get(trade, "quantity"), 0.0) or 0.0
            risk = _to_float(_row_get(trade, "risk_amount"), 0.0) or 0.0
            price = _to_float(prices.get(symbol), None)
            pnl = None
            pnl_r = None
            if price is not None and entry and qty:
                pnl = (price - entry) * qty if direction == "LONG" else (entry - price) * qty
                shadow_unrealized_pnl += pnl
                pnl_r = pnl / risk if risk > 0 else None
            shadow_open_risk += risk
            shadow_open_positions.append({
                "id": _row_get(trade, "id"),
                "symbol": symbol,
                "direction": direction,
                "quantity": _row_get(trade, "quantity"),
                "entry_price": _row_get(trade, "entry_price"),
                "current_price": round(price, 8) if price is not None else None,
                "risk_amount": round(risk, 4),
                "unrealized_pnl": round(pnl, 4) if pnl is not None else None,
                "pnl_r": round(pnl_r, 3) if pnl_r is not None else None,
                "created_at": _row_get(trade, "created_at"),
            })

        shadow_metrics = {
            "closed_trades": shadow_closed_count,
            "open_trades": len(shadow_open),
            "wins": shadow_wins,
            "losses": shadow_losses,
            "winrate": round(shadow_wins / shadow_closed_count * 100, 1) if shadow_closed_count else 0,
            "profit_factor": round(shadow_profit_factor, 2),
            "realized_pnl": round(shadow_realized_pnl, 4),
            "unrealized_pnl": round(shadow_unrealized_pnl, 4),
            "total_pnl": round(shadow_realized_pnl + shadow_unrealized_pnl, 4),
            "avg_r": round(sum(shadow_r_values) / shadow_closed_count, 3) if shadow_closed_count else 0,
            "max_drawdown": shadow_max_dd_pct,
            "max_drawdown_usdt": shadow_max_dd_usdt,
            "sample_age_days": round(shadow_sample_age_days, 2),
            "first_trade_at": _fmt_dt(shadow_first_dt),
            "last_trade_at": _fmt_dt(shadow_last_dt),
            "open_risk": round(shadow_open_risk, 4),
            "open_positions": shadow_open_positions,
            "strategy_logic_version_breakdown": _strategy_logic_version_breakdown(shadow_closed),
        }
        shadow_gate = evaluate_shadow_gate(shadow_metrics)
        shadow_funnel = item["shadow_funnel"]

        row = {
            "strategy": strategy,
            "strategy_mode": strategy_modes.get(strategy, "unknown"),
            "closed_trades": closed_count,
            "closed_trade_clusters": clusters["closed_clusters"],
            "open_trades": len(open_trades),
            "signals_total": item["signals_total"],
            "shadow_signals": item["shadow_signals"],
            "candidate_evidence": {
                "avg_signal_confidence": round(
                    sum(item["signal_confidences"]) / len(item["signal_confidences"]),
                    3,
                )
                if item["signal_confidences"]
                else 0,
                "avg_confluence": round(
                    sum(item["signal_confluences"]) / len(item["signal_confluences"]),
                    2,
                )
                if item["signal_confluences"]
                else 0,
                "counts": dict(sorted(item["evidence_counts"].items())),
                "diagnostics": {
                    "total": item["diagnostics_total"],
                    "by_reason": dict(
                        sorted(item["diagnostics_by_reason"].items(), key=lambda kv: kv[1], reverse=True)
                    ),
                    "top_symbols": dict(
                        sorted(item["diagnostic_symbols"].items(), key=lambda kv: kv[1], reverse=True)[:5]
                    ),
                    "last_at": _fmt_dt(item["last_diagnostic_at"]),
                },
                "order_flow": {
                    "total": item["order_flow_total"],
                    "avg_score": round(item["order_flow_score_sum"] / item["order_flow_total"], 3)
                    if item["order_flow_total"]
                    else 0,
                    "by_alignment": dict(
                        sorted(item["order_flow_by_alignment"].items(), key=lambda kv: kv[1], reverse=True)
                    ),
                    "risk_flags": dict(
                        sorted(item["order_flow_risk_flags"].items(), key=lambda kv: kv[1], reverse=True)
                    ),
                    "top_symbols": dict(
                        sorted(item["order_flow_symbols"].items(), key=lambda kv: kv[1], reverse=True)[:5]
                    ),
                    "last_at": _fmt_dt(item["last_order_flow_at"]),
                },
                "relative_strength": {
                    "total": item["relative_strength_total"],
                    "avg_score": round(item["relative_strength_score_sum"] / item["relative_strength_total"], 3)
                    if item["relative_strength_total"]
                    else 0,
                    "by_alignment": dict(
                        sorted(item["relative_strength_by_alignment"].items(), key=lambda kv: kv[1], reverse=True)
                    ),
                    "top_symbols": dict(
                        sorted(item["relative_strength_symbols"].items(), key=lambda kv: kv[1], reverse=True)[:5]
                    ),
                    "last_at": _fmt_dt(item["last_relative_strength_at"]),
                },
                "shadow_funnel": {
                    "signals": shadow_funnel["signals"],
                    "context_rejections": shadow_funnel["context_rejections"],
                    "cooldown_rejections": shadow_funnel["cooldown_rejections"],
                    "risk_rejections": shadow_funnel["risk_rejections"],
                    "opened": shadow_funnel["opened"],
                    "open_rate": round(shadow_funnel["opened"] / shadow_funnel["signals"] * 100, 1)
                    if shadow_funnel["signals"]
                    else 0,
                    "rejected_by_reason": dict(
                        sorted(
                            shadow_funnel["rejected_by_reason"].items(),
                            key=lambda kv: kv[1],
                            reverse=True,
                        )
                    ),
                    "last_signal_at": _fmt_dt(shadow_funnel["last_signal_at"]),
                    "last_context_rejection_at": _fmt_dt(shadow_funnel["last_context_rejection_at"]),
                    "last_opened_at": _fmt_dt(shadow_funnel["last_opened_at"]),
                },
            },
            "shadow_paper": shadow_metrics,
            "shadow_gate": shadow_gate,
            "post_p5_evidence": {
                "scope": "post_p5_realistic_execution",
                "closed_trades": post_p5_closed_count,
                "closed_trade_clusters": post_p5_clusters["closed_clusters"],
                "pre_p5_closed_trades": pre_p5_closed_count,
                "wins": post_p5_wins,
                "losses": post_p5_losses,
                "winrate": round(post_p5_wins / post_p5_closed_count * 100, 1) if post_p5_closed_count else 0,
                "cluster_wins": post_p5_clusters["wins"],
                "cluster_losses": post_p5_clusters["losses"],
                "cluster_winrate": post_p5_clusters["winrate"],
                "gross_profit": round(post_p5_gross_profit, 4),
                "gross_loss": round(post_p5_gross_loss, 4),
                "profit_factor": round(post_p5_profit_factor, 2),
                "cluster_profit_factor": post_p5_clusters["profit_factor"],
                "realized_pnl": round(post_p5_realized_pnl, 4),
                "avg_r": round(sum(post_p5_r_values) / post_p5_closed_count, 3) if post_p5_closed_count else 0,
                "cluster_avg_r": post_p5_clusters["avg_r"],
                "max_drawdown": post_p5_max_dd_pct,
                "max_drawdown_usdt": post_p5_max_dd_usdt,
                "sample_age_days": round(post_p5_sample_age_days, 2),
                "calendar_span_days": round(post_p5_span_days, 2),
                "closed_trades_per_day": round(post_p5_closed_count / post_p5_span_days, 2)
                if post_p5_span_days
                else 0,
                "closed_trade_clusters_per_day": round(
                    post_p5_clusters["closed_clusters"] / post_p5_span_days,
                    2,
                )
                if post_p5_span_days
                else 0,
                "first_trade_at": _fmt_dt(post_p5_first_dt),
                "last_trade_at": _fmt_dt(post_p5_last_dt),
                "trade_clusters": {
                    "window_minutes": SCORECARD_CLUSTER_WINDOW_MINUTES,
                    "closed_clusters": post_p5_clusters["closed_clusters"],
                    "largest_size": post_p5_clusters["largest_size"],
                    "multi_trade_clusters": post_p5_clusters["multi_trade_clusters"],
                    "wins": post_p5_clusters["wins"],
                    "losses": post_p5_clusters["losses"],
                    "winrate": post_p5_clusters["winrate"],
                    "profit_factor": post_p5_clusters["profit_factor"],
                    "avg_r": post_p5_clusters["avg_r"],
                },
                "exit_profile_breakdown": _exit_profile_breakdown(post_p5_closed),
                "strategy_logic_version_breakdown": _strategy_logic_version_breakdown(post_p5_closed),
            },
            "post_p5_closed_trades": post_p5_closed_count,
            "post_p5_closed_trade_clusters": post_p5_clusters["closed_clusters"],
            "post_p5_realized_pnl": round(post_p5_realized_pnl, 4),
            "pre_p5_closed_trades": pre_p5_closed_count,
            "wins": wins,
            "losses": losses,
            "winrate": round(wins / closed_count * 100, 1) if closed_count else 0,
            "cluster_wins": clusters["wins"],
            "cluster_losses": clusters["losses"],
            "cluster_winrate": clusters["winrate"],
            "gross_profit": round(gross_profit, 4),
            "gross_loss": round(gross_loss, 4),
            "profit_factor": round(profit_factor, 2),
            "cluster_profit_factor": clusters["profit_factor"],
            "realized_pnl": round(realized_pnl, 4),
            "unrealized_pnl": round(unrealized_pnl, 4),
            "total_pnl": round(realized_pnl + unrealized_pnl, 4),
            "avg_r": round(sum(r_values) / closed_count, 3) if closed_count else 0,
            "cluster_avg_r": clusters["avg_r"],
            "trade_clusters": {
                "window_minutes": SCORECARD_CLUSTER_WINDOW_MINUTES,
                "closed_clusters": clusters["closed_clusters"],
                "largest_size": clusters["largest_size"],
                "multi_trade_clusters": clusters["multi_trade_clusters"],
                "wins": clusters["wins"],
                "losses": clusters["losses"],
                "winrate": clusters["winrate"],
                "profit_factor": clusters["profit_factor"],
                "avg_r": clusters["avg_r"],
            },
            "max_drawdown": max_dd_pct,
            "max_drawdown_usdt": max_dd_usdt,
            "open_risk": round(open_risk, 4),
            "first_trade_at": _fmt_dt(first_dt),
            "last_trade_at": _fmt_dt(last_dt),
            "last_signal_at": _fmt_dt(item["last_signal_at"]),
            "sample_age_days": round(sample_age_days, 2),
            "calendar_span_days": round(span_days, 2),
            "closed_trades_per_day": round(closed_count / span_days, 2) if span_days else 0,
            "closed_trade_clusters_per_day": round(clusters["closed_clusters"] / span_days, 2) if span_days else 0,
            "rejections_total": item["rejections_total"],
            "rejections_by_type": dict(sorted(item["rejections_by_type"].items())),
            "by_symbol": _finalize_breakdown(by_symbol),
            "by_direction": _finalize_breakdown(by_direction),
            "open_positions": open_positions,
        }
        post_p5_evidence = row["post_p5_evidence"]
        gate_metrics = {
            **row,
            "closed_trades": post_p5_evidence["closed_trades"],
            "closed_trade_clusters": post_p5_evidence["closed_trade_clusters"],
            "wins": post_p5_evidence["wins"],
            "losses": post_p5_evidence["losses"],
            "winrate": post_p5_evidence["winrate"],
            "cluster_wins": post_p5_evidence["cluster_wins"],
            "cluster_losses": post_p5_evidence["cluster_losses"],
            "cluster_winrate": post_p5_evidence["cluster_winrate"],
            "profit_factor": post_p5_evidence["profit_factor"],
            "cluster_profit_factor": post_p5_evidence["cluster_profit_factor"],
            "realized_pnl": post_p5_evidence["realized_pnl"],
            "avg_r": post_p5_evidence["avg_r"],
            "cluster_avg_r": post_p5_evidence["cluster_avg_r"],
            "max_drawdown": post_p5_evidence["max_drawdown"],
            "max_drawdown_usdt": post_p5_evidence["max_drawdown_usdt"],
            "sample_age_days": post_p5_evidence["sample_age_days"],
            "calendar_span_days": post_p5_evidence["calendar_span_days"],
            "closed_trades_per_day": post_p5_evidence["closed_trades_per_day"],
            "closed_trade_clusters_per_day": post_p5_evidence["closed_trade_clusters_per_day"],
        }
        row["gate"] = evaluate_strategy_gate(gate_metrics, gate_thresholds)
        row["gate"]["evidence_scope"] = "post_p5_realistic_execution"
        row["gate"]["post_p5_closed_trades"] = post_p5_closed_count
        row["gate"]["post_p5_closed_trade_clusters"] = post_p5_clusters["closed_clusters"]
        row["gate"]["pre_p5_closed_trades"] = pre_p5_closed_count
        row["promotion_policy"] = apply_strategy_promotion_policy(row)
        strategies.append(row)
        summary["closed_trades"] += closed_count
        summary["closed_trade_clusters"] += clusters["closed_clusters"]
        summary["post_p5_closed_trades"] += post_p5_closed_count
        summary["post_p5_closed_trade_clusters"] += post_p5_clusters["closed_clusters"]
        summary["pre_p5_closed_trades"] += pre_p5_closed_count
        summary["open_trades"] += len(open_trades)
        summary["realized_pnl"] += realized_pnl
        summary["unrealized_pnl"] += unrealized_pnl
        summary["post_p5_realized_pnl"] += post_p5_realized_pnl
        summary["shadow_closed_trades"] += shadow_closed_count
        summary["shadow_open_trades"] += len(shadow_open)
        summary["shadow_realized_pnl"] += shadow_realized_pnl
        summary["shadow_unrealized_pnl"] += shadow_unrealized_pnl
        summary["rejections_total"] += item["rejections_total"]

    strategies.sort(key=lambda x: (x["total_pnl"], x["closed_trades"], -x["rejections_total"]), reverse=True)
    summary["strategies"] = len(strategies)
    summary["realized_pnl"] = round(summary["realized_pnl"], 4)
    summary["unrealized_pnl"] = round(summary["unrealized_pnl"], 4)
    summary["total_pnl"] = round(summary["realized_pnl"] + summary["unrealized_pnl"], 4)
    summary["post_p5_realized_pnl"] = round(summary["post_p5_realized_pnl"], 4)
    summary["shadow_realized_pnl"] = round(summary["shadow_realized_pnl"], 4)
    summary["shadow_unrealized_pnl"] = round(summary["shadow_unrealized_pnl"], 4)
    summary["shadow_total_pnl"] = round(summary["shadow_realized_pnl"] + summary["shadow_unrealized_pnl"], 4)
    return {
        "initial_equity": initial_equity,
        "gate_thresholds": STRATEGY_GATE_DEFAULTS if gate_thresholds is None else {**STRATEGY_GATE_DEFAULTS, **gate_thresholds},
        "summary": summary,
        "strategies": strategies,
    }


def _allocator_score(metrics: dict[str, Any]) -> float:
    closed = _to_float(metrics.get("closed_trades"), 0.0) or 0.0
    winrate = _to_float(metrics.get("winrate"), 0.0) or 0.0
    profit_factor = _to_float(metrics.get("profit_factor"), 0.0) or 0.0
    avg_r = _to_float(metrics.get("avg_r"), 0.0) or 0.0
    max_drawdown = _to_float(metrics.get("max_drawdown"), 0.0) or 0.0

    maturity = min(closed / 100.0, 1.0) * 20.0
    expectancy = max(min(avg_r / 0.50, 1.0), -1.0) * 30.0
    pf_score = max(min((profit_factor - 1.0) / 1.0, 1.0), 0.0) * 25.0
    win_score = max(min((winrate - 40.0) / 25.0, 1.0), 0.0) * 15.0
    drawdown_score = 10.0 if max_drawdown >= -5.0 else 5.0 if max_drawdown >= -10.0 else -15.0
    return round(max(maturity + expectancy + pf_score + win_score + drawdown_score, 0.0), 2)


def _allocator_metrics_for_row(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    mode = str(row.get("strategy_mode") or "").lower()
    if mode in {"shadow", "disabled"}:
        shadow = row.get("shadow_paper") or {}
        return "shadow_paper", {
            "closed_trades": shadow.get("closed_trades", 0),
            "open_trades": shadow.get("open_trades", 0),
            "winrate": shadow.get("winrate", 0),
            "profit_factor": shadow.get("profit_factor", 0),
            "avg_r": shadow.get("avg_r", 0),
            "max_drawdown": shadow.get("max_drawdown", 0),
            "total_pnl": shadow.get("total_pnl", 0),
            "open_risk": shadow.get("open_risk", 0),
        }
    post_p5 = row.get("post_p5_evidence") or {}
    if post_p5:
        return "post_p5_realistic_execution", {
            "closed_trades": post_p5.get("closed_trade_clusters") or post_p5.get("closed_trades", 0),
            "open_trades": row.get("open_trades", 0),
            "winrate": post_p5.get("cluster_winrate")
            if post_p5.get("cluster_winrate") is not None
            else post_p5.get("winrate", 0),
            "profit_factor": post_p5.get("cluster_profit_factor") or post_p5.get("profit_factor", 0),
            "avg_r": post_p5.get("cluster_avg_r")
            if post_p5.get("cluster_avg_r") is not None
            else post_p5.get("avg_r", 0),
            "max_drawdown": post_p5.get("max_drawdown", 0),
            "total_pnl": post_p5.get("realized_pnl", 0),
            "open_risk": row.get("open_risk", 0),
        }
    return "paper", {
        "closed_trades": row.get("closed_trade_clusters") or row.get("closed_trades", 0),
        "open_trades": row.get("open_trades", 0),
        "winrate": row.get("cluster_winrate") if row.get("cluster_winrate") is not None else row.get("winrate", 0),
        "profit_factor": row.get("cluster_profit_factor") or row.get("profit_factor", 0),
        "avg_r": row.get("cluster_avg_r") if row.get("cluster_avg_r") is not None else row.get("avg_r", 0),
        "max_drawdown": row.get("max_drawdown", 0),
        "total_pnl": row.get("total_pnl", 0),
        "open_risk": row.get("open_risk", 0),
    }


def build_strategy_allocator(scorecard: dict[str, Any]) -> dict[str, Any]:
    allocations: list[dict[str, Any]] = []
    for row in scorecard.get("strategies", []):
        strategy = str(row.get("strategy") or "UNKNOWN")
        mode = str(row.get("strategy_mode") or "unknown").lower()
        source, metrics = _allocator_metrics_for_row(row)
        closed = int(_to_float(metrics.get("closed_trades"), 0.0) or 0)
        avg_r = _to_float(metrics.get("avg_r"), 0.0) or 0.0
        profit_factor = _to_float(metrics.get("profit_factor"), 0.0) or 0.0
        total_pnl = _to_float(metrics.get("total_pnl"), 0.0) or 0.0
        gate = row.get("gate") or {}
        shadow_gate = row.get("shadow_gate") or {}
        policy = row.get("promotion_policy") or apply_strategy_promotion_policy(row)
        score = _allocator_score(metrics)
        reasons: list[str] = []

        if mode in {"paper", "live"}:
            if closed <= 0:
                action = "COLLECT_PAPER_EVIDENCE"
                cap = 0.0
                reasons.append("no closed post-P5 realistic paper clusters yet")
            elif avg_r <= 0 or profit_factor < 1:
                action = "REDUCE_OR_DISABLE_REVIEW"
                cap = 0.0
                reasons.append("paper expectancy is not positive")
            elif gate.get("status") == "PROMOTABLE":
                action = "CORE_CANDIDATE"
                cap = ALLOCATOR_WEIGHT_CAPS["CORE_CANDIDATE"]
                reasons.append("paper gate is promotable")
            elif strategy == "SQUEEZE_BREAKOUT":
                action = "CHAMPION_WATCH"
                cap = ALLOCATOR_WEIGHT_CAPS["CHAMPION_WATCH"]
                reasons.append("champion strategy remains positive but still collecting evidence")
            else:
                action = "KEEP_LIMITED_PAPER"
                cap = ALLOCATOR_WEIGHT_CAPS["KEEP_LIMITED_PAPER"]
                reasons.append("paper strategy is positive but not fully gated")
        elif mode == "shadow":
            if shadow_gate.get("promotion_candidate"):
                action = "PROMOTION_REVIEW"
                cap = ALLOCATOR_WEIGHT_CAPS["PROMOTION_REVIEW"]
                reasons.append("shadow gate suggests human promotion review")
            elif closed <= 0:
                action = "COLLECT_SHADOW_EVIDENCE"
                cap = 0.0
                reasons.append("no closed shadow-paper trades yet")
            elif avg_r <= 0 or profit_factor < 1 or total_pnl <= 0:
                action = "RESEARCH_ONLY"
                cap = 0.0
                reasons.append("shadow expectancy is not positive")
            else:
                action = "WATCH_SHADOW"
                cap = ALLOCATOR_WEIGHT_CAPS["WATCH_SHADOW"]
                reasons.append("positive shadow evidence, not enough for promotion")
        else:
            action = "DISABLED"
            cap = 0.0
            reasons.append("strategy is not active")

        suggested = round(min(cap, cap * score / 100.0), 2) if cap > 0 and score > 0 else 0.0
        reasons.extend(policy.get("reasons", [])[:2])
        allocations.append({
            "strategy": strategy,
            "mode": mode,
            "evidence_source": source,
            "action": action,
            "policy_action": policy.get("action"),
            "policy_tier": policy.get("tier"),
            "paper_review_allowed": bool(policy.get("paper_review_allowed")),
            "live_review_allowed": bool(policy.get("live_review_allowed")),
            "score": score,
            "suggested_risk_weight_pct": suggested,
            "max_risk_weight_pct": cap,
            "closed_trades_or_clusters": closed,
            "avg_r": round(avg_r, 3),
            "profit_factor": round(profit_factor, 3),
            "winrate": round(_to_float(metrics.get("winrate"), 0.0) or 0.0, 2),
            "max_drawdown": round(_to_float(metrics.get("max_drawdown"), 0.0) or 0.0, 2),
            "total_pnl": round(total_pnl, 4),
            "open_trades": int(_to_float(metrics.get("open_trades"), 0.0) or 0),
            "open_risk": round(_to_float(metrics.get("open_risk"), 0.0) or 0.0, 4),
            "reasons": reasons,
        })

    allocations.sort(
        key=lambda item: (
            item["suggested_risk_weight_pct"],
            item["score"],
            item["total_pnl"],
        ),
        reverse=True,
    )
    return {
        "generated_at": scorecard.get("generated_at") or datetime.now(timezone.utc).isoformat(),
        "mode": "ADVISORY_ONLY",
        "auto_switching_enabled": False,
        "summary": {
            "strategies": len(allocations),
            "positive_weight_count": sum(1 for item in allocations if item["suggested_risk_weight_pct"] > 0),
            "suggested_total_risk_weight_pct": round(sum(item["suggested_risk_weight_pct"] for item in allocations), 2),
        },
        "allocations": allocations,
    }


def build_weekly_research_report(
    scorecard: dict[str, Any],
    allocator: dict[str, Any],
    promotions: dict[str, Any],
    order_flow: dict[str, Any],
    ml_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    allocations = {row.get("strategy"): row for row in allocator.get("allocations", [])}
    rankings: list[dict[str, Any]] = []
    demotions: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    for row in scorecard.get("strategies", []):
        strategy = str(row.get("strategy") or "UNKNOWN")
        mode = str(row.get("strategy_mode") or "unknown").lower()
        shadow = row.get("shadow_paper") or {}
        policy = row.get("promotion_policy") or apply_strategy_promotion_policy(row)
        allocation = allocations.get(strategy, {})
        source = allocation.get("evidence_source") or ("shadow_paper" if mode in {"shadow", "disabled"} else "paper")
        if source == "shadow_paper":
            pnl = _to_float(shadow.get("total_pnl"), 0.0) or 0.0
            winrate = _to_float(shadow.get("winrate"), 0.0) or 0.0
            profit_factor = _to_float(shadow.get("profit_factor"), 0.0) or 0.0
            avg_r = _to_float(shadow.get("avg_r"), 0.0) or 0.0
            max_drawdown = _to_float(shadow.get("max_drawdown"), 0.0) or 0.0
            closed = int(_to_float(shadow.get("closed_trades"), 0) or 0)
            open_trades = int(_to_float(shadow.get("open_trades"), 0) or 0)
        else:
            post_p5 = row.get("post_p5_evidence") or {}
            pnl = _to_float(post_p5.get("realized_pnl", row.get("post_p5_realized_pnl")), 0.0) or 0.0
            winrate = _to_float(post_p5.get("cluster_winrate", row.get("cluster_winrate")), 0.0) or 0.0
            profit_factor = _to_float(post_p5.get("cluster_profit_factor", row.get("cluster_profit_factor")), 0.0) or 0.0
            avg_r = _to_float(post_p5.get("cluster_avg_r", row.get("cluster_avg_r")), 0.0) or 0.0
            max_drawdown = _to_float(post_p5.get("max_drawdown", row.get("max_drawdown")), 0.0) or 0.0
            closed = int(_to_float(post_p5.get("closed_trade_clusters", row.get("closed_trade_clusters")), 0) or 0)
            open_trades = int(_to_float(row.get("open_trades"), 0) or 0)

        order_flow_summary = (row.get("candidate_evidence") or {}).get("order_flow") or {}
        of_score = _to_float(order_flow_summary.get("avg_score"), 0.0) or 0.0
        score = _research_score(pnl, profit_factor, avg_r, winrate, max_drawdown, of_score, closed)
        action = str(allocation.get("policy_action") or policy.get("action") or allocation.get("action") or "REVIEW")
        ranking = {
            "strategy": strategy,
            "mode": mode,
            "tier": policy.get("tier"),
            "evidence_source": source,
            "score": score,
            "action": action,
            "closed": closed,
            "open": open_trades,
            "pnl": round(pnl, 4),
            "winrate": round(winrate, 2),
            "profit_factor": round(profit_factor, 3),
            "avg_r": round(avg_r, 3),
            "max_drawdown": round(max_drawdown, 2),
            "order_flow_avg_score": round(of_score, 3),
            "allocation": {
                "action": allocation.get("action"),
                "suggested_risk_weight_pct": allocation.get("suggested_risk_weight_pct", 0),
                "max_risk_weight_pct": allocation.get("max_risk_weight_pct", 0),
            },
            "policy_reasons": policy.get("reasons", []),
        }
        rankings.append(ranking)

        if mode in {"paper", "live"} and (
            avg_r <= 0 or profit_factor < 1 or action in {"KEEP_LIMITED_PAPER_OR_DEMOTE_REVIEW"}
        ) and closed > 0:
            demotions.append({
                "strategy": strategy,
                "reason": "paper/live expectancy is weak or policy asks for demotion review",
                "metrics": {k: ranking[k] for k in ("closed", "pnl", "profit_factor", "avg_r", "max_drawdown")},
            })
        if mode == "shadow" and closed >= 10 and (pnl <= 0 or avg_r <= 0 or profit_factor < 1):
            anomalies.append({
                "strategy": strategy,
                "type": "negative_shadow_edge",
                "reason": "shadow strategy has enough closed trades but no positive post-cost edge",
                "metrics": {k: ranking[k] for k in ("closed", "pnl", "profit_factor", "avg_r", "order_flow_avg_score")},
            })
        if of_score and of_score < 0.35:
            anomalies.append({
                "strategy": strategy,
                "type": "weak_order_flow",
                "reason": "recent order-flow evidence is weak",
                "metrics": {"order_flow_avg_score": round(of_score, 3), "closed": closed},
            })

    rankings.sort(key=lambda item: (item["score"], item["pnl"], item["closed"]), reverse=True)
    promotion_candidates = promotions.get("candidates", [])
    research_retest = [
        item for item in promotion_candidates
        if str(item.get("recommendation") or "").startswith("RESEARCH_RETEST")
    ]
    ml = ml_report or {}
    ml_summary = {
        "available": bool(ml),
        "validated": bool(ml.get("validated")) if ml else False,
        "baseline_trades": ((ml.get("baseline") or {}).get("trades") if ml else 0) or 0,
        "filtered_trades": ((ml.get("ml_filtered") or {}).get("trades") if ml else 0) or 0,
        "improvement": (ml.get("comparison") or {}).get("total_r_improvement") if ml else None,
    }
    recommendations = _weekly_report_recommendations(rankings, promotion_candidates, demotions, anomalies, ml_summary)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": "rolling_week",
        "mode": "ADVISORY_ONLY",
        "summary": {
            "strategies": len(rankings),
            "promotion_candidates": len(promotion_candidates),
            "research_retest_candidates": len(research_retest),
            "demotion_reviews": len(demotions),
            "anomalies": len(anomalies),
            "order_flow_annotations": (order_flow.get("summary") or {}).get("total", 0),
        },
        "ranking": rankings,
        "promotion_candidates": promotion_candidates,
        "demotion_reviews": demotions,
        "anomalies": anomalies,
        "order_flow": order_flow.get("summary", {}),
        "ml": ml_summary,
        "recommendations": recommendations,
    }


def build_production_readiness_report(
    scorecard: dict[str, Any],
    testnet_evidence: dict[str, Any] | None = None,
    chaos_evidence: dict[str, Any] | None = None,
    production_unlock: dict[str, Any] | None = None,
    live_strategies: list[str] | None = None,
) -> dict[str, Any]:
    """Build a strict, advisory-only live readiness report from current evidence."""
    testnet_evidence = testnet_evidence or {}
    chaos_evidence = chaos_evidence or {}
    if "passed" not in chaos_evidence and "scenarios" in chaos_evidence:
        chaos_evidence = build_chaos_readiness_report(chaos_evidence)
    production_unlock = production_unlock or {}
    live_strategies = live_strategies or ["SQUEEZE_BREAKOUT"]
    rows = {str(row.get("strategy") or ""): row for row in scorecard.get("strategies", [])}
    summary = scorecard.get("summary") or {}
    checks: list[dict[str, Any]] = []

    post_p5_closed = int(_to_float(summary.get("post_p5_closed_trades"), 0) or 0)
    checks.append(_readiness_check(
        "post_p5_closed_trades",
        post_p5_closed >= 500,
        "At least 500 closed v2.1 post-P5 realistic paper/testnet trades.",
        actual=post_p5_closed,
        required=500,
    ))

    for strategy in live_strategies:
        row = rows.get(strategy) or {}
        post_p5 = row.get("post_p5_evidence") or {}
        clusters = int(_to_float(post_p5.get("closed_trade_clusters"), 0) or 0)
        profit_factor = _to_float(post_p5.get("cluster_profit_factor"), 0.0) or 0.0
        winrate = _to_float(post_p5.get("cluster_winrate"), 0.0) or 0.0
        avg_r = _to_float(post_p5.get("cluster_avg_r"), 0.0) or 0.0
        max_drawdown = _to_float(post_p5.get("max_drawdown"), 0.0) or 0.0
        prefix = f"{strategy.lower()}_"
        checks.extend([
            _readiness_check(
                prefix + "closed_clusters",
                clusters >= 100,
                f"{strategy} needs at least 100 closed post-P5 trade clusters.",
                actual=clusters,
                required=100,
            ),
            _readiness_check(
                prefix + "profit_factor",
                profit_factor >= 1.30,
                f"{strategy} post-P5 profit factor must be >= 1.30.",
                actual=round(profit_factor, 3),
                required=1.30,
            ),
            _readiness_check(
                prefix + "winrate",
                winrate >= 40.0,
                f"{strategy} post-P5 winrate must be >= 40%.",
                actual=round(winrate, 2),
                required=40.0,
            ),
            _readiness_check(
                prefix + "avg_r",
                avg_r >= 0.25,
                f"{strategy} post-P5 average R must be >= +0.25.",
                actual=round(avg_r, 3),
                required=0.25,
            ),
            _readiness_check(
                prefix + "max_drawdown",
                max_drawdown >= -10.0,
                f"{strategy} post-P5 max drawdown must be better than -10%.",
                actual=round(max_drawdown, 2),
                required=">= -10.0",
            ),
        ])
        if avg_r < 0:
            checks.append(_readiness_check(
                prefix + "no_negative_avg_r",
                False,
                f"{strategy} has negative average R and cannot be live-enabled.",
                actual=round(avg_r, 3),
                required=">= 0",
            ))

    lifecycle_required = {
        "entry": "Testnet entry order evidence.",
        "stop_loss": "Testnet SL placement/fill evidence.",
        "take_profit": "Testnet TP placement/fill evidence.",
        "cancel": "Testnet cancel/cleanup evidence.",
        "partial_fill": "Testnet partial-fill scaling evidence.",
        "restart_recovery": "Testnet restart recovery evidence.",
    }
    lifecycle = testnet_evidence.get("lifecycle") or testnet_evidence
    for key, description in lifecycle_required.items():
        checks.append(_readiness_check(
            f"testnet_{key}",
            bool(lifecycle.get(key)),
            description,
            actual=bool(lifecycle.get(key)),
            required=True,
        ))

    checks.extend([
        _readiness_check(
            "duplicate_orders",
            int(_to_float(testnet_evidence.get("duplicate_orders"), 999)) == 0,
            "No duplicate orders/positions in lifecycle evidence.",
            actual=testnet_evidence.get("duplicate_orders"),
            required=0,
        ),
        _readiness_check(
            "unprotected_positions",
            int(_to_float(testnet_evidence.get("unprotected_positions"), 999)) == 0,
            "No unprotected testnet/live positions after restart.",
            actual=testnet_evidence.get("unprotected_positions"),
            required=0,
        ),
        _readiness_check(
            "critical_incidents",
            int(_to_float(testnet_evidence.get("critical_incidents"), 999)) == 0,
            "No critical technical incidents in the soak window.",
            actual=testnet_evidence.get("critical_incidents"),
            required=0,
        ),
        _readiness_check(
            "soak_days",
            (_to_float(testnet_evidence.get("soak_days"), 0.0) or 0.0) >= 14.0,
            "At least 14 continuous paper/testnet soak days.",
            actual=testnet_evidence.get("soak_days"),
            required=14,
        ),
        _readiness_check(
            "chaos_scenarios",
            bool(chaos_evidence.get("passed")),
            "P3-03 chaos evidence must pass reboot, API timeout, network loss and stale-stream scenarios.",
            actual={
                "passed": bool(chaos_evidence.get("passed")),
                "blocked": chaos_evidence.get("summary", {}).get("blocked"),
                "source": chaos_evidence.get("source"),
            },
            required="all P3-03 required scenarios PASS",
        ),
        _readiness_check(
            "production_unlock",
            bool(production_unlock.get("human_approved_by"))
            and bool(production_unlock.get("backtest_approved"))
            and bool(production_unlock.get("paper_trading_approved")),
            "Human-reviewed production unlock must approve backtest and paper trading.",
            actual={
                "human_approved_by": production_unlock.get("human_approved_by"),
                "backtest_approved": bool(production_unlock.get("backtest_approved")),
                "paper_trading_approved": bool(production_unlock.get("paper_trading_approved")),
            },
            required="human_approved_by + backtest_approved + paper_trading_approved",
        ),
    ])

    blocked = [check for check in checks if check["status"] == "BLOCKED"]
    warnings = [check for check in checks if check["status"] == "WARN"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "ADVISORY_ONLY",
        "ready_for_mainnet": not blocked,
        "status": "READY" if not blocked else "BLOCKED",
        "summary": {
            "checks": len(checks),
            "passed": sum(1 for check in checks if check["status"] == "PASS"),
            "blocked": len(blocked),
            "warnings": len(warnings),
            "live_strategies": live_strategies,
        },
        "checks": checks,
        "blockers": blocked,
        "warnings": warnings,
        "notes": [
            "This report is advisory and cannot unlock MAINNET_LIVE by itself.",
            "Production unlock remains a separate human-reviewed file.",
        ],
    }


CHAOS_REQUIRED_SCENARIOS = {
    "control_api_timeout": "Control API remains responsive when a status/systemctl probe is slow or times out.",
    "dashboard_status_debounce": "Dashboard does not show hard offline after one transient status miss.",
    "service_restart_recovery": "Trading bot, paper monitor and control API recover after service restart.",
    "binance_timeout_backoff": "Binance timeout/rate-limit path backs off without duplicate orders.",
    "network_loss_recovery": "Temporary network loss blocks new risky entries and resumes cleanly.",
    "stale_user_stream_rest_fallback": "Stale user stream triggers REST reconciliation fallback before live entries.",
    "vps_reboot_recovery": "VPS reboot restores services and leaves no duplicate or unprotected positions.",
}


def build_chaos_readiness_report(evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an advisory P3-03 chaos-test readiness report from an evidence JSON file."""
    evidence = evidence or {}
    scenarios = evidence.get("scenarios") or {}
    checks: list[dict[str, Any]] = []

    if not evidence:
        checks.append(_readiness_check(
            "chaos_evidence_file",
            False,
            "Chaos evidence file data/chaos_evidence.json is missing.",
            actual="missing",
            required="present",
        ))

    for scenario_id, description in CHAOS_REQUIRED_SCENARIOS.items():
        value = scenarios.get(scenario_id)
        if isinstance(value, dict):
            passed = bool(value.get("passed")) or str(value.get("status") or "").upper() == "PASS"
            actual: Any = {
                "passed": passed,
                "status": value.get("status"),
                "observed_at": value.get("observed_at"),
                "notes": value.get("notes"),
            }
        else:
            passed = bool(value)
            actual = value
        checks.append(_readiness_check(
            scenario_id,
            passed,
            description,
            actual=actual,
            required=True,
        ))

    checks.extend([
        _readiness_check(
            "duplicate_orders_after_chaos",
            int(_to_float(evidence.get("duplicate_orders_after_chaos"), 999)) == 0,
            "Chaos tests must leave zero duplicate orders/positions.",
            actual=evidence.get("duplicate_orders_after_chaos"),
            required=0,
        ),
        _readiness_check(
            "unprotected_positions_after_chaos",
            int(_to_float(evidence.get("unprotected_positions_after_chaos"), 999)) == 0,
            "Chaos tests must leave zero positions without verified SL/TP protection.",
            actual=evidence.get("unprotected_positions_after_chaos"),
            required=0,
        ),
        _readiness_check(
            "critical_incidents_after_chaos",
            int(_to_float(evidence.get("critical_incidents_after_chaos"), 999)) == 0,
            "Chaos tests must produce zero unresolved critical technical incidents.",
            actual=evidence.get("critical_incidents_after_chaos"),
            required=0,
        ),
    ])

    blocked = [check for check in checks if check["status"] == "BLOCKED"]
    warnings = [check for check in checks if check["status"] == "WARN"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "ADVISORY_ONLY",
        "source": evidence.get("source") or "data/chaos_evidence.json",
        "passed": not blocked,
        "status": "PASS" if not blocked else "BLOCKED",
        "summary": {
            "checks": len(checks),
            "passed": sum(1 for check in checks if check["status"] == "PASS"),
            "blocked": len(blocked),
            "warnings": len(warnings),
        },
        "checks": checks,
        "blockers": blocked,
        "warnings": warnings,
        "notes": [
            "This report is evidence-gated; destructive scenarios remain manual/operator-controlled.",
            "Do not mark MAINNET_LIVE ready until all required P3-03 scenarios pass.",
        ],
    }


def build_soak_readiness_report(evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an advisory P3-04 14-day paper/testnet soak readiness report."""
    evidence = evidence or {}
    soak_days = _to_float(evidence.get("soak_days"), 0.0) or 0.0
    critical_incidents = _to_int(evidence.get("critical_incidents"), 999)
    duplicate_orders = _to_int(evidence.get("duplicate_orders"), 999)
    unprotected_positions = _to_int(evidence.get("unprotected_positions"), 999)
    started_at = evidence.get("started_at") or evidence.get("first_seen_at")
    last_checked_at = evidence.get("last_checked_at") or evidence.get("finished_at") or evidence.get("generated_at")

    checks = [
        _readiness_check(
            "soak_evidence_file",
            bool(evidence),
            "Soak evidence file data/soak_evidence.json or data/testnet_lifecycle_evidence.json is missing.",
            actual="present" if evidence else "missing",
            required="present",
        ),
        _readiness_check(
            "soak_days",
            soak_days >= 14.0,
            "At least 14 continuous paper/testnet soak days.",
            actual=round(soak_days, 2),
            required=14.0,
        ),
        _readiness_check(
            "critical_incidents",
            critical_incidents == 0,
            "Soak window must have zero unresolved critical technical incidents.",
            actual=critical_incidents,
            required=0,
        ),
        _readiness_check(
            "duplicate_orders",
            duplicate_orders == 0,
            "Soak window must have zero duplicate orders.",
            actual=duplicate_orders,
            required=0,
        ),
        _readiness_check(
            "unprotected_positions",
            unprotected_positions == 0,
            "Soak window must have zero unprotected positions.",
            actual=unprotected_positions,
            required=0,
        ),
    ]
    blocked = [check for check in checks if check["status"] == "BLOCKED"]
    warnings = [check for check in checks if check["status"] == "WARN"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "ADVISORY_ONLY",
        "source": evidence.get("source") or "data/soak_evidence.json",
        "started_at": started_at,
        "last_checked_at": last_checked_at,
        "passed": not blocked,
        "status": "PASS" if not blocked else "BLOCKED",
        "summary": {
            "soak_days": round(soak_days, 2),
            "critical_incidents": critical_incidents,
            "duplicate_orders": duplicate_orders,
            "unprotected_positions": unprotected_positions,
            "checks": len(checks),
            "passed": sum(1 for check in checks if check["status"] == "PASS"),
            "blocked": len(blocked),
            "warnings": len(warnings),
        },
        "checks": checks,
        "blockers": blocked,
        "warnings": warnings,
        "notes": [
            "This report does not create soak evidence automatically; it verifies operator/runtime evidence.",
            "P3-04 is complete only after 14 continuous days with zero critical incidents.",
        ],
    }


def build_monthly_target_report(
    scorecard: dict[str, Any],
    *,
    initial_equity: float = 1000.0,
    target_monthly_return_pct: float = 10.0,
    base_risk_pct: float = 0.02,
) -> dict[str, Any]:
    """Estimate whether current evidence can mathematically support a monthly return target."""
    target_monthly_return_pct = max(float(target_monthly_return_pct), 0.0)
    base_risk_pct = max(float(base_risk_pct), 0.0001)
    initial_equity = max(float(initial_equity), 0.0001)
    target_r_month = target_monthly_return_pct / (base_risk_pct * 100.0)
    rows: list[dict[str, Any]] = []
    recommendations: list[str] = []

    for row in scorecard.get("strategies", []):
        strategy = str(row.get("strategy") or "UNKNOWN")
        mode = str(row.get("strategy_mode") or "unknown").lower()
        if mode == "shadow":
            metrics = row.get("shadow_paper") or {}
            scope = "shadow_paper"
            closed = int(_to_float(metrics.get("closed_trades"), 0) or 0)
            avg_r = _to_float(metrics.get("avg_r"), 0.0) or 0.0
            profit_factor = _to_float(metrics.get("profit_factor"), 0.0) or 0.0
            winrate = _to_float(metrics.get("winrate"), 0.0) or 0.0
            pnl = _to_float(metrics.get("realized_pnl"), 0.0) or 0.0
            sample_age_days = _to_float(metrics.get("sample_age_days"), 0.0) or 0.0
            open_trades = int(_to_float(metrics.get("open_trades"), 0) or 0)
        else:
            metrics = row.get("post_p5_evidence") or {}
            scope = "post_p5_realistic_execution"
            closed = int(_to_float(metrics.get("closed_trade_clusters"), metrics.get("closed_trades", 0)) or 0)
            avg_r = _to_float(metrics.get("cluster_avg_r"), metrics.get("avg_r", 0.0)) or 0.0
            profit_factor = _to_float(metrics.get("cluster_profit_factor"), metrics.get("profit_factor", 0.0)) or 0.0
            winrate = _to_float(metrics.get("cluster_winrate"), metrics.get("winrate", 0.0)) or 0.0
            pnl = _to_float(metrics.get("realized_pnl"), 0.0) or 0.0
            sample_age_days = _to_float(metrics.get("sample_age_days"), 0.0) or 0.0
            open_trades = int(_to_float(row.get("open_trades"), 0) or 0)

        evidence_days = max(sample_age_days, 1.0 if closed else 0.0)
        clusters_per_day = closed / evidence_days if evidence_days else 0.0
        monthly_clusters = clusters_per_day * 30.0
        projected_monthly_r = avg_r * monthly_clusters
        projected_monthly_return_pct = projected_monthly_r * base_risk_pct * 100.0
        realized_monthly_return_pct = (pnl / initial_equity) * (30.0 / evidence_days) * 100.0 if evidence_days else 0.0
        required_avg_r = target_r_month / monthly_clusters if monthly_clusters > 0 else None
        required_clusters_at_current_avg_r = target_r_month / avg_r if avg_r > 0 else None
        monthly_cluster_gap = (
            max(0.0, required_clusters_at_current_avg_r - monthly_clusters)
            if required_clusters_at_current_avg_r is not None
            else None
        )
        avg_r_gap = max(0.0, required_avg_r - avg_r) if required_avg_r is not None else None

        blockers: list[str] = []
        if closed < 20:
            blockers.append("low_sample")
        if avg_r <= 0:
            blockers.append("non_positive_avg_r")
        if profit_factor and profit_factor < 1.20:
            blockers.append("low_profit_factor")
        if projected_monthly_return_pct < target_monthly_return_pct:
            blockers.append("target_gap")

        if not blockers and closed >= 100 and profit_factor >= 1.30 and avg_r >= 0.25:
            status = "ON_TRACK"
        elif projected_monthly_return_pct >= target_monthly_return_pct and avg_r > 0:
            status = "WATCH_SAMPLE"
        elif avg_r > 0 and profit_factor >= 1.0:
            status = "IMPROVE_OR_SCALE"
        else:
            status = "BLOCKED"
        if closed < 20:
            primary_blocker = "sample"
        elif avg_r <= 0:
            primary_blocker = "expectancy"
        elif projected_monthly_return_pct < target_monthly_return_pct and monthly_clusters < (
            required_clusters_at_current_avg_r or float("inf")
        ):
            primary_blocker = "frequency"
        elif projected_monthly_return_pct < target_monthly_return_pct:
            primary_blocker = "avg_r"
        else:
            primary_blocker = "none"

        rows.append({
            "strategy": strategy,
            "mode": mode,
            "scope": scope,
            "status": status,
            "closed_clusters": closed,
            "open_trades": open_trades,
            "sample_age_days": round(sample_age_days, 2),
            "clusters_per_day": round(clusters_per_day, 3),
            "monthly_clusters_estimate": round(monthly_clusters, 2),
            "avg_r": round(avg_r, 3),
            "profit_factor": round(profit_factor, 3),
            "winrate": round(winrate, 1),
            "realized_pnl": round(pnl, 4),
            "projected_monthly_r": round(projected_monthly_r, 3),
            "projected_monthly_return_pct": round(projected_monthly_return_pct, 2),
            "realized_monthly_return_pct": round(realized_monthly_return_pct, 2),
            "required_avg_r_at_current_frequency": round(required_avg_r, 3) if required_avg_r is not None else None,
            "required_monthly_clusters_at_current_avg_r": (
                round(required_clusters_at_current_avg_r, 2) if required_clusters_at_current_avg_r is not None else None
            ),
            "monthly_cluster_gap_at_current_avg_r": round(monthly_cluster_gap, 2) if monthly_cluster_gap is not None else None,
            "avg_r_gap_at_current_frequency": round(avg_r_gap, 3) if avg_r_gap is not None else None,
            "primary_blocker": primary_blocker,
            "blockers": blockers,
        })

    rows.sort(key=lambda item: (item["projected_monthly_return_pct"], item["closed_clusters"]), reverse=True)
    viable = [
        row
        for row in rows
        if row["status"] in {"ON_TRACK", "IMPROVE_OR_SCALE"}
        and "low_sample" not in row["blockers"]
        and row["avg_r"] > 0
        and row["profit_factor"] >= 1.20
    ]
    watch_sample = [row for row in rows if row["status"] == "WATCH_SAMPLE"]
    combined_projected = sum(row["projected_monthly_return_pct"] for row in viable)
    if not viable:
        recommendations.append("No strategy currently has positive enough evidence to support scaling toward the monthly target.")
    if any(row["strategy"] == "SQUEEZE_BREAKOUT" and row["avg_r"] <= 0 for row in rows):
        recommendations.append("SQUEEZE_BREAKOUT needs positive post-P5 Avg R before increasing risk or leverage.")
    if any(row["mode"] == "shadow" and row["status"] == "WATCH_SAMPLE" for row in rows):
        recommendations.append("Shadow winners need more closed clusters before promotion; do not scale from a spike.")
    if watch_sample:
        recommendations.append("WATCH_SAMPLE projections are excluded from the target summary until the sample is large enough.")
    if combined_projected < target_monthly_return_pct:
        recommendations.append("The current viable strategy mix is below target; improve Avg R first, then consider frequency/risk.")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "ADVISORY_ONLY",
        "target_monthly_return_pct": round(target_monthly_return_pct, 2),
        "base_risk_pct": round(base_risk_pct, 4),
        "target_r_month": round(target_r_month, 2),
        "initial_equity": round(initial_equity, 2),
        "summary": {
            "strategies": len(rows),
            "viable_strategies": len(viable),
            "combined_projected_monthly_return_pct": round(combined_projected, 2),
            "target_gap_pct": round(target_monthly_return_pct - combined_projected, 2),
            "status": "ON_TRACK" if combined_projected >= target_monthly_return_pct else "BELOW_TARGET",
        },
        "strategies": rows,
        "recommendations": recommendations,
    }


def _readiness_check(
    check_id: str,
    passed: bool,
    description: str,
    *,
    actual: Any,
    required: Any,
    warn: bool = False,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": "PASS" if passed else "WARN" if warn else "BLOCKED",
        "description": description,
        "actual": actual,
        "required": required,
    }


def _research_score(
    pnl: float,
    profit_factor: float,
    avg_r: float,
    winrate: float,
    max_drawdown: float,
    order_flow_score: float,
    closed: int,
) -> float:
    maturity = min(closed / 50.0, 1.0) * 15.0
    pnl_score = max(min(pnl / 100.0, 1.0), -1.0) * 20.0
    pf_score = max(min((profit_factor - 1.0) / 1.0, 1.0), -1.0) * 20.0
    avg_r_score = max(min(avg_r / 0.25, 1.0), -1.0) * 20.0
    win_score = max(min((winrate - 45.0) / 25.0, 1.0), -1.0) * 10.0
    dd_score = 10.0 if max_drawdown >= -5.0 else 2.0 if max_drawdown >= -10.0 else -10.0
    of_score = max(min((order_flow_score - 0.35) / 0.35, 1.0), -1.0) * 5.0 if order_flow_score else 0.0
    return round(max(maturity + pnl_score + pf_score + avg_r_score + win_score + dd_score + of_score, 0.0), 2)


def _weekly_report_recommendations(
    rankings: list[dict[str, Any]],
    promotions: list[dict[str, Any]],
    demotions: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
    ml_summary: dict[str, Any],
) -> list[str]:
    recommendations: list[str] = []
    if rankings:
        leader = rankings[0]
        recommendations.append(
            f"Top strategy by research score: {leader['strategy']} ({leader['mode']}, score={leader['score']})."
        )
    if promotions:
        recommendations.append("Review promotion candidates manually; auto-promotion remains disabled.")
    if demotions:
        recommendations.append("Review paper/live demotion candidates before increasing risk.")
    if anomalies:
        recommendations.append("Investigate anomaly list before trusting shadow edge.")
    if not ml_summary.get("available"):
        recommendations.append("ML walk-forward report is missing; keep ML advisory only.")
    elif not ml_summary.get("validated"):
        recommendations.append("ML walk-forward report is not validated; do not enforce ML decisions.")
    return recommendations


def service_status(name: str) -> str:
    now = time.monotonic()
    cached = _SERVICE_STATUS_CACHE.get(name)
    if cached and now - cached[0] <= SERVICE_STATUS_CACHE_SECONDS:
        return cached[1]
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=SERVICE_STATUS_TIMEOUT_SECONDS,
        )
        status = result.stdout.strip() or "unknown"
    except subprocess.TimeoutExpired:
        status = cached[1] if cached else "unknown"
    except Exception:
        status = "unknown"
    _SERVICE_STATUS_CACHE[name] = (now, status)
    return status


def api_status() -> dict:
    return {
        "bot": service_status(BOT_SERVICE_NAME),
        "paper_monitor": service_status(PAPER_MONITOR_SERVICE_NAME),
        "bot_service": BOT_SERVICE_NAME,
        "paper_monitor_service": PAPER_MONITOR_SERVICE_NAME,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def api_positions() -> list[dict]:
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, stop_loss, take_profit,
                   quantity, mode, status, risk_amount, created_at, metadata
            FROM trades
            WHERE status IN ('ACCEPTED', 'OPEN', 'ACTIVE')
            ORDER BY id DESC
        """).fetchall()
        conn.close()
        result = []
        for row in rows:
            t = dict(row)
            symbol = t["symbol"]
            entry = float(t["entry_price"] or 0)
            sl = float(t["stop_loss"] or 0)
            tp = float(t["take_profit"] or 0)
            qty = float(t["quantity"] or 0)
            risk = float(t["risk_amount"] or 0)
            price = get_price(symbol)
            pnl = None
            pnl_pct = None
            if price and entry and qty:
                if t["direction"] == "LONG":
                    pnl = (price - entry) * qty
                else:
                    pnl = (entry - price) * qty
                if risk > 0:
                    pnl_pct = pnl / risk * 100
            result.append({
                **t,
                "current_price": price,
                "unrealized_pnl": round(pnl, 4) if pnl is not None else None,
                "pnl_r": round(pnl_pct / 100, 3) if pnl_pct is not None else None,
            })
        return result
    except Exception as e:
        return [{"error": str(e)}]


def api_stats() -> dict:
    try:
        conn = get_db()
        trades = conn.execute("""
            SELECT realized_pnl, r_multiple, direction, symbol, created_at, metadata
            FROM trades WHERE status = 'CLOSED'
            ORDER BY id DESC
        """).fetchall()
        conn.close()

        total = len(trades)
        if total == 0:
            initial_equity = float(api_config().get("initial_equity_usdt", 1000.0))
            return {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "winrate": 0,
                "profit_factor": 0,
                "avg_r": 0,
                "total_pnl": 0,
                "max_drawdown": 0,
                "initial_equity": initial_equity,
                "equity_curve": [initial_equity],
                "by_strategy": {},
            }

        wins = sum(1 for t in trades if float(t["realized_pnl"] or 0) > 0)
        total_pnl = sum(float(t["realized_pnl"] or 0) for t in trades)
        gross_profit = sum(float(t["realized_pnl"] or 0) for t in trades if float(t["realized_pnl"] or 0) > 0)
        gross_loss = sum(abs(float(t["realized_pnl"] or 0)) for t in trades if float(t["realized_pnl"] or 0) < 0)
        avg_r = sum(float(t["r_multiple"] or 0) for t in trades) / total
        pf = gross_profit / gross_loss if gross_loss > 0 else gross_profit

        # Equity curve
        initial_equity = float(api_config().get("initial_equity_usdt", 1000.0))
        balance = initial_equity
        equity = [balance]
        peak = balance
        max_dd = 0.0
        for t in reversed(trades):
            balance += float(t["realized_pnl"] or 0)
            equity.append(round(balance, 4))
            if balance > peak:
                peak = balance
            dd = (balance - peak) / peak * 100 if peak > 0 else 0
            if dd < max_dd:
                max_dd = dd

        # По стратегиям
        strategies = {}
        for t in trades:
            strat = _strategy_from_trade(t)
            if strat not in strategies:
                strategies[strat] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0, "r_sum": 0}
            strategies[strat]["trades"] += 1
            pnl_val = float(t["realized_pnl"] or 0)
            if pnl_val > 0:
                strategies[strat]["wins"] += 1
            if pnl_val < 0:
                strategies[strat]["losses"] += 1
            strategies[strat]["pnl"] += pnl_val
            strategies[strat]["r_sum"] += float(t["r_multiple"] or 0)

        for value in strategies.values():
            count = value["trades"]
            value["pnl"] = round(value["pnl"], 4)
            value["winrate"] = round(value["wins"] / count * 100, 1) if count else 0
            value["avg_r"] = round(value.pop("r_sum") / count, 3) if count else 0

        return {
            "trades": total,
            "wins": wins,
            "losses": total - wins,
            "winrate": round(wins / total * 100, 1),
            "profit_factor": round(pf, 2),
            "avg_r": round(avg_r, 3),
            "total_pnl": round(total_pnl, 4),
            "max_drawdown": round(max_dd, 2),
            "initial_equity": initial_equity,
            "equity_curve": equity[-50:],  # последние 50 точек
            "by_strategy": strategies,
        }
    except Exception as e:
        return {"error": str(e)}


def api_signals(limit: int = 50) -> list[dict]:
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT symbol, direction, confidence, reason, created_at, metadata
            FROM signals ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def api_trades(limit: int = 50) -> list[dict]:
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, stop_loss, take_profit,
                   quantity, status, realized_pnl, r_multiple, created_at, metadata
            FROM trades ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        return [_with_execution_costs(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def api_shadow_trades(limit: int = 100) -> list[dict]:
    try:
        conn = get_db()
        ensure_shadow_trades_table(conn)
        rows = conn.execute("""
            SELECT id, created_at, closed_at, symbol, direction, strategy,
                   quantity, entry_price, stop_loss, take_profit, mode, status,
                   risk_amount, realized_pnl, r_multiple, close_reason, metadata
            FROM shadow_trades
            ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        return [_with_execution_costs(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def api_logs(lines: int = 100) -> list[str]:
    try:
        p = Path(LOG_PATH)
        if not p.exists():
            return []
        all_lines = p.read_text(errors="ignore").splitlines()
        # Фильтруем HTTP мусор
        filtered = [l for l in all_lines if not any(x in l for x in [
            "HTTP Request", "httpx", "httpcore", "Skipping", "receive_", "send_request"
        ])]
        return filtered[-lines:]
    except Exception as e:
        return [str(e)]


def api_risk_state() -> dict:
    try:
        conn = get_db()
        row = conn.execute("SELECT * FROM risk_state WHERE id=1").fetchone()
        conn.close()
        if row:
            return dict(row)
        return {"losing_streak": 0, "cooldown_until": None, "realized_pnl_today": "0"}
    except Exception:
        return {}


def emergency_stop() -> dict:
    Path(EMERGENCY_STOP_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(EMERGENCY_STOP_FILE).touch()
    return {"status": "emergency_stop_activated"}


def clear_emergency_stop() -> dict:
    p = Path(EMERGENCY_STOP_FILE)
    if p.exists():
        p.unlink()
    return {"status": "emergency_stop_cleared"}


def restart_bot() -> dict:
    result = subprocess.run(["systemctl", "restart", BOT_SERVICE_NAME], capture_output=True, text=True)
    if result.returncode != 0:
        return {"status": "error", "stderr": result.stderr.strip(), "stdout": result.stdout.strip()}
    return {"status": "restarting", "service": BOT_SERVICE_NAME}

def api_config() -> dict:
    try:
        import sys
        sys.path.insert(0, f"{BOT_ROOT}/src")
        from trading_bot.config import load_config
        cfg = load_config()
        eq = cfg.account.initial_equity_usdt
        return {
            "initial_equity_usdt": float(eq) if eq else 1000.0,
            "mode": cfg.mode.value,
            "strategy_modes": cfg.strategy.mode_summary(),
            "execution_strategies": cfg.strategy.execution_strategies(cfg.mode),
            "shadow_strategies": cfg.strategy.shadow_strategies(),
            "ml": {
                "enabled": cfg.ml.enabled,
                "enforce_decisions": cfg.ml.enforce_decisions,
                "decision_min_trades": cfg.ml.decision_min_trades,
                "model_path": cfg.ml.model_path,
                "validation_report_path": cfg.ml.validation_report_path,
            },
        }
    except Exception as e:
        return {"initial_equity_usdt": 1000.0, "error": str(e)}


def configured_mode() -> str:
    try:
        import sys
        sys.path.insert(0, f"{BOT_ROOT}/src")
        from trading_bot.config import load_config

        return load_config().mode.value
    except Exception:
        return "UNKNOWN"


def api_rejections(limit: int = 100) -> list[dict]:
    try:
        conn = get_db()
        rows: list[dict[str, Any]] = []
        if table_exists(conn, "filter_rejections"):
            rows.extend(
                {
                    **dict(row),
                    "source": "filter_rejections",
                }
                for row in conn.execute("""
                    SELECT symbol, direction, strategy, confidence, filter_type, reason, created_at
                    FROM filter_rejections
                    ORDER BY id DESC
                    LIMIT ?
                """, (limit,)).fetchall()
            )
        if table_exists(conn, "ml_feature_snapshots"):
            shadow_rows = conn.execute("""
                SELECT symbol, direction, strategy, confidence, decision, reason, created_at
                FROM ml_feature_snapshots
                WHERE decision IN (
                    'SHADOW_PAPER_REJECTED_RISK',
                    'SHADOW_PAPER_REJECTED_COOLDOWN',
                    'SHADOW_PAPER_REJECTED_CONTEXT',
                    'STRATEGY_DIAGNOSTIC'
                )
                ORDER BY id DESC
                LIMIT ?
            """, (limit,)).fetchall()
            rows.extend(_shadow_rejection_row(row) for row in shadow_rows)
        conn.close()
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows[:limit]
    except Exception as e:
        return [{"error": str(e)}]


def api_rejection_stats() -> dict:
    try:
        conn = get_db()
        by_type: dict[str, int] = {}
        total = 0
        if table_exists(conn, "filter_rejections"):
            rows = conn.execute("""
                SELECT filter_type, COUNT(*) as cnt
                FROM filter_rejections GROUP BY filter_type ORDER BY cnt DESC
            """).fetchall()
            by_type.update({r[0]: r[1] for r in rows})
            total += conn.execute("SELECT COUNT(*) FROM filter_rejections").fetchone()[0]
        if table_exists(conn, "ml_feature_snapshots"):
            rows = conn.execute("""
                SELECT decision, COUNT(*) as cnt
                FROM ml_feature_snapshots
                WHERE decision IN (
                    'SHADOW_PAPER_REJECTED_RISK',
                    'SHADOW_PAPER_REJECTED_COOLDOWN',
                    'SHADOW_PAPER_REJECTED_CONTEXT',
                    'STRATEGY_DIAGNOSTIC'
                )
                GROUP BY decision
            """).fetchall()
            for decision, count in rows:
                filter_type = _shadow_rejection_filter_type(decision)
                by_type[filter_type] = by_type.get(filter_type, 0) + int(count)
                total += int(count)
        conn.close()
        return {"total": total, "by_type": dict(sorted(by_type.items(), key=lambda item: item[1], reverse=True))}
    except Exception as e:
        return {"error": str(e)}


def _shadow_rejection_filter_type(decision: str) -> str:
    if decision == "STRATEGY_DIAGNOSTIC":
        return "DIAGNOSTIC"
    if decision == "SHADOW_PAPER_REJECTED_COOLDOWN":
        return "SHADOW_COOLDOWN"
    if decision == "SHADOW_PAPER_REJECTED_RISK":
        return "SHADOW_RISK"
    if decision == "SHADOW_PAPER_REJECTED_CONTEXT":
        return "SHADOW_CONTEXT"
    return "SHADOW"


def _shadow_rejection_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    decision = str(_row_get(row, "decision", "") or "")
    return {
        "symbol": _row_get(row, "symbol", ""),
        "direction": _row_get(row, "direction", ""),
        "strategy": _row_get(row, "strategy", ""),
        "confidence": _row_get(row, "confidence", ""),
        "filter_type": _shadow_rejection_filter_type(decision),
        "reason": _row_get(row, "reason", ""),
        "created_at": _row_get(row, "created_at", ""),
        "source": "ml_feature_snapshots",
        "decision": decision,
    }


def _order_flow_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    features = _parse_metadata(_row_get(row, "features", {}))
    if features:
        return features
    metadata = _parse_metadata(_row_get(row, "metadata", {}))
    payload = metadata.get("order_flow")
    return payload if isinstance(payload, dict) else {}


def _relative_strength_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    features = _parse_metadata(_row_get(row, "features", {}))
    if features:
        return features
    metadata = _parse_metadata(_row_get(row, "metadata", {}))
    payload = metadata.get("relative_strength")
    return payload if isinstance(payload, dict) else {}


def _order_flow_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    payload = _order_flow_payload(row)
    return {
        "created_at": _row_get(row, "created_at", ""),
        "symbol": _row_get(row, "symbol", ""),
        "direction": _row_get(row, "direction", ""),
        "strategy": _row_get(row, "strategy", ""),
        "confidence": _row_get(row, "confidence", ""),
        "alignment": payload.get("alignment", "unknown"),
        "score": _to_float(payload.get("score"), 0.0) or 0.0,
        "flow_bias": payload.get("flow_bias", "NONE"),
        "liquidity_side": payload.get("liquidity_side", "none"),
        "risk_flags": payload.get("risk_flags") if isinstance(payload.get("risk_flags"), list) else [],
        "reasons": payload.get("reasons") if isinstance(payload.get("reasons"), list) else [],
        "taker_buy_ratio": _to_float(payload.get("taker_buy_ratio"), None),
        "order_book_imbalance": _to_float(payload.get("order_book_imbalance"), None),
        "aggressive_delta": _to_float(payload.get("aggressive_delta"), None),
        "open_interest_change_pct": _to_float(payload.get("open_interest_change_pct"), None),
        "funding_rate": _to_float(payload.get("funding_rate"), None),
        "distance_to_upper_liquidity_bps": _to_float(payload.get("distance_to_upper_liquidity_bps"), None),
        "distance_to_lower_liquidity_bps": _to_float(payload.get("distance_to_lower_liquidity_bps"), None),
    }


def _summarize_order_flow(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_alignment: dict[str, int] = {}
    by_strategy: dict[str, dict[str, Any]] = {}
    risk_flags: dict[str, int] = {}
    total_score = 0.0
    for row in rows:
        alignment = str(row.get("alignment") or "unknown")
        strategy = str(row.get("strategy") or "UNKNOWN")
        score = _to_float(row.get("score"), 0.0) or 0.0
        total_score += score
        _increment(by_alignment, alignment)
        strat = by_strategy.setdefault(strategy, {"total": 0, "avg_score": 0.0, "score_sum": 0.0, "by_alignment": {}})
        strat["total"] += 1
        strat["score_sum"] += score
        _increment(strat["by_alignment"], alignment)
        for flag in row.get("risk_flags") or []:
            _increment(risk_flags, str(flag))
    for value in by_strategy.values():
        value["avg_score"] = round(value["score_sum"] / value["total"], 3) if value["total"] else 0
        value.pop("score_sum", None)
    return {
        "total": len(rows),
        "avg_score": round(total_score / len(rows), 3) if rows else 0,
        "by_alignment": dict(sorted(by_alignment.items(), key=lambda item: item[1], reverse=True)),
        "by_strategy": dict(sorted(by_strategy.items())),
        "risk_flags": dict(sorted(risk_flags.items(), key=lambda item: item[1], reverse=True)),
    }


def _build_strategy_scorecard() -> dict:
    try:
        conn = get_db()
        ensure_shadow_trades_table(conn)
        trades = conn.execute("""
            SELECT id, created_at, closed_at, symbol, direction, quantity,
                   entry_price, stop_loss, take_profit, mode, status, risk_amount,
                   r_multiple, realized_pnl, metadata
            FROM trades
            ORDER BY id ASC
        """).fetchall()
        rejections = conn.execute("""
            SELECT symbol, direction, strategy, confidence, filter_type, reason, created_at
            FROM filter_rejections
            ORDER BY id ASC
        """).fetchall()
        signals = conn.execute("""
            SELECT symbol, direction, confidence, reason, created_at, metadata
            FROM signals
            ORDER BY id ASC
        """).fetchall()
        shadow_trades = conn.execute("""
            SELECT id, created_at, closed_at, symbol, direction, strategy, quantity,
                   entry_price, stop_loss, take_profit, mode, status, risk_amount,
                   r_multiple, realized_pnl, close_reason, metadata
            FROM shadow_trades
            ORDER BY id ASC
        """).fetchall()
        diagnostics = conn.execute("""
            SELECT symbol, direction, strategy, confidence, decision, reason, created_at, features, metadata
            FROM (
                SELECT symbol, direction, strategy, confidence, decision, reason, created_at, features, metadata, id
                FROM ml_feature_snapshots
                WHERE decision IN ('STRATEGY_DIAGNOSTIC', 'ORDER_FLOW_ANNOTATION', 'RELATIVE_STRENGTH_ANNOTATION')
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
        """, (SCORECARD_DIAGNOSTIC_LIMIT,)).fetchall()
        shadow_diagnostics = conn.execute("""
            SELECT symbol, direction, strategy, confidence, decision, reason, created_at, features, metadata
            FROM (
                SELECT symbol, direction, strategy, confidence, decision, reason, created_at, features, metadata, id
                FROM ml_feature_snapshots
                WHERE decision IN (
                    'SHADOW_SIGNAL',
                    'SHADOW_PAPER_REJECTED_CONTEXT',
                    'SHADOW_PAPER_REJECTED_COOLDOWN',
                    'SHADOW_PAPER_REJECTED_RISK',
                    'SHADOW_PAPER_OPENED'
                )
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
        """, (SCORECARD_DIAGNOSTIC_LIMIT,)).fetchall()
        conn.close()

        open_symbols = sorted({
            str(_row_get(t, "symbol", "") or "")
            for t in [*trades, *shadow_trades]
            if str(_row_get(t, "status", "") or "").upper() in OPEN_TRADE_STATUSES
        })
        prices = get_prices(open_symbols)
        config = api_config()
        scorecard = build_strategy_scorecard(
            list(trades),
            list(rejections),
            list(signals),
            list(shadow_trades),
            [*list(diagnostics), *list(shadow_diagnostics)],
            initial_equity=float(config.get("initial_equity_usdt", 1000.0)),
            prices=prices,
            strategy_modes=config.get("strategy_modes", {}),
        )
        scorecard["generated_at"] = datetime.now(timezone.utc).isoformat()
        scorecard["diagnostics_scope"] = {
            "limit": SCORECARD_DIAGNOSTIC_LIMIT,
            "loaded": len(diagnostics),
            "shadow_loaded": len(shadow_diagnostics),
            "note": "recent diagnostics only to keep dashboard memory bounded",
        }
        return scorecard
    except Exception as e:
        return {"error": str(e)}


def _refresh_strategy_scorecard_cache() -> dict:
    global _SCORECARD_BUILDING
    try:
        scorecard = _build_strategy_scorecard()
        if "error" not in scorecard:
            with _SCORECARD_CACHE_CONDITION:
                _SCORECARD_CACHE["data"] = scorecard
                _SCORECARD_CACHE["expires_at"] = time.monotonic() + SCORECARD_CACHE_SECONDS
        return scorecard
    finally:
        with _SCORECARD_CACHE_CONDITION:
            _SCORECARD_BUILDING = False
            _SCORECARD_CACHE_CONDITION.notify_all()


def api_strategy_scorecard() -> dict:
    global _SCORECARD_BUILDING
    with _SCORECARD_CACHE_CONDITION:
        now_monotonic = time.monotonic()
        cached = _SCORECARD_CACHE.get("data")
        expires_at = float(_SCORECARD_CACHE.get("expires_at", 0.0) or 0.0)
        if cached is not None and now_monotonic < expires_at:
            return cached
        if _SCORECARD_BUILDING:
            # Return the previous analytics snapshot while another request refreshes it.
            if cached is not None:
                return cached
            while _SCORECARD_BUILDING:
                _SCORECARD_CACHE_CONDITION.wait()
            cached = _SCORECARD_CACHE.get("data")
            if cached is not None:
                return cached
        _SCORECARD_BUILDING = True
        if cached is not None:
            threading.Thread(target=_refresh_strategy_scorecard_cache, daemon=True).start()
            return cached

    return _refresh_strategy_scorecard_cache()


def api_order_flow(limit: int = 200) -> dict:
    try:
        conn = get_db()
        if not table_exists(conn, "ml_feature_snapshots"):
            conn.close()
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "summary": _summarize_order_flow([]),
                "rows": [],
            }
        rows = conn.execute("""
            SELECT symbol, direction, strategy, confidence, decision, reason, created_at, features, metadata
            FROM ml_feature_snapshots
            WHERE decision='ORDER_FLOW_ANNOTATION'
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        payload_rows = [_order_flow_row(row) for row in rows]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": _summarize_order_flow(payload_rows),
            "rows": payload_rows,
        }
    except Exception as e:
        return {"error": str(e)}


def api_strategy_promotions() -> dict:
    scorecard = api_strategy_scorecard()
    if "error" in scorecard:
        return scorecard
    return _strategy_promotions_from_scorecard(scorecard)


def api_strategy_policy() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "auto_promotion_enabled": False,
        "policies": {
            **STRATEGY_PROMOTION_POLICIES,
            **{
                strategy: strategy_promotion_policy(strategy)
                for strategy in sorted(SQZ_GATE_COHORT_SHADOW_STRATEGIES)
            },
        },
    }


def api_strategy_allocator() -> dict:
    scorecard = api_strategy_scorecard()
    if "error" in scorecard:
        return scorecard
    return build_strategy_allocator(scorecard)


def api_weekly_research_report() -> dict:
    scorecard = api_strategy_scorecard()
    if "error" in scorecard:
        return scorecard
    allocator = build_strategy_allocator(scorecard)
    promotions = _strategy_promotions_from_scorecard(scorecard)
    order_flow = api_order_flow(limit=1000)
    ml_report = _load_ml_validation_report()
    return build_weekly_research_report(scorecard, allocator, promotions, order_flow, ml_report)


def api_production_readiness() -> dict:
    scorecard = api_strategy_scorecard()
    if "error" in scorecard:
        return scorecard
    testnet_evidence = _load_optional_json("data/testnet_lifecycle_evidence.json")
    chaos_evidence = api_chaos_readiness()
    production_unlock = _load_optional_json(_production_unlock_path())
    return build_production_readiness_report(scorecard, testnet_evidence, chaos_evidence, production_unlock)


def api_chaos_readiness() -> dict:
    evidence = _load_optional_json("data/chaos_evidence.json")
    return build_chaos_readiness_report(evidence)


def api_soak_readiness() -> dict:
    evidence = _load_optional_json("data/soak_evidence.json")
    if not evidence:
        evidence = _load_optional_json("data/testnet_lifecycle_evidence.json")
    return build_soak_readiness_report(evidence)


def api_monthly_target_plan() -> dict:
    scorecard = api_strategy_scorecard()
    if "error" in scorecard:
        return scorecard
    config = api_config()
    return build_monthly_target_report(
        scorecard,
        initial_equity=_to_float(config.get("initial_equity_usdt"), 1000.0) or 1000.0,
        target_monthly_return_pct=_to_float(os.getenv("BOT_TARGET_MONTHLY_RETURN_PCT"), 10.0) or 10.0,
        base_risk_pct=_to_float(config.get("risk", {}).get("risk_per_trade_pct"), 0.02) or 0.02,
    )


def _strategy_promotions_from_scorecard(scorecard: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    for row in scorecard.get("strategies", []):
        shadow_gate = row.get("shadow_gate") or {}
        shadow_paper = row.get("shadow_paper") or {}
        policy = row.get("promotion_policy") or apply_strategy_promotion_policy(row)
        policy_action = str(policy.get("action") or "")
        is_review_candidate = bool(policy.get("paper_review_allowed") or policy.get("live_review_allowed"))
        is_shadow_candidate = bool(shadow_gate.get("promotion_candidate"))
        is_warning_candidate = policy_action in {
            "RESEARCH_RETEST_BEFORE_PAPER",
            "WALK_FORWARD_REVIEW_REQUIRED",
            "KEEP_LIMITED_PAPER_OR_DEMOTE_REVIEW",
            "KEEP_CHAMPION_UNDER_REVIEW",
        }
        if is_review_candidate or is_shadow_candidate or is_warning_candidate:
            candidates.append({
                "strategy": row.get("strategy"),
                "recommendation": policy_action or shadow_gate.get("recommendation", "KEEP_SHADOW"),
                "current_mode": row.get("strategy_mode"),
                "policy_tier": policy.get("tier"),
                "paper_review_allowed": bool(policy.get("paper_review_allowed")),
                "live_review_allowed": bool(policy.get("live_review_allowed")),
                "human_review_required": bool(policy.get("human_review_required", True)),
                "policy_reasons": policy.get("reasons", []),
                "shadow_gate_status": shadow_gate.get("status"),
                "shadow_gate_recommendation": shadow_gate.get("recommendation", "KEEP_SHADOW"),
                "closed_shadow_trades": shadow_paper.get("closed_trades", 0),
                "open_shadow_trades": shadow_paper.get("open_trades", 0),
                "shadow_total_pnl": shadow_paper.get("total_pnl", 0),
                "shadow_winrate": shadow_paper.get("winrate", 0),
                "shadow_profit_factor": shadow_paper.get("profit_factor", 0),
                "shadow_avg_r": shadow_paper.get("avg_r", 0),
                "shadow_max_drawdown": shadow_paper.get("max_drawdown", 0),
                "last_shadow_trade_at": shadow_paper.get("last_trade_at"),
                "checks": shadow_gate.get("checks", []),
            })
    return {
        "generated_at": scorecard.get("generated_at"),
        "promotion_count": len(candidates),
        "candidates": candidates,
    }


def _load_ml_validation_report() -> dict[str, Any]:
    try:
        config = api_config()
        ml = config.get("ml") or {}
        path = Path(str(ml.get("validation_report_path") or "data/ml/walk_forward_report.json"))
        if not path.is_absolute():
            path = Path(BOT_ROOT) / path
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc), "validated": False}


def _load_optional_json(path_value: str | Path) -> dict[str, Any]:
    try:
        path = Path(path_value)
        if not path.is_absolute():
            path = Path(BOT_ROOT) / path
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        return {"error": str(exc)}


def _production_unlock_path() -> str:
    try:
        import sys
        sys.path.insert(0, f"{BOT_ROOT}/src")
        from trading_bot.config import load_config

        return load_config().safety.production_unlock_file
    except Exception:
        return "data/production_unlock.json"


ROUTES = {
    "/status": api_status,
    "/positions": api_positions,
    "/stats": api_stats,
    "/signals": api_signals,
    "/trades": api_trades,
    "/shadow-trades": api_shadow_trades,
    "/logs": api_logs,
    "/risk": api_risk_state,
    "/config": api_config,
    "/rejections": api_rejections,
    "/rejection-stats": api_rejection_stats,
    "/strategy-scorecard": api_strategy_scorecard,
    "/strategy-promotions": api_strategy_promotions,
    "/strategy-policy": api_strategy_policy,
    "/strategy-allocator": api_strategy_allocator,
    "/weekly-research-report": api_weekly_research_report,
    "/chaos-readiness": api_chaos_readiness,
    "/soak-readiness": api_soak_readiness,
    "/production-readiness": api_production_readiness,
    "/monthly-target-plan": api_monthly_target_plan,
    "/order-flow": api_order_flow,
}

POST_ROUTES = {
    "/emergency-stop": emergency_stop,
    "/clear-emergency-stop": clear_emergency_stop,
    "/restart": restart_bot,
}


class Handler(http.server.BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(CONTROL_REQUEST_TIMEOUT_SECONDS)
        super().setup()

    def _send(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-Bot-Control-Token, Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def _send_bytes(self, body: bytes, content_type: str, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-Bot-Control-Token, Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def _send_dashboard(self):
        path = Path(DASHBOARD_PATH)
        if not path.exists():
            self._send({"error": "dashboard not found", "path": DASHBOARD_PATH}, 404)
            return
        self._send_bytes(path.read_bytes(), "text/html; charset=utf-8")

    def _send_db(self):
        path = Path(DB_PATH)
        if not path.exists():
            self._send({"error": "database not found", "path": DB_PATH}, 404)
            return
        # The production SQLite file can be hundreds of megabytes. Loading it
        # with read_bytes() killed the control API through OOM during /db
        # downloads, so stream a bounded chunk at a time instead.
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-Bot-Control-Token, Content-Type")
        self.end_headers()
        try:
            with path.open("rb") as database_file:
                while chunk := database_file.read(1024 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def _authorized(self) -> bool:
        if not CONTROL_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        header_token = self.headers.get("X-Bot-Control-Token", "")
        candidates = [bearer, header_token]
        return any(secrets.compare_digest(candidate, CONTROL_TOKEN) for candidate in candidates if candidate)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-Bot-Control-Token, Content-Type")
        self.end_headers()

    def do_GET(self):
        if not self._authorized():
            self._send({"error": "unauthorized"}, 401)
            return
        path = self.path.split("?")[0]
        if path in {"/", "/dashboard", "/dashboard_v2.html"}:
            self._send_dashboard()
            return
        if path == "/db":
            self._send_db()
            return
        if path in ROUTES:
            self._send(ROUTES[path]())
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            self._send({"error": "unauthorized"}, 401)
            return
        path = self.path.split("?")[0]
        if path in POST_ROUTES:
            self._send(POST_ROUTES[path]())
        else:
            self._send({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    public_bind_without_token = HOST not in {"127.0.0.1", "localhost", "::1"} and not CONTROL_TOKEN
    if public_bind_without_token and configured_mode() == "MAINNET_LIVE":
        raise SystemExit("Refusing to expose Bot Control API without BOT_CONTROL_TOKEN in MAINNET_LIVE.")
    if public_bind_without_token and not ALLOW_UNSAFE_PUBLIC:
        raise SystemExit("Refusing to bind Bot Control API outside localhost without BOT_CONTROL_TOKEN.")
    try:
        conn = get_db()
        try:
            ensure_dashboard_indexes(conn)
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        print(f"Dashboard index setup skipped: {exc}")
    print(f"Bot v2 Control API запущен на {HOST}:{PORT}")
    start_watchdog_thread("bot-control-v2-1 running")
    threading.Thread(target=api_strategy_scorecard, daemon=True).start()
    ControlHTTPServer((HOST, PORT), Handler).serve_forever()
