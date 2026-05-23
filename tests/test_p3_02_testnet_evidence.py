from __future__ import annotations

from scripts.p3_02_testnet_integration import _lifecycle_evidence_from_report


def test_lifecycle_evidence_maps_successful_order_steps() -> None:
    report = {
        "ok": True,
        "execute": True,
        "symbol": "XRPUSDT",
        "base_url": "https://demo-fapi.binance.com",
        "finished_at": "2026-01-01T00:00:00+00:00",
        "steps": [
            {"name": "entry_market", "status": "FILLED", "executed_qty": "10"},
            {
                "name": "protective_orders",
                "stop": {"client_order_id": "p3-sl", "status": "NEW"},
                "take_profit": {"client_order_id": "p3-tp", "status": "NEW"},
            },
            {"name": "cancel_limit_order", "queried_status": "CANCELED"},
            {
                "name": "restart_recovery",
                "position_amt": "10",
                "open_order_client_ids": ["p3-sl", "p3-tp"],
                "missing_protection": [],
            },
            {"name": "partial_fill", "status": "PARTIALLY_FILLED"},
            {"name": "cleanup", "result": "closed position and canceled test orders"},
            {"name": "final_clean_check", "position_amt": "0", "remaining_test_orders": 0},
        ],
    }

    evidence = _lifecycle_evidence_from_report(report)

    assert evidence["ok"] is True
    assert evidence["lifecycle"] == {
        "entry": True,
        "stop_loss": True,
        "take_profit": True,
        "cancel": True,
        "partial_fill": True,
        "restart_recovery": True,
    }
    assert evidence["duplicate_orders"] == 0
    assert evidence["unprotected_positions"] == 0


def test_lifecycle_evidence_stays_blocked_without_execute_or_partial_fill() -> None:
    report = {
        "ok": True,
        "execute": False,
        "symbol": "XRPUSDT",
        "note": "Preflight only.",
        "steps": [
            {"name": "ping", "result": {}},
            {"name": "preflight_account_clean", "result": "ok"},
        ],
    }

    evidence = _lifecycle_evidence_from_report(report)

    assert evidence["ok"] is False
    assert evidence["lifecycle"]["entry"] is False
    assert evidence["lifecycle"]["partial_fill"] is False
    assert evidence["unprotected_positions"] == 1
    assert any("P3_TESTNET_EXECUTE" in reason for reason in evidence["blocked_reasons"])
