"""
Paper Trading Monitor — v2.1
Мониторит открытые paper-позиции бота v2.1 и закрывает по стопу/тейку.

Запуск: python3 /root/bot_v2_1/paper_monitor_v2.py
Запускать параллельно с ботом через systemd (paper-monitor-v2-1.service).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

try:
    from trading_bot.operational import SystemdNotifier
except Exception:  # pragma: no cover - standalone fallback
    SystemdNotifier = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [paper_monitor_v2] %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("PAPER_DB_PATH", "/root/bot_v2_1/data/trading_bot_v2_1.sqlite3"))
CHECK_INTERVAL_SEC = 15
BASE_URL = os.getenv("PAPER_PRICE_BASE_URL", "https://fapi.binance.com")
TAKER_FEE_BPS = Decimal(os.getenv("PAPER_TAKER_FEE_BPS", "4.0"))
SLIPPAGE_BPS = Decimal(os.getenv("PAPER_SLIPPAGE_BPS", "5.0"))
FUNDING_BPS_PER_8H = Decimal(os.getenv("PAPER_FUNDING_BPS_PER_8H", "1.0"))
BREAKEVEN_OFFSET_BPS = Decimal(os.getenv("PAPER_BREAKEVEN_OFFSET_BPS", "2.0"))
TRAILING_CALLBACK_RATE_PCT = Decimal(os.getenv("PAPER_TRAILING_CALLBACK_RATE_PCT", "0.4"))
PESSIMISTIC_INTRABAR = os.getenv("PAPER_PESSIMISTIC_INTRABAR", "1").strip().lower() not in {"0", "false", "no"}


@dataclass(frozen=True)
class MarketSnapshot:
    price: Decimal
    high: Decimal
    low: Decimal
    candle_open_time: datetime | None = None
    candle_close_time: datetime | None = None


@dataclass(frozen=True)
class ExecutionBreakdown:
    net_pnl: Decimal
    gross_pnl: Decimal
    fees: Decimal
    slippage_cost: Decimal
    funding_cost: Decimal
    effective_close_price: Decimal
    held_hours: Decimal


def ensure_shadow_trades_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            strategy TEXT NOT NULL,
            quantity TEXT NOT NULL,
            entry_price TEXT NOT NULL,
            stop_loss TEXT NOT NULL,
            take_profit TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'SHADOW_PAPER',
            status TEXT NOT NULL,
            risk_amount TEXT DEFAULT '0',
            r_multiple TEXT DEFAULT '0',
            realized_pnl TEXT DEFAULT '0',
            close_reason TEXT,
            metadata TEXT NOT NULL
        )
    """)


async def get_market_snapshot(client: httpx.AsyncClient, symbol: str) -> MarketSnapshot | None:
    try:
        resp = await client.get(
            f"{BASE_URL}/fapi/v1/ticker/price",
            params={"symbol": symbol},
            timeout=5,
        )
        data = resp.json()
        price = Decimal(str(data["price"]))
        high = price
        low = price
        candle_open_time: datetime | None = None
        candle_close_time: datetime | None = None
        try:
            candle_resp = await client.get(
                f"{BASE_URL}/fapi/v1/klines",
                params={"symbol": symbol, "interval": "1m", "limit": 1},
                timeout=5,
            )
            candles = candle_resp.json()
            if candles:
                candle = candles[-1]
                high = max(high, Decimal(str(candle[2])))
                low = min(low, Decimal(str(candle[3])))
                candle_open_time = datetime.fromtimestamp(int(candle[0]) / 1000, tz=timezone.utc)
                candle_close_time = datetime.fromtimestamp(int(candle[6]) / 1000, tz=timezone.utc)
        except Exception as candle_error:
            logger.debug("Не удалось получить 1m свечу %s: %s", symbol, candle_error)
        return MarketSnapshot(
            price=price,
            high=high,
            low=low,
            candle_open_time=candle_open_time,
            candle_close_time=candle_close_time,
        )
    except Exception as e:
        logger.warning("Не удалось получить цену %s: %s", symbol, e)
        return None


async def get_current_price(client: httpx.AsyncClient, symbol: str) -> Decimal | None:
    snapshot = await get_market_snapshot(client, symbol)
    return snapshot.price if snapshot else None


