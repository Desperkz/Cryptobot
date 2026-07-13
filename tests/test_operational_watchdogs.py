from __future__ import annotations

import asyncio
from collections import deque
from decimal import Decimal

from trading_bot.bot import TradingBot, _asset_disaster_skip_reason
from trading_bot.data_provider import BinanceUSDMClient
from trading_bot.disaster_mode import DisasterConfig, DisasterDetector, DisasterLevel
from trading_bot.models import MarketMetrics
from trading_bot.operational import IncidentAlerter, SystemdNotifier


def test_incident_alerter_throttles_repeated_keys() -> None:
    alerter = IncidentAlerter(cooldown_sec=60)

    assert alerter.should_send("rate_limits", now=100.0) is True
    assert alerter.should_send("rate_limits", now=120.0) is False
    assert alerter.should_send("rate_limits", now=161.0) is True


def test_systemd_notifier_is_disabled_without_notify_socket(monkeypatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    notifier = SystemdNotifier()

    assert notifier.enabled is False
    assert notifier.ready() is False
    assert notifier.watchdog("testing") is False


def test_binance_rate_limit_counter_prunes_old_events(monkeypatch) -> None:
    client = BinanceUSDMClient.__new__(BinanceUSDMClient)
    client._rate_limit_events = deque(maxlen=100)

    times = iter([100.0, 101.0, 500.0])
    monkeypatch.setattr("trading_bot.data_provider.binance_usdm.time.monotonic", lambda: next(times))
    client._record_rate_limit_event()
    client._record_rate_limit_event()

    assert client.recent_rate_limit_count(window_sec=300) == 0


def test_asset_disaster_skip_reason_does_not_mutate_global_disaster_state() -> None:
    config = DisasterConfig(cascade_price_move_pct=5.0, cascade_funding_rate_threshold=0.003)
    detector = DisasterDetector(config)
    metrics = MarketMetrics(
        symbol="MUSDT",
        quote_volume_24h=Decimal("100000000"),
        spread_bps=Decimal("2"),
        top_book_liquidity_usdt=Decimal("1000000"),
        funding_rate=Decimal("-0.022"),
    )

    reason = _asset_disaster_skip_reason("MUSDT", metrics, -6.6, config)

    assert reason is not None
    assert "skipping symbol only" in reason
    assert detector.blocks_new_entries is False


class _HealthyBinanceProbe:
    async def ping(self) -> dict:
        return {}


class _FailedBinanceProbe:
    async def ping(self) -> dict:
        raise RuntimeError("temporary Binance outage")


def test_critical_api_outage_recovers_after_health_probe() -> None:
    detector = DisasterDetector(
        DisasterConfig(api_max_consecutive_failures=3, recovery_cooldown_sec=0),
    )
    for _ in range(3):
        detector.record_api_failure()

    assert asyncio.run(detector.check()) is DisasterLevel.CRITICAL

    bot = TradingBot.__new__(TradingBot)
    bot.binance = _HealthyBinanceProbe()
    bot.disaster = detector

    assert asyncio.run(bot._probe_disaster_recovery()) is True
    assert asyncio.run(detector.check()) is DisasterLevel.NONE
    assert detector.blocks_new_entries is False


def test_failed_disaster_health_probe_keeps_entry_blocked() -> None:
    detector = DisasterDetector(
        DisasterConfig(api_max_consecutive_failures=3, recovery_cooldown_sec=0),
    )
    for _ in range(3):
        detector.record_api_failure()
    assert asyncio.run(detector.check()) is DisasterLevel.CRITICAL

    bot = TradingBot.__new__(TradingBot)
    bot.binance = _FailedBinanceProbe()
    bot.disaster = detector

    assert asyncio.run(bot._probe_disaster_recovery()) is False
    assert asyncio.run(detector.check()) is DisasterLevel.CRITICAL
