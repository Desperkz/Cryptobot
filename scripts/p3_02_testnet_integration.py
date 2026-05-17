from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import httpx

from trading_bot.config import load_config
from trading_bot.data_provider import BinanceAPIError, BinanceUSDMClient
from trading_bot.models import SymbolFilters, to_decimal
from trading_bot.order_manager.formatting import format_decimal


DEFAULT_SYMBOL = "XRPUSDT"
DEFAULT_NOTIONAL_USDT = Decimal("20")


def main() -> None:
    report = asyncio.run(run())
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    if not report.get("ok"):
        raise SystemExit(1)


async def run() -> dict[str, Any]:
    config = load_config(PROJECT_ROOT / "config.yaml", PROJECT_ROOT / ".env")
    symbol = os.getenv("P3_TESTNET_SYMBOL", DEFAULT_SYMBOL).upper()
    execute = os.getenv("P3_TESTNET_EXECUTE") == "1"
    notional = Decimal(os.getenv("P3_TESTNET_NOTIONAL_USDT", str(DEFAULT_NOTIONAL_USDT)))
    prefix = f"codexp3{int(time.time()) % 1_000_000_000}"
    report: dict[str, Any] = {
        "ok": False,
        "execute": execute,
        "symbol": symbol,
        "prefix": prefix,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": [],
    }

    if not config.secrets.binance_testnet_api_key or not config.secrets.binance_testnet_api_secret:
        report["error"] = "Missing BINANCE_TESTNET_API_KEY or BINANCE_TESTNET_API_SECRET in .env"
        return report

    base_url = os.getenv("P3_TESTNET_BASE_URL", config.exchange.testnet_base_url)
    report["base_url"] = base_url
    report["vps_public_ip"] = await _public_ip()

    client = BinanceUSDMClient(
        base_url=base_url,
        api_key=config.secrets.binance_testnet_api_key,
        api_secret=config.secrets.binance_testnet_api_secret,
        recv_window_ms=config.exchange.recv_window_ms,
        timeout_sec=config.exchange.request_timeout_sec,
        max_retries=config.exchange.max_retries,
    )
    opened_position = False
    filters: SymbolFilters | None = None
    executed_qty = Decimal("0")
    client_ids: list[str] = []
    try:
        report["steps"].append({"name": "ping", "result": await client.ping()})
        report["steps"].append({"name": "server_time", "result": await client.server_time()})
        signed_probe = await _signed_probe(
            base_url,
            config.secrets.binance_testnet_api_key,
            config.secrets.binance_testnet_api_secret,
            config.exchange.recv_window_ms,
            symbol,
        )
        report["steps"].append({"name": "signed_endpoint_probe", "result": signed_probe})
        balances = signed_probe.get("balance")
        if not balances:
            error = signed_probe.get("errors", {}).get("/fapi/v3/balance") or signed_probe.get("errors", {}).get(
                "/fapi/v2/balance"
            )
            report["signed_request_error"] = {
                "status_code": error.get("status_code") if isinstance(error, dict) else None,
                "payload": error.get("payload") if isinstance(error, dict) else error,
                "hint": (
                    "Signed testnet request failed. Check that the key belongs to the same USD-M Futures "
                    "demo/testnet endpoint, Futures trading permission is enabled, and any IP whitelist "
                    "includes the VPS public IP."
                ),
            }
            return report
        usdt_balance = next((item for item in balances if item.get("asset") == "USDT"), {})
        report["steps"].append(
            {
                "name": "balance",
                "available_usdt": usdt_balance.get("availableBalance") or usdt_balance.get("balance"),
            }
        )

        exchange_info = await client.exchange_info()
        symbol_info = _symbol_info(exchange_info, symbol)
        filters = SymbolFilters.from_exchange_symbol(symbol_info)
        book = await client.book_ticker(symbol)
        price = to_decimal(book.get("askPrice") or book.get("bidPrice"))
        qty = _quantity_for_notional(notional, price, filters)
        report["steps"].append(
            {
                "name": "preflight_market",
                "price": str(price),
                "quantity": str(qty),
                "notional": str(qty * price),
                "min_notional": str(filters.min_notional),
            }
        )

        await _assert_clean_symbol(
            client,
            symbol,
            base_url,
            config.secrets.binance_testnet_api_key,
            config.secrets.binance_testnet_api_secret,
            config.exchange.recv_window_ms,
        )
        report["steps"].append({"name": "preflight_account_clean", "result": "ok"})
        if not execute:
            report["ok"] = True
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            report["note"] = "Preflight only. Set P3_TESTNET_EXECUTE=1 to place demo/testnet orders."
            return report

        entry_id = f"{prefix}-entry"
        stop_id = f"{prefix}-sl"
        tp_id = f"{prefix}-tp"
        cancel_id = f"{prefix}-cancel"
        close_id = f"{prefix}-close"
        client_ids.extend([entry_id, stop_id, tp_id, cancel_id, close_id])

        entry = await client.new_order(
            symbol=symbol,
            side="BUY",
            type="MARKET",
            quantity=format_decimal(qty),
            newOrderRespType="RESULT",
            newClientOrderId=entry_id,
        )
        executed_qty = _executed_quantity(entry)
        if executed_qty <= 0:
            raise RuntimeError(f"Entry returned zero executed quantity: {entry}")
        opened_position = True
        report["steps"].append(
            {
                "name": "entry_market",
                "client_order_id": entry_id,
                "status": entry.get("status"),
                "executed_qty": str(executed_qty),
            }
        )

        entry_state = await client.query_order(symbol, orig_client_order_id=entry_id)
        report["steps"].append({"name": "entry_query_by_client_id", "status": entry_state.get("status")})

        stop_price = filters.round_price(price * Decimal("0.95"))
        tp_price = filters.round_price(price * Decimal("1.05"))
        stop_order = await client.new_order(
            symbol=symbol,
            side="SELL",
            type="STOP_MARKET",
            stopPrice=format_decimal(stop_price),
            closePosition="true",
            workingType="MARK_PRICE",
            newClientOrderId=stop_id,
        )
        tp_order = await client.new_order(
            symbol=symbol,
            side="SELL",
            type="TAKE_PROFIT_MARKET",
            stopPrice=format_decimal(tp_price),
            quantity=format_decimal(executed_qty),
            reduceOnly="true",
            workingType="MARK_PRICE",
            newClientOrderId=tp_id,
        )
        report["steps"].append(
            {
                "name": "protective_orders",
                "stop": {"client_order_id": stop_id, "status": stop_order.get("status"), "price": str(stop_price)},
                "take_profit": {"client_order_id": tp_id, "status": tp_order.get("status"), "price": str(tp_price)},
            }
        )

        cancel_price = filters.round_price(price * Decimal("0.50"))
        cancel_order = await client.new_order(
            symbol=symbol,
            side="BUY",
            type="LIMIT",
            timeInForce="GTC",
            quantity=format_decimal(executed_qty),
            price=format_decimal(cancel_price),
            newClientOrderId=cancel_id,
        )
        canceled = await client.cancel_order(symbol, orig_client_order_id=cancel_id)
        canceled_state = await client.query_order(symbol, orig_client_order_id=cancel_id)
        report["steps"].append(
            {
                "name": "cancel_limit_order",
                "submitted_status": cancel_order.get("status"),
                "cancel_status": canceled.get("status"),
                "queried_status": canceled_state.get("status"),
            }
        )
        if canceled_state.get("status") != "CANCELED":
            raise RuntimeError(f"Cancel verification failed: {canceled_state}")

        await client.close()
        client = BinanceUSDMClient(
            base_url=base_url,
            api_key=config.secrets.binance_testnet_api_key,
            api_secret=config.secrets.binance_testnet_api_secret,
            recv_window_ms=config.exchange.recv_window_ms,
            timeout_sec=config.exchange.request_timeout_sec,
            max_retries=config.exchange.max_retries,
        )
        recovered_position = await _position_amount(
            client,
            symbol,
            base_url,
            config.secrets.binance_testnet_api_key,
            config.secrets.binance_testnet_api_secret,
            config.exchange.recv_window_ms,
        )
        recovered_orders = await client.open_orders(symbol)
        recovered_client_ids = {str(order.get("clientOrderId")) for order in recovered_orders}
        missing = {stop_id, tp_id} - recovered_client_ids
        report["steps"].append(
            {
                "name": "restart_recovery",
                "position_amt": str(recovered_position),
                "open_order_client_ids": sorted(recovered_client_ids),
                "missing_protection": sorted(missing),
            }
        )
        if recovered_position <= 0 or missing:
            raise RuntimeError("Restart recovery failed to find test position and protective orders.")

        await _cleanup(
            client,
            symbol,
            client_ids,
            filters,
            base_url,
            config.secrets.binance_testnet_api_key,
            config.secrets.binance_testnet_api_secret,
            config.exchange.recv_window_ms,
        )
        opened_position = False
        report["steps"].append({"name": "cleanup", "result": "closed position and canceled test orders"})
        final_position = await _position_amount(
            client,
            symbol,
            base_url,
            config.secrets.binance_testnet_api_key,
            config.secrets.binance_testnet_api_secret,
            config.exchange.recv_window_ms,
        )
        remaining_orders = [
            order
            for order in await client.open_orders(symbol)
            if str(order.get("clientOrderId", "")).startswith(prefix)
        ]
        report["steps"].append(
            {
                "name": "final_clean_check",
                "position_amt": str(final_position),
                "remaining_test_orders": len(remaining_orders),
            }
        )
        if final_position != 0 or remaining_orders:
            raise RuntimeError("Final cleanup verification failed.")

        report["ok"] = True
        return report
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        try:
            if execute and filters is not None and (opened_position or client_ids):
                await _cleanup(
                    client,
                    symbol,
                    client_ids,
                    filters,
                    base_url,
                    config.secrets.binance_testnet_api_key,
                    config.secrets.binance_testnet_api_secret,
                    config.exchange.recv_window_ms,
                )
        except Exception as cleanup_exc:
            report.setdefault("cleanup_errors", []).append(str(cleanup_exc))
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report_path = PROJECT_ROOT / "data" / "p3_02_testnet_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        await client.close()


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _symbol_info(exchange_info: dict[str, Any], symbol: str) -> dict[str, Any]:
    for item in exchange_info.get("symbols", []):
        if item.get("symbol") == symbol:
            if item.get("status") != "TRADING":
                raise RuntimeError(f"{symbol} is not TRADING on testnet.")
            return item
    raise RuntimeError(f"{symbol} not found on testnet exchangeInfo.")