def get_open_positions() -> list[dict]:
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            SELECT id, created_at, symbol, direction, entry_price, stop_loss, take_profit, quantity,
                   risk_amount, realized_pnl, metadata
            FROM trades
            WHERE status IN ('ACCEPTED', 'OPEN', 'ACTIVE')
              AND mode = 'PAPER_TRADING'
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.error("Ошибка чтения БД: %s", e)
        return []


def get_open_shadow_positions() -> list[dict]:
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        ensure_shadow_trades_table(conn)
        cur = conn.execute("""
            SELECT id, created_at, symbol, direction, strategy, entry_price, stop_loss, take_profit,
                   quantity, risk_amount, realized_pnl, metadata
            FROM shadow_trades
            WHERE status IN ('ACCEPTED', 'OPEN', 'ACTIVE')
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.error("Shadow DB read error: %s", e)
        return []


def close_position(
    trade_id: int,
    symbol: str,
    direction: str,
    entry: Decimal,
    close_price: Decimal,
    qty: Decimal,
    reason: str,
    risk_amount: Decimal | None = None,
    realized_pnl: Decimal = Decimal("0"),
    metadata: dict | None = None,
    opened_at: object = None,
) -> None:
    metadata = metadata or {}
    execution = _execution_pnl(direction, entry, close_price, qty, opened_at=opened_at)
    _record_execution_metadata(metadata, "paper_executions", "FINAL", reason, close_price, execution)

    total_pnl = realized_pnl + execution.net_pnl
    r_multiple = total_pnl / risk_amount if risk_amount and risk_amount > 0 else Decimal("0")

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("""
            UPDATE trades
            SET status = 'CLOSED',
                realized_pnl = ?,
                r_multiple = ?,
                metadata = ?,
                closed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (str(total_pnl), str(r_multiple), json.dumps(metadata, ensure_ascii=False), trade_id))
        conn.commit()
        conn.close()
        emoji = "🔴" if reason == "stop_loss" else "🟢"
        logger.info(
            "%s %s #%d закрыта по %s @ $%s | Net PnL: %+.4f USDT | costs=%s | R: %.2f",
            emoji,
            symbol,
            trade_id,
            reason,
            close_price,
            float(total_pnl),
            _format_costs(execution),
            float(r_multiple),
        )
    except Exception as e:
        logger.error("Ошибка закрытия позиции #%d: %s", trade_id, e)


