import asyncio
import socketserver

import httpx
import pytest

import bot_control_v2 as control
import paper_monitor_v2 as monitor


def test_price_request_recovers_after_one_timeout():
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        if request.url.path.endswith("price"):
            return httpx.Response(200, json={"price": "100"})
        return httpx.Response(200, json=[[0, "100", "102", "98", "101", "1", 59999]])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await monitor.get_market_snapshot(client, "BTCUSDT")

    snapshot = asyncio.run(run())
    assert snapshot.price == 100 and snapshot.high == 102 and snapshot.low == 98
    assert len(calls) == 3


def test_persistent_timeout_is_bounded_and_does_not_invent_price(caplog):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ConnectTimeout("", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await monitor.get_market_snapshot(client, "BTCUSDT")

    assert asyncio.run(run()) is None
    assert len(calls) == 2
    assert "ConnectTimeout" in caplog.text


def test_http_failure_is_not_treated_as_price_or_retried():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, json={"price": "100"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await monitor.get_market_snapshot(client, "BTCUSDT")

    assert asyncio.run(run()) is None
    assert len(calls) == 1


@pytest.mark.parametrize("error,reported", [(ConnectionResetError(), False), (BrokenPipeError(), False), (ValueError("bug"), True)])
def test_only_client_disconnects_skip_traceback(monkeypatch, error, reported):
    reports = []
    monkeypatch.setattr(socketserver.BaseServer, "handle_error", lambda *args: reports.append(args))
    server = control.ControlHTTPServer.__new__(control.ControlHTTPServer)
    try:
        raise error
    except Exception:
        server.handle_error(None, ("127.0.0.1", 1234))
    assert bool(reports) is reported
