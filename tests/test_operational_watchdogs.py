from __future__ import annotations

from collections import deque

from trading_bot.data_provider import BinanceUSDMClient
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
