from __future__ import annotations

import json
import sqlite3
import socketserver
import subprocess
import threading
import time
from pathlib import Path

import bot_control_v2


def test_control_api_uses_threaded_server() -> None:
    assert issubclass(bot_control_v2.ControlHTTPServer, socketserver.ThreadingMixIn)
    assert bot_control_v2.ControlHTTPServer.daemon_threads is True
    assert bot_control_v2.ControlHTTPServer.allow_reuse_address is True


def test_control_api_has_request_timeout() -> None:
    assert bot_control_v2.CONTROL_REQUEST_TIMEOUT_SECONDS > 0


def test_scorecard_sql_aggregates_keep_exact_signal_and_rejection_counts() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            metadata TEXT
        );
        CREATE TABLE filter_rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT,
            filter_type TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO signals(created_at, metadata) VALUES(?, ?)",
        [
            ("2026-09-01 00:00:00", json.dumps({"strategy": "TREND_PULLBACK", "strategy_mode": "shadow"})),
            ("2026-09-02 00:00:00", json.dumps({"signal_metadata": {"strategy": "TREND_PULLBACK", "strategy_mode": "shadow"}})),
            ("2026-09-02 01:00:00", "not-json"),
        ],
    )
    conn.executemany(
        "INSERT INTO filter_rejections(strategy, filter_type) VALUES(?, ?)",
        [("TREND_PULLBACK", "ORDER_FLOW"), ("TREND_PULLBACK", "ORDER_FLOW"), ("TREND_PULLBACK", "UTC")],
    )

    signals = bot_control_v2._scorecard_signal_aggregates(conn)
    rejections = bot_control_v2._scorecard_rejection_aggregates(conn)
    conn.close()

    assert signals["TREND_PULLBACK"] == {
        "signals_total": 2,
        "shadow_signals": 2,
        "last_signal_at": "2026-09-02 00:00:00",
    }
    assert signals["UNKNOWN"]["signals_total"] == 1
    assert rejections["TREND_PULLBACK"] == {
        "rejections_total": 3,
        "by_type": {"ORDER_FLOW": 2, "UTC": 1},
    }


