from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx


logger = logging.getLogger(__name__)

RETRYABLE_BINANCE_CODES = {-1008}


class BinanceAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload: Any | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class BinanceUSDMClient:
    """Minimal async Binance USD-M Futures REST client.

    Endpoints follow Binance official USD-M docs:
    - /fapi/v1/exchangeInfo
    - /fapi/v1/order
    - /fapi/v3/positionRisk
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        recv_window_ms: int = 5000,
        timeout_sec: int = 15,
        max_retries: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window_ms = recv_window_ms
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout_sec)
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            if elapsed < 0.05:
                await asyncio.sleep(0.05 - elapsed)
            self._last_request_at = time.monotonic()

    def _sign(self, params: dict[str, Any]) -> str:
        if not self.api_secret:
            raise BinanceAPIError("Signed Binance request requires API secret.")
        query = urlencode(params, doseq=True)
        return hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        payload = {k: v for k, v in (params or {}).items() if v is not None}
        headers: dict[str, str] = {}
        if signed:
            if not self.api_key:
                raise BinanceAPIError("Signed Binance request requires API key.")
            payload.setdefault("recvWindow", self.recv_window_ms)
            payload["timestamp"] = int(time.time() * 1000)
            payload["signature"] = self._sign(payload)
            headers["X-MBX-APIKEY"] = self.api_key
        elif self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._throttle()
            try:
                response = await self._client.request(method, url, params=payload, headers=headers)
                if response.status_code in {418, 429}:
                    sleep_for = _retry_delay(attempt, response.headers.get("Retry-After"))
                    logger.warning("Binance rate limit status=%s, backing off %.2fs", response.status_code, sleep_for)
                    await asyncio.sleep(sleep_for)
                    continue
                data = _safe_json(response)
                if isinstance(data, dict) and data.get("code") in RETRYABLE_BINANCE_CODES:
                    sleep_for = _retry_delay(attempt, response.headers.get("Retry-After"))
                    logger.warning(
                        "Binance retryable code=%s status=%s, backing off %.2fs: %s",
                        data.get("code"),
                        response.status_code,
                        sleep_for,
                        data.get("msg", ""),
                    )
                    await asyncio.sleep(sleep_for)
                    continue
                if response.status_code >= 500:
                    message = str(data.get("msg", "")) if isinstance(data, dict) else response.text
                    if "Unknown error" in message:
                        raise BinanceAPIError(
                            "Binance returned 503 unknown execution status; query order state before retrying.",
                            response.status_code,
                            data,
                        )
                    await asyncio.sleep(min(0.25 * (2**attempt), 5))
                    continue
                if response.status_code >= 400:
                    message = data.get("msg") if isinstance(data, dict) else response.text
                    raise BinanceAPIError(str(message), response.status_code, data)
                return response.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                await asyncio.sleep(min(0.25 * (2**attempt), 5))
        if last_error:
            raise BinanceAPIError(f"Binance request failed after retries: {last_error}") from last_error
        raise BinanceAPIError("Binance request failed after retries.")

    async def ping(self) -> Any:
        return await self._request("GET", "/fapi/v1/ping")

    async def server_time(self) -> Any:
        return await self._request("GET", "/fapi/v1/time")

    async def exchange_info(self) -> dict[str, Any]:
        return await self._request("GET", "/fapi/v1/exchangeInfo")

    async def klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        return await self._request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit, "startTime": start_time, "endTime": end_time},
        )

    async def depth(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        return await self._request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    async def agg_trades(self, symbol: str, limit: int = 500) -> list[dict[str, Any]]:
        return await self._request("GET", "/fapi/v1/aggTrades", {"symbol": symbol, "limit": limit})

    async def ticker_24h(self, symbol: str) -> dict[str, Any]:
        return await self._request("GET", "/fapi/v1/ticker/24hr", {"symbol": symbol})

    async def ticker_price(self, symbol: str) -> dict[str, Any]:
        return await self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})

    async def book_ticker(self, symbol: str) -> dict[str, Any]:
        return await self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})

    async def funding_rate(self, symbol: str, limit: int = 1) -> list[dict[str, Any]]:
        return await self._request("GET", "/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})

    async def open_interest(self, symbol: str) -> dict[str, Any]:
        return await self._request("GET", "/fapi/v1/openInterest", {"symbol": symbol})

    async def open_interest_hist(self, symbol: str, period: str = "15m", limit: int = 2) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            "/futures/data/openInterestHist",
            {"symbol": symbol, "period": period, "limit": limit},
        )

    async def balance(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/fapi/v3/balance", signed=True)

    async def account(self) -> dict[str, Any]:
        return await self._request("GET", "/fapi/v3/account", signed=True)

    async def position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return await self._request("GET", "/fapi/v3/positionRisk", {"symbol": symbol}, signed=True)

    async def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return await self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    async def query_order(
        self,
        symbol: str,
        order_id: int | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": orig_client_order_id},
            signed=True,
        )

    async def change_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    async def change_margin_type(self, symbol: str, margin_type: str) -> dict[str, Any]:
        return await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, signed=True)

    async def new_order(self, **params: Any) -> dict[str, Any]:
        return await self._request("POST", "/fapi/v1/order", params, signed=True)

    async def modify_order(self, **params: Any) -> dict[str, Any]:
        return await self._request("PUT", "/fapi/v1/order", params, signed=True)

    async def test_order(self, **params: Any) -> dict[str, Any]:
        return await self._request("POST", "/fapi/v1/order/test", params, signed=True)

    async def cancel_order(
        self,
        symbol: str,
        order_id: int | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": orig_client_order_id},
            signed=True,
        )

    async def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        return await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    async def start_user_stream(self) -> dict[str, Any]:
        return await self._request("POST", "/fapi/v1/listenKey", signed=False)

    async def keepalive_user_stream(self, listen_key: str) -> dict[str, Any]:
        return await self._request("PUT", "/fapi/v1/listenKey", {"listenKey": listen_key}, signed=False)

    async def close_user_stream(self, listen_key: str) -> dict[str, Any]:
        return await self._request("DELETE", "/fapi/v1/listenKey", {"listenKey": listen_key}, signed=False)


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return min(max(0.5, 2**attempt), 30.0)
