from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class CoinGeckoClient:
    def __init__(self, api_key: str | None = None, timeout_sec: int = 15, max_retries: int = 3) -> None:
        self.api_key = api_key
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(base_url="https://api.coingecko.com/api/v3", timeout=timeout_sec)

    async def close(self) -> None:
        await self._client.aclose()

    async def top_markets(self, per_page: int = 25, vs_currency: str = "usd") -> list[dict[str, Any]]:
        headers = {"x-cg-demo-api-key": self.api_key} if self.api_key else {}
        params = {
            "vs_currency": vs_currency,
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "24h",
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get("/coins/markets", params=params, headers=headers)
                if response.status_code in {429, 500, 502, 503, 504}:
                    await asyncio.sleep(min(2**attempt, 20))
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                await asyncio.sleep(min(0.5 * (2**attempt), 5))
        raise RuntimeError(f"CoinGecko request failed after retries: {last_error}")