def close_shadow_position(
    trade_id: int,
    symbol: str,
    direction: str,
    entry: Decimal,
    close_price: Decimal,
    qty: Decimal,
    reason: str,
    risk_amount: Decimal | None = None,
    opened_at: object = None,
) -> None:
    execution = _execution_pnl(direction, entry, close_price, qty, opened_at=opened_at)
    r_multiple = execution.net_pnl / risk_amount if risk_amount and risk_amount > 0 else Decimal("0")

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        ensure_shadow_trades_table(conn)
        row = conn.execute("SELECT metadata FROM shadow_trades WHERE id = ?", (trade_id,)).fetchone()
        metadata = _metadata(row[0] if row else "{}")
        metadata["shadow_close_price"] = str(close_price)
        metadata["shadow_effective_close_price"] = str(execution.effective_close_price)
        metadata["shadow_fees"] = str(execution.fees)
        metadata["shadow_slippage_cost"] = str(execution.slippage_cost)
        metadata["shadow_funding_cost"] = str(execution.funding_cost)
        _record_execution_metadata(metadata, "shadow_executions", "FINAL", reason, close_price, execution)
        conn.execute("""
            UPDATE shadow_trades
            SET status = 'CLOSED',
                realized_pnl = ?,
                r_multiple = ?,
                close_reason = ?,
                metadata = ?,
                closed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            str(execution.net_pnl),
            str(r_multiple),
            reason,
            json.dumps(metadata, ensure_ascii=False),
            trade_id,
        ))
        conn.commit()
        conn.close()
        logger.info(
            "SHADOW %s #%d %s closed by %s @ $%s | Net PnL: %+.4f USDT | costs=%s | R: %.2f",
            symbol,
            trade_id,
            direction,
            reason,
            close_price,
            float(execution.net_pnl),
            _format_costs(execution),
            float(r_multiple),
        )
    except Exception as e:
        logger.error("Shadow close error #%d: %s", trade_id, e)


def close_partial_target(
    trade_id: int,
    symbol: str,
    direction: str,
    entry: Decimal,
    target: dict,
    qty: Decimal,
    stop_loss: Decimal,
    metadata: dict,
    risk_amount: Decimal | None = None,
    opened_at: object = None,
) -> None:
    target_name = str(target.get("name", "TP"))
    target_price = Decimal(str(target["price"]))
    target_qty = min(Decimal(str(target.get("quantity", qty))), qty)
    if target_qty <= 0:
        return
    execution = _execution_pnl(direction, entry, target_price, target_qty, opened_at=opened_at)
    pnl = execution.net_pnl

    remaining_qty = qty - target_qty
    original_qty = _original_quantity(metadata, qty)
    filled = set(metadata.get("filled_partial_targets") or [])
    filled.add(target_name)
    metadata["filled_partial_targets"] = sorted(filled)
    metadata.setdefault("original_quantity", str(original_qty))
    metadata["remaining_quantity"] = str(remaining_qty)
    metadata.setdefault("paper_costs", []).append({
        "target": target_name,
        "trigger_price": str(target_price),
        "effective_exit_price": str(execution.effective_close_price),
        "gross_pnl": str(execution.gross_pnl),
        "fees": str(execution.fees),
        "slippage_cost": str(execution.slippage_cost),
        "funding_cost": str(execution.funding_cost),
        "net_pnl": str(execution.net_pnl),
        "slippage_bps": str(SLIPPAGE_BPS),
        "taker_fee_bps": str(TAKER_FEE_BPS),
        "funding_bps_per_8h": str(FUNDING_BPS_PER_8H),
    })
    _record_execution_metadata(metadata, "paper_executions", target_name, "partial_take_profit", target_price, execution)
    next_stop = stop_loss
    if target.get("move_stop_to_breakeven"):
        next_stop = _breakeven_price(direction, entry, metadata)
        metadata["stop_moved_to_breakeven"] = True
        metadata["breakeven_price"] = str(next_stop)
    if target.get("activate_trailing") and remaining_qty > 0:
        metadata["trailing_active"] = True
        metadata.setdefault("trailing_anchor_price", str(target_price))
        metadata["trailing_callback_rate_pct"] = str(_trailing_callback_rate(metadata))

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        if remaining_qty <= 0:
            current_realized = conn.execute(
                "SELECT realized_pnl FROM trades WHERE id = ?",
                (trade_id,),
            ).fetchone()
            total_pnl = Decimal(str(current_realized[0] if current_realized else "0")) + pnl
            r_multiple = total_pnl / risk_amount if risk_amount and risk_amount > 0 else Decimal("0")
            conn.execute("""
                UPDATE trades
                SET status = 'CLOSED',
                    quantity = ?,
                    stop_loss = ?,
                    realized_pnl = realized_pnl + ?,
                    r_multiple = ?,
                    metadata = ?,
                    closed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (
                str(original_qty),
                str(next_stop),
                str(pnl),
                str(r_multiple),
                json.dumps(metadata, ensure_ascii=False),
                trade_id,
            ))
        else:
            conn.execute("""
                UPDATE trades
                SET quantity = ?,
                    stop_loss = ?,
                    realized_pnl = realized_pnl + ?,
                    metadata = ?
                WHERE id = ?
            """, (str(remaining_qty), str(next_stop), str(pnl), json.dumps(metadata, ensure_ascii=False), trade_id))
        conn.commit()
        conn.close()
        logger.info(
            "🟡 %s #%d %s @ $%s | qty=%s | Net partial PnL: %+.4f | costs=%s | Остаток=%s | SL=%s",
            symbol,
            trade_id,
            target_name,
            target_price,
            target_qty,
            float(pnl),
            _format_costs(execution),
            remaining_qty,
            next_stop,
        )
    except Exception as e:
        logger.error("Ошибка частичного тейка #%d: %s", trade_id, e)


def _metadata(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}


def _target_hit(direction: str, snapshot: MarketSnapshot, price: Decimal) -> bool:
    return snapshot.low <= price if direction == "SHORT" else snapshot.high >= price


