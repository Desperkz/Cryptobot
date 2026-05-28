from __future__ import annotations

import asyncio

import httpx
import pytest

from trading_bot.telegram_notifier.notifier import TelegramNotifier


class FailingClient:
    def __init__(self) -> None:
        self.posts = 0

    async def post(self, *_args, **_kwargs):
        self.posts += 1
        raise httpx.ReadTimeout("telegram timeout")

    async def aclose(self) -> None:
        pass


class OkResponse:
    def raise_for_status(self) -> None:
        pass


class RecoveringClient:
    def __init__(self) -> None:
        self.posts = 0

    async def post(self, *_args, **_kwargs):
        self.posts += 1
        return OkResponse()

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_telegram_failure_enters_backoff_and_skips_immediate_retry() -> None:
    notifier = TelegramNotifier(
        "token",
        "chat",
        failure_backoff_base_sec=60,
        failure_backoff_max_sec=300,
    )
    client = FailingClient()
    notifier._client = client

    await notifier.send("first")
    await asyncio.sleep(0)
    await notifier.send("second")

    assert client.posts == 1
    assert notifier._consecutive_failures == 1
    assert notifier._next_attempt_at > 0


@pytest.mark.asyncio
async def test_telegram_success_resets_failure_backoff() -> None:
    notifier = TelegramNotifier("token", "chat")
    client = RecoveringClient()
    notifier._client = client
    notifier._consecutive_failures = 2
    notifier._next_attempt_at = 0

    await notifier.send("ok")
    await asyncio.sleep(0)

    assert client.posts == 1
    assert notifier._consecutive_failures == 0
    assert notifier._next_attempt_at == 0