def test_log_tail_reads_only_recent_bounded_window(tmp_path, monkeypatch) -> None:
    log_path = Path(tmp_path) / "bot.log"
    log_path.write_text(
        "OLD_MARKER\n" + ("x" * 500) + "\nHTTP Request noise\nRECENT_MARKER\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bot_control_v2, "LOG_PATH", str(log_path))
    monkeypatch.setattr(bot_control_v2, "LOG_TAIL_MAX_BYTES", 80)

    lines = bot_control_v2.api_logs(100)

    assert "RECENT_MARKER" in lines
    assert all("OLD_MARKER" not in line for line in lines)
    assert all("HTTP Request" not in line for line in lines)


def test_monthly_target_api_uses_configured_risk_and_mainnet_cap(monkeypatch) -> None:
    monkeypatch.setattr(bot_control_v2, "api_strategy_scorecard", lambda: {"summary": {}, "strategies": []})
    monkeypatch.setattr(
        bot_control_v2,
        "api_config",
        lambda: {
            "initial_equity_usdt": 1000.0,
            "risk": {
                "risk_per_trade_pct": 0.005,
                "max_mainnet_risk_per_trade_pct": 0.0025,
            },
        },
    )

    report = bot_control_v2.api_monthly_target_plan()

    assert report["base_risk_pct"] == 0.005
    assert report["target_r_month"] == 20.0
    assert report["mainnet_risk_cap_pct"] == 0.0025
    assert report["mainnet_target_r_month"] == 40.0


def test_dashboard_indexes_cover_bounded_diagnostics_query() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ml_feature_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision TEXT NOT NULL
        )
    """)

    bot_control_v2.ensure_dashboard_indexes(conn)

    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list('ml_feature_snapshots')").fetchall()
    }
    assert "idx_ml_feature_snapshots_decision_id" in indexes


def test_scorecard_serves_stale_snapshot_while_refreshing(monkeypatch) -> None:
    stale = {"generated_at": "old"}
    fresh = {"generated_at": "new"}
    started = threading.Event()
    release = threading.Event()
    original_cache = dict(bot_control_v2._SCORECARD_CACHE)
    original_building = bot_control_v2._SCORECARD_BUILDING

    def fake_build() -> dict:
        started.set()
        assert release.wait(timeout=1)
        return fresh

    monkeypatch.setattr(bot_control_v2, "_build_strategy_scorecard", fake_build)
    try:
        with bot_control_v2._SCORECARD_CACHE_CONDITION:
            bot_control_v2._SCORECARD_CACHE.update({"data": stale, "expires_at": 0.0})
            bot_control_v2._SCORECARD_BUILDING = False

        assert bot_control_v2.api_strategy_scorecard() is stale
        assert started.wait(timeout=1)
        release.set()

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with bot_control_v2._SCORECARD_CACHE_CONDITION:
                if bot_control_v2._SCORECARD_CACHE["data"] is fresh and not bot_control_v2._SCORECARD_BUILDING:
                    break
            time.sleep(0.01)
        assert bot_control_v2._SCORECARD_CACHE["data"] is fresh
        assert bot_control_v2._SCORECARD_BUILDING is False
    finally:
        release.set()
        with bot_control_v2._SCORECARD_CACHE_CONDITION:
            bot_control_v2._SCORECARD_CACHE.clear()
            bot_control_v2._SCORECARD_CACHE.update(original_cache)
            bot_control_v2._SCORECARD_BUILDING = original_building


def test_service_status_uses_timeout_and_cache(monkeypatch) -> None:
    bot_control_v2._SERVICE_STATUS_CACHE.clear()
    calls = []

    def fake_run(cmd, capture_output, text, timeout):
        calls.append((cmd, capture_output, text, timeout))

        class Result:
            stdout = "active\n"

        return Result()

    monkeypatch.setattr(bot_control_v2.subprocess, "run", fake_run)
    monkeypatch.setattr(bot_control_v2, "SERVICE_STATUS_CACHE_SECONDS", 10)
    monkeypatch.setattr(bot_control_v2, "SERVICE_STATUS_TIMEOUT_SECONDS", 0.25)

    assert bot_control_v2.service_status("trading-bot-v2-1") == "active"
    assert bot_control_v2.service_status("trading-bot-v2-1") == "active"
    assert len(calls) == 1
    assert calls[0][0] == ["systemctl", "is-active", "trading-bot-v2-1"]
    assert calls[0][3] == 0.25


def test_service_status_timeout_returns_cached_value(monkeypatch) -> None:
    bot_control_v2._SERVICE_STATUS_CACHE.clear()
    bot_control_v2._SERVICE_STATUS_CACHE["trading-bot-v2-1"] = (0.0, "active")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["systemctl"], timeout=0.1)

    monkeypatch.setattr(bot_control_v2.subprocess, "run", fake_run)
    monkeypatch.setattr(bot_control_v2, "time", type("FakeTime", (), {"monotonic": staticmethod(lambda: 100.0)}))

    assert bot_control_v2.service_status("trading-bot-v2-1") == "active"


def test_rejections_include_shadow_paper_risk_and_cooldown(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "bot.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE filter_rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            symbol TEXT,
            direction TEXT,
            strategy TEXT,
            confidence TEXT,
            filter_type TEXT,
            reason TEXT
        );
        CREATE TABLE ml_feature_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            symbol TEXT,
            direction TEXT,
            strategy TEXT,
            confidence TEXT,
            decision TEXT,
            reason TEXT,
            features TEXT,
            metadata TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO filter_rejections(symbol, direction, strategy, confidence, filter_type, reason, created_at)
        VALUES('PAXGUSDT', 'SHORT', 'SQUEEZE_BREAKOUT', '0.88', 'RISK', 'margin cap', '2026-05-14 21:59:03')
        """
    )
    conn.execute(
        """
        INSERT INTO ml_feature_snapshots(symbol, direction, strategy, confidence, decision, reason, features, metadata, created_at)
        VALUES('ETCUSDT', 'SHORT', 'RANGE_GRID', '0.54', 'SHADOW_PAPER_REJECTED_RISK',
               'Reward/risk 0.83 is below minimum 1.0.', '{}', '{}', '2026-05-17 05:37:28')
        """
    )
    conn.execute(
        """
        INSERT INTO ml_feature_snapshots(symbol, direction, strategy, confidence, decision, reason, features, metadata, created_at)
        VALUES('MUSDT', 'SHORT', 'VWAP_REVERSION_WATCH', '0.63', 'SHADOW_PAPER_REJECTED_COOLDOWN',
               'MUSDT VWAP_REVERSION_WATCH re-entry cooldown is active.', '{}', '{}', '2026-05-17 05:40:00')
        """
    )
    conn.execute(
        """
        INSERT INTO ml_feature_snapshots(symbol, direction, strategy, confidence, decision, reason, features, metadata, created_at)
        VALUES('ADAUSDT', 'SHORT', 'LIQUIDITY_SWEEP_REVERSAL', '0.64', 'SHADOW_PAPER_REJECTED_CONTEXT',
               'LSR shadow blocked: adverse liquidity remains nearby after the sweep.', '{}', '{}', '2026-05-17 05:41:00')
        """
    )
    conn.execute(
        """
        INSERT INTO ml_feature_snapshots(symbol, direction, strategy, confidence, decision, reason, features, metadata, created_at)
        VALUES('BTCUSDT', 'SHORT', 'VWAP_REVERSION_WATCH', '0.0', 'STRATEGY_DIAGNOSTIC',
               'below_min_deviation_or_rsi', '{}', '{}', '2026-05-17 05:42:00')
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(bot_control_v2, "DB_PATH", str(db_path))

    rows = bot_control_v2.api_rejections()
    types = [row["filter_type"] for row in rows]
    stats = bot_control_v2.api_rejection_stats()

    assert types[:4] == ["DIAGNOSTIC", "SHADOW_CONTEXT", "SHADOW_COOLDOWN", "SHADOW_RISK"]
    assert "RISK" in types
    assert stats["total"] == 5
    assert stats["by_type"]["DIAGNOSTIC"] == 1
    assert stats["by_type"]["SHADOW_RISK"] == 1
    assert stats["by_type"]["SHADOW_COOLDOWN"] == 1
    assert stats["by_type"]["SHADOW_CONTEXT"] == 1


def test_trades_endpoint_exposes_execution_cost_breakdown(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "bot.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            symbol TEXT,
            direction TEXT,
            entry_price TEXT,
            stop_loss TEXT,
            take_profit TEXT,
            quantity TEXT,
            status TEXT,
            realized_pnl TEXT,
            r_multiple TEXT,
            metadata TEXT
        );
        """
    )
    metadata = {
        "paper_execution_summary": {
            "gross_pnl": "12.5",
            "fees": "0.8",
            "slippage_cost": "0.4",
            "funding_cost": "0.1",
            "net_pnl": "11.2",
        }
    }
    conn.execute(
        """
        INSERT INTO trades(symbol, direction, entry_price, stop_loss, take_profit, quantity, status,
                           realized_pnl, r_multiple, metadata, created_at)
        VALUES('BTCUSDT', 'LONG', '100', '95', '110', '1', 'CLOSED', '11.2', '1.12', ?, '2026-05-18 10:00:00')
        """,
        (json.dumps(metadata),),
    )
    conn.execute(
        """
        INSERT INTO trades(symbol, direction, entry_price, stop_loss, take_profit, quantity, status,
                           realized_pnl, r_multiple, metadata, created_at)
        VALUES('ETHUSDT', 'SHORT', '100', '105', '90', '1', 'CLOSED', '8', '0.8', '{}', '2026-05-18 10:05:00')
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(bot_control_v2, "DB_PATH", str(db_path))

    rows = bot_control_v2.api_trades()

    assert rows[1]["execution_costs"]["realistic_execution"] is True
    assert rows[1]["execution_costs"]["total_cost"] == 1.3
    assert rows[0]["execution_costs"]["realistic_execution"] is False
    assert rows[0]["execution_costs"]["pre_p5_ideal_fill"] is True


def test_order_flow_endpoint_summarizes_recent_annotations(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "bot.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE ml_feature_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            symbol TEXT,
            direction TEXT,
            strategy TEXT,
            confidence TEXT,
            decision TEXT,
            reason TEXT,
            features TEXT,
            metadata TEXT
        );
        """
    )
    aligned = {
        "alignment": "aligned",
        "score": 0.76,
        "flow_bias": "LONG",
        "liquidity_side": "upside",
        "risk_flags": [],
        "reasons": ["taker_flow_aligned"],
        "taker_buy_ratio": 0.61,
    }
    against = {
        "alignment": "against",
        "score": 0.21,
        "flow_bias": "SHORT",
        "liquidity_side": "downside",
        "risk_flags": ["liquidation_cascade"],
        "reasons": [],
        "open_interest_change_pct": -0.35,
    }
    conn.execute(
        """
        INSERT INTO ml_feature_snapshots(symbol, direction, strategy, confidence, decision, reason, features, metadata, created_at)
        VALUES('BTCUSDT', 'LONG', 'SQUEEZE_BREAKOUT', '0.8', 'ORDER_FLOW_ANNOTATION',
               'alignment=aligned; score=0.76', ?, '{}', '2026-05-17 10:01:00')
        """,
        (json.dumps(aligned),),
    )
    conn.execute(
        """
        INSERT INTO ml_feature_snapshots(symbol, direction, strategy, confidence, decision, reason, features, metadata, created_at)
        VALUES('ETHUSDT', 'LONG', 'SQUEEZE_BREAKOUT', '0.7', 'ORDER_FLOW_ANNOTATION',
               'alignment=against; score=0.21', ?, '{}', '2026-05-17 10:02:00')
        """,
        (json.dumps(against),),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(bot_control_v2, "DB_PATH", str(db_path))

    data = bot_control_v2.api_order_flow()

    assert data["summary"]["total"] == 2
    assert data["summary"]["avg_score"] == 0.485
    assert data["summary"]["by_alignment"] == {"against": 1, "aligned": 1}
    assert data["summary"]["risk_flags"] == {"liquidation_cascade": 1}
    assert data["rows"][0]["symbol"] == "ETHUSDT"
    assert data["rows"][0]["alignment"] == "against"
