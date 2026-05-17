from __future__ import annotations

from types import SimpleNamespace

from trading_bot.config import active_systemd_services


def test_active_systemd_services_returns_only_active(monkeypatch) -> None:
    monkeypatch.setattr("trading_bot.config.shutil.which", lambda name: "/bin/systemctl")

    def fake_run(cmd, **kwargs):
        service = cmd[-1]
        stdout = "active\n" if service == "trading-bot-v2" else "inactive\n"
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr("trading_bot.config.subprocess.run", fake_run)

    assert active_systemd_services(["trading-bot", "trading-bot-v2"]) == ["trading-bot-v2"]


def test_active_systemd_services_no_systemctl(monkeypatch) -> None:
    monkeypatch.setattr("trading_bot.config.shutil.which", lambda name: None)

    assert active_systemd_services(["trading-bot-v2"]) == []
