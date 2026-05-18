from __future__ import annotations

import json
import sqlite3
import socketserver

import bot_control_v2


def test_control_api_uses_threaded_server() -> None:
    assert issubclass(bot_control_v2.ControlHTTPServer, socketserver.ThreadingMixIn)
    assert bot_control_v2.ControlHTTPServer.daemon_threads is True
    assert bot_control_v2.ControlHTTPServer.allow_reuse_address is True


def test_control_api_has_request_timeout() -> None:
    assert bot_control_v2.CONTROL_REQUEST_TIMEOUT_SECONDS > 0


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
    conn.commit()
    conn.close()

    monkeypatch.setattr(bot_control_v2, "DB_PATH", str(db_path))

    rows = bot_control_v2.api_rejections()
    types = [row["filter_type"] for row in rows]
    stats = bot_control_v2.api_rejection_stats()

    assert types[:3] == ["SHADOW_CONTEXT", "SHADOW_COOLDOWN", "SHADOW_RISK"]
    assert "RISK" in types
    assert stats["total"] == 4
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
