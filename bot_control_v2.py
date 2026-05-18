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
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
SHADOW_GATE_DEFAULTS = {
    "min_closed_trades": int(os.getenv("SHADOW_GATE_MIN_CLOSED_TRADES", "30")),
    "min_sample_age_days": float(os.getenv("SHADOW_GATE_MIN_SAMPLE_AGE_DAYS", "3")),
    "min_winrate": float(os.getenv("SHADOW_GATE_MIN_WINRATE", "45")),
    "min_profit_factor": float(os.getenv("SHADOW_GATE_MIN_PROFIT_FACTOR", "1.25")),
    "min_avg_r": float(os.getenv("SHADOW_GATE_MIN_AVG_R", "0")),
    "max_drawdown": float(os.getenv("SHADOW_GATE_MAX_DRAWDOWN", "-10")),
}
ALLOCATOR_WEIGHT_CAPS = {
    "CORE_CANDIDATE": float(os.getenv("ALLOCATOR_CORE_CAP_PCT", "60")),
    "CHAMPION_WATCH": float(os.getenv("ALLOCATOR_CHAMPION_WATCH_CAP_PCT", "45")),
    "KEEP_LIMITED_PAPER": float(os.getenv("ALLOCATOR_LIMITED_PAPER_CAP_PCT", "30")),
    "PROMOTION_REVIEW": float(os.getenv("ALLOCATOR_PROMOTION_REVIEW_CAP_PCT", "10")),
    "WATCH_SHADOW": float(os.getenv("ALLOCATOR_SHADOW_WATCH_CAP_PCT", "5")),
}


class ControlHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


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


def _truthy_metadata(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _increment(mapping: dict[str, int], key: str, amount: int = 1) -> None:
    mapping[key] = mapping.get(key, 0) + amount


def _strategy_from_trade(row: Any) -> str:
    metadata = _parse_metadata(_row_get(row, "metadata", {}))
    signal_metadata = metadata.get("signal_metadata")
    if isinstance(signal_metadata, dict):
        strategy = signal_metadata.get("strategy")
        if strategy:
            return str(strategy)
    strategy = metadata.get("strategy")
    return str(strategy or "UNKNOWN")


def _strategy_mode_from_row(row: Any, strategy_modes: dict[str, str] | None = None) -> str:
    strategy_modes = strategy_modes or {}
    metadata = _parse_metadata(_row_get(row, "metadata", {}))
    signal_metadata = metadata.get("signal_metadata")
    mode = None
    if isinstance(signal_metadata, dict):
        mode = signal_metadata.get("strategy_mode")
    mode = mode or metadata.get("strategy_mode")
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
    buckets: dict[str, dict[str, Any]] = {}

    def bucket(strategy: str) -> dict[str, Any]:
        return buckets.setdefault(strategy, {
            "closed": [],
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
        strategy = _strategy_from_trade(signal)
        item = bucket(strategy)
        item["signals_total"] += 1
        if _strategy_mode_from_row(signal, strategy_modes) == "shadow":
            item["shadow_signals"] += 1
        confidence = _to_float(_row_get(signal, "confidence"), None)
        if confidence is not None:
            item["signal_confidences"].append(confidence)
        metadata = _parse_metadata(_row_get(signal, "metadata", {}))
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
        "open_trades": 0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "shadow_closed_trades": 0,
        "shadow_open_trades": 0,
        "shadow_realized_pnl": 0.0,
        "shadow_unrealized_pnl": 0.0,
        "rejections_total": 0,
    }

    for strategy, item in buckets.items():
        closed = item["closed"]
        open_trades = item["open"]
        shadow_closed = item["shadow_closed"]
        shadow_open = item["shadow_open"]
        pnl_values = [_to_float(_row_get(t, "realized_pnl"), 0.0) or 0.0 for t in closed]
        r_values = [_to_float(_row_get(t, "r_multiple"), 0.0) or 0.0 for t in closed]
        shadow_pnl_values = [_to_float(_row_get(t, "realized_pnl"), 0.0) or 0.0 for t in shadow_closed]
        shadow_r_values = [_to_float(_row_get(t, "r_multiple"), 0.0) or 0.0 for t in shadow_closed]
        gross_profit = sum(v for v in pnl_values if v > 0)
        gross_loss = abs(sum(v for v in pnl_values if v < 0))
        closed_count = len(closed)
        wins = sum(1 for v in pnl_values if v > 0)
        losses = sum(1 for v in pnl_values if v < 0)
        realized_pnl = sum(pnl_values)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit
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
        shadow_max_dd_pct, shadow_max_dd_usdt = _max_drawdown(shadow_closed, initial_equity)
        clusters = _cluster_metrics(closed, SCORECARD_CLUSTER_WINDOW_MINUTES)

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
        }
        shadow_gate = evaluate_shadow_gate(shadow_metrics)

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
            },
            "shadow_paper": shadow_metrics,
            "shadow_gate": shadow_gate,
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
        row["gate"] = evaluate_strategy_gate(row, gate_thresholds)
        strategies.append(row)
        summary["closed_trades"] += closed_count
        summary["closed_trade_clusters"] += clusters["closed_clusters"]
        summary["open_trades"] += len(open_trades)
        summary["realized_pnl"] += realized_pnl
        summary["unrealized_pnl"] += unrealized_pnl
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
        score = _allocator_score(metrics)
        reasons: list[str] = []

        if mode in {"paper", "live"}:
            if closed <= 0:
                action = "COLLECT_PAPER_EVIDENCE"
                cap = 0.0
                reasons.append("no closed paper clusters yet")
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
        allocations.append({
            "strategy": strategy,
            "mode": mode,
            "evidence_source": source,
            "action": action,
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


def service_status(name: str) -> str:
    result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
    return result.stdout.strip() or "unknown"


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
        return [dict(r) for r in rows]
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
        return [dict(r) for r in rows]
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
                    'SHADOW_PAPER_REJECTED_CONTEXT'
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
                    'SHADOW_PAPER_REJECTED_CONTEXT'
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


def api_strategy_scorecard() -> dict:
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
            FROM ml_feature_snapshots
            WHERE decision IN ('STRATEGY_DIAGNOSTIC', 'ORDER_FLOW_ANNOTATION')
            ORDER BY id ASC
        """).fetchall()
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
            list(diagnostics),
            initial_equity=float(config.get("initial_equity_usdt", 1000.0)),
            prices=prices,
            strategy_modes=config.get("strategy_modes", {}),
        )
        scorecard["generated_at"] = datetime.now(timezone.utc).isoformat()
        return scorecard
    except Exception as e:
        return {"error": str(e)}


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
    candidates = []
    for row in scorecard.get("strategies", []):
        shadow_gate = row.get("shadow_gate") or {}
        shadow_paper = row.get("shadow_paper") or {}
        if shadow_gate.get("promotion_candidate"):
            candidates.append({
                "strategy": row.get("strategy"),
                "recommendation": shadow_gate.get("recommendation", "PROMOTE_TO_PAPER"),
                "current_mode": row.get("strategy_mode"),
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


def api_strategy_allocator() -> dict:
    scorecard = api_strategy_scorecard()
    if "error" in scorecard:
        return scorecard
    return build_strategy_allocator(scorecard)


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
    "/strategy-allocator": api_strategy_allocator,
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
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-Bot-Control-Token, Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        self._send_bytes(path.read_bytes(), "application/octet-stream")

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
    print(f"Bot v2 Control API запущен на {HOST}:{PORT}")
    ControlHTTPServer((HOST, PORT), Handler).serve_forever()