def _stop_hit(direction: str, snapshot: MarketSnapshot, stop_loss: Decimal) -> bool:
    return snapshot.high >= stop_loss if direction == "SHORT" else snapshot.low <= stop_loss


def _next_exit_event(
    direction: str,
    snapshot: MarketSnapshot,
    stop_loss: Decimal,
    take_profit: Decimal,
    partial_targets: list[dict],
    filled_targets: set[str],
) -> tuple[str, Decimal, dict | None] | None:
    stop_hit = _stop_hit(direction, snapshot, stop_loss)
    partial_hit: dict | None = None
    for target in partial_targets:
        name = str(target.get("name", "TP"))
        if name in filled_targets:
            continue
        price = Decimal(str(target["price"]))
        if _target_hit(direction, snapshot, price):
            partial_hit = target
            break
    take_profit_hit = _target_hit(direction, snapshot, take_profit)

    if PESSIMISTIC_INTRABAR and stop_hit and (partial_hit or take_profit_hit):
        return "stop_loss", stop_loss, None
    if stop_hit:
        return "stop_loss", stop_loss, None
    if partial_hit:
        return "partial_take_profit", Decimal(str(partial_hit["price"])), partial_hit
    if take_profit_hit:
        return "take_profit", take_profit, None
    return None


def _snapshot_for_trade_lifetime(snapshot: MarketSnapshot, opened_at: object) -> MarketSnapshot:
    opened = _parse_timestamp(opened_at)
    if opened is None or snapshot.candle_open_time is None:
        return snapshot
    if opened <= snapshot.candle_open_time:
        return snapshot

    # The latest 1m candle range may contain movement that happened before this
    # trade existed. Until the next candle, fall back to current price only.
    return MarketSnapshot(
        price=snapshot.price,
        high=snapshot.price,
        low=snapshot.price,
        candle_open_time=snapshot.candle_open_time,
        candle_close_time=snapshot.candle_close_time,
    )


def _execution_pnl(
    direction: str,
    entry: Decimal,
    exit_price: Decimal,
    qty: Decimal,
    opened_at: object = None,
    closed_at: datetime | None = None,
) -> ExecutionBreakdown:
    closed_at = closed_at or datetime.now(timezone.utc)
    effective_exit = _exit_price_with_slippage(direction, exit_price)
    if direction == "LONG":
        gross = (exit_price - entry) * qty
        gross_after_slippage = (effective_exit - entry) * qty
    else:
        gross = (entry - exit_price) * qty
        gross_after_slippage = (entry - effective_exit) * qty
    slippage_cost = max(gross - gross_after_slippage, Decimal("0"))
    fees = (entry * qty + effective_exit * qty) * TAKER_FEE_BPS / Decimal("10000")
    held_hours = _held_hours(opened_at, closed_at)
    funding_cost = _funding_cost(entry, qty, held_hours)
    return ExecutionBreakdown(
        net_pnl=gross - slippage_cost - fees - funding_cost,
        gross_pnl=gross,
        fees=fees,
        slippage_cost=slippage_cost,
        funding_cost=funding_cost,
        effective_close_price=effective_exit,
        held_hours=held_hours,
    )


def _held_hours(opened_at: object, closed_at: datetime) -> Decimal:
    opened = _parse_timestamp(opened_at)
    if opened is None:
        return Decimal("0")
    seconds = max((closed_at - opened).total_seconds(), 0.0)
    return Decimal(str(seconds)) / Decimal("3600")


