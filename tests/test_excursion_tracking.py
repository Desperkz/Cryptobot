"""Тесты MFE/MAE-трекинга в paper_monitor_v2 и счётчика вердиктов детектора."""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path


def _load_monitor():
    spec = importlib.util.spec_from_file_location(
        "paper_monitor_v2", Path(__file__).parent.parent / "paper_monitor_v2.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["paper_monitor_v2"] = module
    spec.loader.exec_module(module)
    return module


def test_update_excursions_tracks_mfe_and_mae_for_long() -> None:
    pm = _load_monitor()
    metadata = {"original_stop_loss": "95"}
    snap = pm.MarketSnapshot(
        price=Decimal("104"), high=Decimal("106"), low=Decimal("98"),
        candle_open_time=None, candle_close_time=None,
    )
    changed = pm._update_excursions("LONG", Decimal("100"), snap, metadata)
    # риск = 5: high 106 -> MFE 1.2R, low 98 -> MAE 0.4R
    assert changed is True
    assert metadata["mfe_r"] == "1.200"
    assert metadata["mae_r"] == "0.400"

    # цена сходила ниже, но не выше — MFE не меняется, MAE растёт
    snap2 = pm.MarketSnapshot(
        price=Decimal("97"), high=Decimal("104"), low=Decimal("96"),
        candle_open_time=None, candle_close_time=None,
    )
    pm._update_excursions("LONG", Decimal("100"), snap2, metadata)
    assert metadata["mfe_r"] == "1.200"
    assert metadata["mae_r"] == "0.800"


def test_update_excursions_short_direction_and_persist_step() -> None:
    pm = _load_monitor()
    metadata = {"original_stop_loss": "105"}
    snap = pm.MarketSnapshot(
        price=Decimal("99"), high=Decimal("101"), low=Decimal("98"),
        candle_open_time=None, candle_close_time=None,
    )
    changed = pm._update_excursions("SHORT", Decimal("100"), snap, metadata)
    assert changed is True
    assert metadata["mfe_r"] == "0.400"  # (100-98)/5
    assert metadata["mae_r"] == "0.200"  # (101-100)/5

    # микроскопический прирост (< шага персиста 0.1R) значение обновляет,
    # но запись в БД не запрашивает
    snap_small = pm.MarketSnapshot(
        price=Decimal("97.9"), high=Decimal("101"), low=Decimal("97.9"),
        candle_open_time=None, candle_close_time=None,
    )
    changed_small = pm._update_excursions("SHORT", Decimal("100"), snap_small, metadata)
    assert changed_small is False
    assert metadata["mfe_r"] == "0.420"


def test_update_excursions_without_original_stop_is_noop() -> None:
    pm = _load_monitor()
    snap = pm.MarketSnapshot(
        price=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
        candle_open_time=None, candle_close_time=None,
    )
    assert pm._update_excursions("LONG", Decimal("100"), snap, {}) is False