async def _signed_probe(
    base_url: str,
    api_key: str,
    api_secret: str,
    recv_window_ms: int,
    symbol: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": [], "errors": {}}
    async with httpx.AsyncClient(timeout=15) as client:
        for path, params in [
            ("/fapi/v3/balance", {}),
            ("/fapi/v2/balance", {}),
            ("/fapi/v3/positionRisk", {"symbol": symbol}),
            ("/fapi/v2/positionRisk", {"symbol": symbol}),
        ]:
            try:
                payload = await _signed_get(client, base_url, path, api_key, api_secret, recv_window_ms, params)
                result["ok"].append(path)
                if path.endswith("/balance") and "balance" not in result:
                    result["balance"] = payload
                if path.endswith("/positionRisk") and "position_risk" not in result:
                    result["position_risk"] = payload
            except BinanceAPIError as exc:
                result["errors"][path] = {"status_code": exc.status_code, "payload": exc.payload}
            except Exception as exc:
                result["errors"][path] = {"error": str(exc)}
    return result


async def _signed_get(
    client: httpx.AsyncClient,
    base_url: str,
    path: str,
    api_key: str,
    api_secret: str,
    recv_window_ms: int,
    params: dict[str, Any] | None = None,
) -> Any:
    payload = {k: v for k, v in (params or {}).items() if v is not None}
    payload["recvWindow"] = recv_window_ms
    payload["timestamp"] = int(time.time() * 1000)
    query = urlencode(payload, doseq=True)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    payload["signature"] = signature
    response = await client.get(f"{base_url.rstrip('/')}{path}", params=payload, headers={"X-MBX-APIKEY": api_key})
    try:
        data = response.json()
    except ValueError:
        data = response.text
    if response.status_code >= 400:
        raise BinanceAPIError(str(data), response.status_code, data)
    return data


async def _public_ip() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get("https://api.ipify.org")
            response.raise_for_status()
            return response.text.strip()
    except Exception:
        return None


def _quantity_for_notional(notional: Decimal, price: Decimal, filters: SymbolFilters) -> Decimal:
    target_notional = max(notional, filters.min_notional * Decimal("1.25"))
    raw_qty = target_notional / price
    qty = _ceil_to_step(raw_qty, filters.step_size)
    if qty < filters.min_qty:
        qty = filters.min_qty
    if filters.max_qty and qty > filters.max_qty:
        raise RuntimeError(f"Calculated quantity {qty} exceeds maxQty {filters.max_qty}.")
    return qty


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


async def _assert_clean_symbol(
    client: BinanceUSDMClient,
    symbol: str,
    base_url: str,
    api_key: str,
    api_secret: str,
    recv_window_ms: int,
) -> None:
    position_amt = await _position_amount(client, symbol, base_url, api_key, api_secret, recv_window_ms)
    if position_amt != 0:
        raise RuntimeError(f"Pre-existing testnet position on {symbol}: {position_amt}. Choose another symbol.")
    open_orders = await client.open_orders(symbol)
    if open_orders:
        ids = [order.get("clientOrderId") for order in open_orders]
        raise RuntimeError(f"Pre-existing testnet open orders on {symbol}: {ids}. Choose another symbol.")


async def _position_amount(
    client: BinanceUSDMClient,
    symbol: str,
    base_url: str,
    api_key: str,
    api_secret: str,
    recv_window_ms: int,
) -> Decimal:
    try:
        rows = await client.position_risk(symbol)
    except BinanceAPIError:
        async with httpx.AsyncClient(timeout=15) as http_client:
            rows = await _signed_get(
                http_client,
                base_url,
                "/fapi/v2/positionRisk",
                api_key,
                api_secret,
                recv_window_ms,
                {"symbol": symbol},
            )
    for row in rows:
        if row.get("symbol") == symbol:
            return to_decimal(row.get("positionAmt", "0"))
    return Decimal("0")


def _executed_quantity(order: dict[str, Any]) -> Decimal:
    for key in ("executedQty", "cumQty", "origQty"):
        raw = order.get(key)
        if raw not in (None, "", "0", "0.0"):
            return to_decimal(raw)
    return Decimal("0")


async def _cleanup(
    client: BinanceUSDMClient,
    symbol: str,
    client_ids: list[str],
    filters: SymbolFilters,
    base_url: str,
    api_key: str,
    api_secret: str,
    recv_window_ms: int,
) -> None:
    for order in await client.open_orders(symbol):
        client_id = str(order.get("clientOrderId", ""))
        if client_id in client_ids or client_id.startswith("codexp3"):
            try:
                await client.cancel_order(symbol, orig_client_order_id=client_id)
            except BinanceAPIError as exc:
                if exc.status_code != 400:
                    raise
    amount = await _position_amount(client, symbol, base_url, api_key, api_secret, recv_window_ms)
    if amount == 0:
        return
    side = "SELL" if amount > 0 else "BUY"
    close_qty = filters.round_quantity(abs(amount))
    if close_qty <= 0:
        raise RuntimeError(f"Cannot close tiny residual position: {amount}")
    await client.new_order(
        symbol=symbol,
        side=side,
        type="MARKET",
        quantity=format_decimal(close_qty),
        reduceOnly="true",
        newClientOrderId=f"codexp3{int(time.time()) % 1_000_000_000}-cleanup",
    )
    for _ in range(10):
        await asyncio.sleep(0.5)
        if await _position_amount(client, symbol, base_url, api_key, api_secret, recv_window_ms) == 0:
            return
    raise RuntimeError(f"Position cleanup did not flatten {symbol}.")


if __name__ == "__main__":
    main()
