from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable

from trading_bot.config import AppConfig
from trading_bot.data_provider import BinanceUSDMClient
from trading_bot.models import (
    Direction,
    OrderResult,
    Position,
    RiskPlan,
    TakeProfitTarget,
    UserStreamHealth,
    UserStreamStatus,
    to_decimal,
)
from trading_bot.position_manager import PositionManager
from trading_bot.trade_manager.protection import ProtectionManager


logger = logging.getLogger(__name__)


@dataclass
class ManagedTradeState:
    plan: RiskPlan
    filled_targets: set[str]
    trade_id: str | None = None
    client_order_ids: dict[str, str] | None = None
    partial_fill_qty: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    stop_at_breakeven: bool = False
    trailing_active: bool = False


class TradeSupervisor:
    """Event-driven live trade management.

    The supervisor listens to Binance USD-M user-data stream ORDER_TRADE_UPDATE
    events. When a bot TP leg is filled, it performs the follow-up action:

    - TP target filled -> move SL to breakeven when configured.
    - TP target filled -> activate trailing stop when configured.

    REST reconciliation remains as a backup, but this is the fast path for live
    management.
    """

    def __init__(
        self,
        config: AppConfig,
        binance: BinanceUSDMClient,
        positions: PositionManager,
        protection: ProtectionManager,
    ) -> None:
        self.config = config
        self.binance = binance
        self.positions = positions
        self.protection = protection
        self._managed: dict[str, ManagedTradeState] = {}
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._connected = False
        self._last_event_at: float | None = None
        self._last_order_event_at: float | None = None
        self._last_account_event_at: float | None = None
        self._reconnects = 0
        self._last_error: str | None = None
        self._warn: Callable[[str], Awaitable[None]] | None = None
        self._trade_closed: Callable[[str, Decimal], Awaitable[None]] | None = None

    def set_warning_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        self._warn = callback

    def set_trade_closed_callback(self, callback: Callable[[str, Decimal], Awaitable[None]]) -> None:
        self._trade_closed = callback

    def register_plan(self, plan: RiskPlan, result: OrderResult | None = None) -> None:
        self._managed[plan.symbol] = ManagedTradeState(
            plan=plan,
            filled_targets=set(),
            trade_id=result.trade_id if result else None,
            client_order_ids=dict(result.client_order_ids) if result else {},
        )

    def status(self) -> UserStreamStatus:
        health = UserStreamHealth.HEALTHY if self.is_healthy() else UserStreamHealth.STALE
        if not self._connected:
            health = UserStreamHealth.DISCONNECTED
        if self._last_error:
            health = UserStreamHealth.ERROR
        elif self.is_healthy() and self.is_event_stale():
            health = UserStreamHealth.STALE
        return UserStreamStatus(
            health=health,
            connected=self._connected,
            last_event_at=self._last_event_at,
            last_order_event_at=self._last_order_event_at,
            last_account_event_at=self._last_account_event_at,
            reconnects=self._reconnects,
            last_error=self._last_error,
        )

    def is_healthy(self) -> bool:
        if not self._connected or self._last_error is not None:
            return False
        if self._task and self._task.done():
            return False
        return True

    def is_event_stale(self) -> bool:
        if self._last_event_at is None:
            return True
        stale_after = self.config.trade_management.user_stream_stale_after_sec
        return stale_after > 0 and time.monotonic() - self._last_event_at > stale_after

    async def start(self) -> None:
        if self._task or not self.config.is_live:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_user_stream(), name="binance-user-stream")

    async def wait_until_healthy(self, timeout_sec: int = 15) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self.is_healthy():
                return True
            await asyncio.sleep(0.25)
        return self.is_healthy()

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def handle_user_stream_event(self, event: dict[str, Any]) -> None:
        self._last_event_at = time.monotonic()
        event_type = event.get("e")
        if event_type == "ORDER_TRADE_UPDATE":
            await self._handle_order_update(event)
        elif event_type == "ACCOUNT_UPDATE":
            await self._handle_account_update(event)
        elif event_type == "MARGIN_CALL":
            await self._warn_user("Margin call event received; live risk is critical.")
        elif event_type == "listenKeyExpired":
            self._last_error = "listenKeyExpired"
            await self._warn_user("Binance user stream listenKey expired; reconnecting.")
        elif event_type == "CONDITIONAL_ORDER_TRIGGER_REJECT":
            await self._warn_user(f"Conditional order trigger rejected: {event}")

    async def _handle_order_update(self, event: dict[str, Any]) -> None:
        self._last_order_event_at = time.monotonic()
        order = event.get("o", {})
        symbol = order.get("s")
        status = order.get("X")
        execution_type = order.get("x")
        order_type = order.get("o")
        original_order_type = order.get("ot")
        client_order_id = order.get("c", "")
        realized_pnl = to_decimal(order.get("rp") or "0")
        filled_accumulated = to_decimal(order.get("z") or "0")
        last_filled_qty = to_decimal(order.get("l") or "0")
        if not symbol:
            return

        if client_order_id.startswith("autoclose-") or client_order_id == "adl_autoclose" or execution_type == "CALCULATED":
            await self._warn_user(f"{symbol}: liquidation/ADL order event received: {client_order_id}")
            self._managed.pop(symbol, None)
            return

        state = self._managed.get(symbol)
        if not state:
            if not client_order_id.startswith("bot-"):
                await self._warn_user(f"{symbol}: manual/external order update detected: {status} {client_order_id}")
            return

        state.realized_pnl += realized_pnl
        if status == "PARTIALLY_FILLED":
            state.partial_fill_qty = max(state.partial_fill_qty, filled_accumulated)
            logger.info("%s partial fill qty=%s last=%s client_id=%s", symbol, filled_accumulated, last_filled_qty, client_order_id)
            return

        if status in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}:
            await self._warn_user(f"{symbol}: managed order {client_order_id} became {status}.")
            return

        if status != "FILLED":
            return

        if order_type in {"STOP_MARKET", "TRAILING_STOP_MARKET"} or original_order_type in {
            "STOP_MARKET",
            "TRAILING_STOP_MARKET",
        }:
            await self._on_protective_exit_filled(symbol, client_order_id, state)
            return

        target = _target_from_client_order_id(client_order_id, state.plan.partial_take_profits)
        if not target or target.name in state.filled_targets:
            return
        state.filled_targets.add(target.name)
        await self._apply_target_followup(symbol, state, target)

    async def _handle_account_update(self, event: dict[str, Any]) -> None:
        self._last_account_event_at = time.monotonic()
        payload = event.get("a", {})
        reason = payload.get("m", "")
        for item in payload.get("P", []):
            symbol = item.get("s")
            if not symbol:
                continue
            amount = to_decimal(item.get("pa", "0"))
            if amount == 0:
                self.positions.clear_local_position(symbol)
                state = self._managed.pop(symbol, None)
                if state and self._trade_closed:
                    await self._trade_closed(symbol, state.realized_pnl)
                logger.info("%s position closed by ACCOUNT_UPDATE reason=%s", symbol, reason)
                continue
            if symbol not in self._managed and self.config.safety.adopt_manual_positions:
                direction = Direction.LONG if amount > 0 else Direction.SHORT
                self.positions.set_local_position(
                    Position(
                        symbol=symbol,
                        direction=direction,
                        quantity=abs(amount),
                        entry_price=to_decimal(item.get("ep", "0")),
                        mark_price=None,
                        managed_by_bot=False,
                        unrealized_pnl=to_decimal(item.get("up", "0")),
                        source="USER_STREAM_MANUAL_OR_EXTERNAL",
                    )
                )
                await self._warn_user(f"{symbol}: manual/external position adopted from ACCOUNT_UPDATE.")

    async def reconcile_managed_trades(self) -> None:
        """REST backup for missed websocket events."""
        if self.config.trade_management.user_stream_required_for_live and self.is_healthy() and not self.is_event_stale():
            return
        if not self.config.trade_management.rest_reconciliation_when_stale:
            return
        for symbol, state in list(self._managed.items()):
            try:
                orders = await self.binance.open_orders(symbol)
            except Exception:
                logger.exception("Could not reconcile managed trade %s", symbol)
                continue
            open_client_ids = {order.get("clientOrderId", "") for order in orders}
            for target in state.plan.partial_take_profits:
                client_order_id = _target_client_order_id(symbol, state, target)
                if target.name not in state.filled_targets and client_order_id not in open_client_ids:
                    try:
                        order_state = await self.binance.query_order(symbol, orig_client_order_id=client_order_id)
                    except Exception:
                        continue
                    if order_state.get("status") == "FILLED":
                        state.filled_targets.add(target.name)
                        await self._apply_target_followup(symbol, state, target)
            await self._close_if_remote_position_gone(symbol, state)

    async def _apply_target_followup(self, symbol: str, state: ManagedTradeState, target: TakeProfitTarget) -> None:
        position = await self._position(symbol)
        if not position:
            await self._close_managed_trade(symbol, state, f"{symbol}: position absent after {target.name} fill.")
            return
        remaining_qty = position.quantity
        if target.move_stop_to_breakeven and not state.stop_at_breakeven:
            await self.protection.move_stop_to_breakeven(
                position,
                state.plan,
                client_order_id=_managed_client_order_id(symbol, state.trade_id, "slbe"),
            )
            state.stop_at_breakeven = True
            logger.info("%s %s filled; stop moved to breakeven.", symbol, target.name)
        if target.activate_trailing and not state.trailing_active:
            await self.protection.activate_trailing_stop(
                position,
                state.plan,
                remaining_qty,
                client_order_id=_managed_client_order_id(symbol, state.trade_id, "trail"),
            )
            state.trailing_active = True
            logger.info("%s %s filled; trailing stop activated.", symbol, target.name)

    async def _on_protective_exit_filled(self, symbol: str, client_order_id: str, state: ManagedTradeState) -> None:
        await self._close_managed_trade(
            symbol,
            state,
            f"{symbol}: protective exit filled ({client_order_id}); trade management stopped.",
        )

    async def _close_if_remote_position_gone(self, symbol: str, state: ManagedTradeState) -> None:
        try:
            raw_positions = await self.binance.position_risk(symbol)
        except Exception:
            logger.exception("Could not verify remote position state for %s", symbol)
            return
        has_remote_position = any(
            item.get("symbol") == symbol and abs(to_decimal(item.get("positionAmt", "0"))) > 0
            for item in raw_positions
        )
        if has_remote_position:
            return
        await self._close_managed_trade(
            symbol,
            state,
            f"{symbol}: REST reconciliation detected no remote position; trade marked closed.",
        )

    async def _close_managed_trade(self, symbol: str, state: ManagedTradeState, message: str) -> None:
        self._managed.pop(symbol, None)
        self.positions.clear_local_position(symbol)
        if self._trade_closed:
            await self._trade_closed(symbol, state.realized_pnl)
        await self._warn_user(message)

    async def _position(self, symbol: str) -> Position | None:
        for position in await self.positions.active_positions():
            if position.symbol == symbol:
                return position
        return None

    async def _run_user_stream(self) -> None:
        listen_key_payload = await self.binance.start_user_stream()
        listen_key = listen_key_payload.get("listenKey")
        if not listen_key:
            raise RuntimeError("Binance did not return listenKey for user stream.")
        keepalive = asyncio.create_task(self._keepalive_listen_key(listen_key))
        stream_url = f"{self.config.websocket_base_url}/ws/{listen_key}"
        try:
            import websockets

            while not self._stopping.is_set():
                try:
                    self._connected = False
                    async with websockets.connect(stream_url, ping_interval=20, ping_timeout=20) as websocket:
                        self._connected = True
                        self._last_error = None
                        self._last_event_at = time.monotonic()
                        async for raw in websocket:
                            await self.handle_user_stream_event(json.loads(raw))
                            if self._stopping.is_set():
                                break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._connected = False
                    self._reconnects += 1
                    self._last_error = "user stream disconnected"
                    logger.exception("User stream disconnected; reconnecting.")
                    await self._warn_user("Binance user stream disconnected; REST reconciliation is backup until reconnect.")
                    await asyncio.sleep(self.config.trade_management.user_stream_reconnect_backoff_sec)
        finally:
            self._connected = False
            keepalive.cancel()
            try:
                await keepalive
            except asyncio.CancelledError:
                pass
            await self.binance.close_user_stream(listen_key)

    async def _keepalive_listen_key(self, listen_key: str) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(30 * 60)
            await self.binance.keepalive_user_stream(listen_key)

    async def _warn_user(self, message: str) -> None:
        logger.warning(message)
        if self._warn:
            await self._warn(message)


def _target_from_client_order_id(client_order_id: str, targets: tuple[TakeProfitTarget, ...]) -> TakeProfitTarget | None:
    lowered = client_order_id.lower()
    for target in targets:
        if lowered.endswith(f"-{target.name.lower()}"):
            return target
    return None


def _target_client_order_id(symbol: str, state: ManagedTradeState, target: TakeProfitTarget) -> str:
    if state.client_order_ids:
        configured = state.client_order_ids.get(target.name)
        if configured:
            return configured
    return _managed_client_order_id(symbol, state.trade_id, target.name.lower())


def _managed_client_order_id(symbol: str, trade_id: str | None, suffix: str) -> str:
    if trade_id:
        return f"bot-{symbol[:12]}-{trade_id}-{suffix}"[:36]
    return f"bot-{symbol[:12]}-{suffix}"[:36]
