from __future__ import annotations

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
    conn.commit()
    conn.close()

    monkeypatch.setattr(bot_control_v2, "DB_PATH", str(db_path))

    rows = bot_control_v2.api_rejections()
    types = [row["filter_type"] for row in rows]
    stats = bot_control_v2.api_rejection_stats()

    assert types[:2] == ["SHADOW_COOLDOWN", "SHADOW_RISK"]
    assert "RISK" in types
    assert stats["total"] == 3
    assert stats["by_type"]["SHADOW_RISK"] == 1
    assert stats["by_type"]["SHADOW_COOLDOWN"] == 1