def _parse_timestamp(raw: object) -> datetime | None:
    if raw in (None, "", "None"):
        return None
    if isinstance(raw, datetime):
        value = raw
    else:
        text = str(raw).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            try:
                value = datetime.strptime(text.split(".")[0], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _funding_cost(entry: Decimal, qty: Decimal, held_hours: Decimal) -> Decimal:
    if held_hours <= 0 or FUNDING_BPS_PER_8H <= 0:
        return Decimal("0")
    return entry * qty * FUNDING_BPS_PER_8H / Decimal("10000") * (held_hours / Decimal("8"))


def _record_execution_metadata(
    metadata: dict,
    key: str,
    target: str,
    reason: str,
    requested_exit_price: Decimal,
    execution: ExecutionBreakdown,
) -> None:
    event = {
        "target": target,
        "reason": reason,
        "requested_exit_price": str(requested_exit_price),
        "effective_exit_price": str(execution.effective_close_price),
        "gross_pnl": str(execution.gross_pnl),
        "fees": str(execution.fees),
        "slippage_cost": str(execution.slippage_cost),
        "funding_cost": str(execution.funding_cost),
        "net_pnl": str(execution.net_pnl),
        "held_hours": str(execution.held_hours),
    }
    metadata.setdefault(key, []).append(event)
    summary = metadata.setdefault("paper_execution_summary", {
        "gross_pnl": "0",
        "fees": "0",
        "slippage_cost": "0",
        "funding_cost": "0",
        "net_pnl": "0",
    })
    for field in ("gross_pnl", "fees", "slippage_cost", "funding_cost", "net_pnl"):
        summary[field] = str(Decimal(str(summary.get(field, "0"))) + Decimal(event[field]))


def _format_costs(execution: ExecutionBreakdown) -> str:
    total_cost = execution.fees + execution.slippage_cost + execution.funding_cost
    return (
        f"total={total_cost:.4f}, fee={execution.fees:.4f}, "
        f"slip={execution.slippage_cost:.4f}, funding={execution.funding_cost:.4f}"
    )


def _breakeven_price(direction: str, entry: Decimal, metadata: dict) -> Decimal:
    protection = metadata.get("protection") or {}
    raw = protection.get("breakeven_price") or metadata.get("breakeven_price")
    if raw not in (None, "", "None"):
        try:
            return Decimal(str(raw))
        except Exception:
            pass
    offset = entry * BREAKEVEN_OFFSET_BPS / Decimal("10000")
    return entry - offset if direction == "SHORT" else entry + offset


def _trailing_callback_rate(metadata: dict) -> Decimal:
    protection = metadata.get("protection") or {}
    raw = protection.get("trailing_callback_rate_pct") or metadata.get("trailing_callback_rate_pct")
    if raw not in (None, "", "None"):
        try:
            return Decimal(str(raw))
        except Exception:
            pass
    return TRAILING_CALLBACK_RATE_PCT


def _apply_trailing_stop(direction: str, snapshot: MarketSnapshot, stop_loss: Decimal, metadata: dict) -> tuple[Decimal, bool]:
    if not metadata.get("trailing_active"):
        return stop_loss, False
    callback = _trailing_callback_rate(metadata) / Decimal("100")
    anchor_raw = metadata.get("trailing_anchor_price")
    try:
        anchor = Decimal(str(anchor_raw)) if anchor_raw not in (None, "", "None") else snapshot.price
    except Exception:
        anchor = snapshot.price
    if direction == "SHORT":
        anchor = min(anchor, snapshot.low)
        candidate = anchor * (Decimal("1") + callback)
        improved = candidate < stop_loss
    else:
        anchor = max(anchor, snapshot.high)
        candidate = anchor * (Decimal("1") - callback)
        improved = candidate > stop_loss
    metadata["trailing_anchor_price"] = str(anchor)
    metadata["trailing_stop_price"] = str(candidate)
    if improved:
        return candidate, True
    return stop_loss, False


def _original_quantity(metadata: dict, fallback_qty: Decimal) -> Decimal:
    raw = metadata.get("original_quantity")
    if raw not in (None, "", "None"):
        try:
            return Decimal(str(raw))
        except Exception:
            pass
    targets = metadata.get("partial_take_profits") or []
    try:
        total = sum((Decimal(str(target.get("quantity", "0"))) for target in targets), Decimal("0"))
    except Exception:
        total = Decimal("0")
    return total if total > 0 else fallback_qty


def _exit_price_with_slippage(direction: str, price: Decimal) -> Decimal:
    slippage = SLIPPAGE_BPS / Decimal("10000")
    if direction == "LONG":
        return price * (Decimal("1") - slippage)
    return price * (Decimal("1") + slippage)


async def check_positions() -> None:
    positions = get_open_positions()
    if not positions:
        return

    async with httpx.AsyncClient() as client:
        for pos in positions:
            trade_id = pos["id"]
            symbol = pos["symbol"]
            direction = pos["direction"]
            entry = Decimal(str(pos["entry_price"]))
            sl = Decimal(str(pos["stop_loss"]))
            tp = Decimal(str(pos["take_profit"]))
            qty = Decimal(str(pos["quantity"]))
            risk_amount = Decimal(str(pos.get("risk_amount") or "0"))
            realized_pnl = Decimal(str(pos.get("realized_pnl") or "0"))
            metadata = _metadata(pos.get("metadata"))
            opened_at = pos.get("created_at")

            snapshot = await get_market_snapshot(client, symbol)
            if snapshot is None:
                continue
            snapshot = _snapshot_for_trade_lifetime(snapshot, opened_at)

            sl, trailing_changed = _apply_trailing_stop(direction, snapshot, sl, metadata)
            if trailing_changed:
                try:
                    conn = sqlite3.connect(str(DB_PATH), timeout=5)
                    conn.execute(
                        "UPDATE trades SET stop_loss = ?, metadata = ? WHERE id = ?",
                        (str(sl), json.dumps(metadata, ensure_ascii=False), trade_id),
                    )
                    conn.commit()
                    conn.close()
                    logger.info("🔁 %s #%d trailing SL обновлен: %s", symbol, trade_id, sl)
                except Exception as e:
                    logger.error("Ошибка обновления trailing SL #%d: %s", trade_id, e)

            filled_targets = set(metadata.get("filled_partial_targets") or [])
            partial_targets = metadata.get("partial_take_profits") or []
            event = _next_exit_event(direction, snapshot, sl, tp, partial_targets, filled_targets)
            if not event:
                logger.debug(
                    "%s %s: price=$%s high=$%s low=$%s entry=$%s SL=$%s TP=$%s",
                    symbol,
                    direction,
                    snapshot.price,
                    snapshot.high,
                    snapshot.low,
                    entry,
                    sl,
                    tp,
                )
                continue

            reason, trigger_price, target = event
            if reason == "partial_take_profit" and target is not None:
                close_partial_target(
                    trade_id,
                    symbol,
                    direction,
                    entry,
                    target,
                    qty,
                    sl,
                    metadata,
                    risk_amount,
                    opened_at,
                )
                continue

            if reason == "stop_loss":
                logger.info("❌ %s: 1m range high=%s low=%s hit SL %s", symbol, snapshot.high, snapshot.low, trigger_price)
            else:
                logger.info("✅ %s: 1m range high=%s low=%s hit TP %s", symbol, snapshot.high, snapshot.low, trigger_price)
            close_position(
                trade_id,
                symbol,
                direction,
                entry,
                trigger_price,
                qty,
                reason,
                risk_amount,
                realized_pnl,
                metadata,
                opened_at,
            )


async def check_shadow_positions() -> None:
    positions = get_open_shadow_positions()
    if not positions:
        return

    async with httpx.AsyncClient() as client:
        for pos in positions:
            trade_id = pos["id"]
            symbol = pos["symbol"]
            direction = pos["direction"]
            entry = Decimal(str(pos["entry_price"]))
            sl = Decimal(str(pos["stop_loss"]))
            tp = Decimal(str(pos["take_profit"]))
            qty = Decimal(str(pos["quantity"]))
            risk_amount = Decimal(str(pos.get("risk_amount") or "0"))
            opened_at = pos.get("created_at")

            snapshot = await get_market_snapshot(client, symbol)
            if snapshot is None:
                continue
            snapshot = _snapshot_for_trade_lifetime(snapshot, opened_at)

            event = _next_exit_event(direction, snapshot, sl, tp, [], set())
            if not event:
                continue
            reason, trigger_price, _target = event
            close_shadow_position(trade_id, symbol, direction, entry, trigger_price, qty, reason, risk_amount, opened_at)


async def main() -> None:
    logger.info("Paper Monitor v2 запущен. База: %s", DB_PATH)
    logger.info("Проверка каждые %d сек.", CHECK_INTERVAL_SEC)
    notifier = SystemdNotifier() if SystemdNotifier else None
    if notifier:
        notifier.ready()
    while True:
        try:
            await check_positions()
            await check_shadow_positions()
            if notifier:
                notifier.watchdog("paper-monitor-v2-1 running")
        except Exception as e:
            logger.error("Ошибка цикла: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
