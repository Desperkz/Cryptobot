"""
Paper Trading Monitor — v2
Мониторит открытые позиции бота v2 и закрывает по стопу/тейку.

Запуск: python3 /root/bot_v2/paper_monitor_v2.py
Запускать параллельно с ботом через systemd (paper-monitor-v2.service).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [paper_monitor_v2] %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("PAPER_DB_PATH", "/root/bot_v2/data/trading_bot.sqlite3"))
CHECK_INTERVAL_SEC = 15
BASE_URL = os.getenv("PAPER_PRICE_BASE_URL", "https://fapi.binance.com")
TAKER_FEE_BPS = Decimal(os.getenv("PAPER_TAKER_FEE_BPS", "4.0"))
SLIPPAGE_BPS = Decimal(os.getenv("PAPER_SLIPPAGE_BPS", "5.0"))


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


async def get_current_price(client: httpx.AsyncClient, symbol: str) -> Decimal | None:
    try:
        resp = await client.get(
            f"{BASE_URL}/fapi/v1/ticker/price",
            params={"symbol": symbol},
            timeout=5,
        )
        data = resp.json()
        return Decimal(str(data["price"]))
    except Exception as e:
        logger.warning("Не удалось получить цену %s: %s", symbol, e)
        return None


def get_open_positions() -> list[dict]:
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            SELECT id, symbol, direction, entry_price, stop_loss, take_profit, quantity,
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
            SELECT id, symbol, direction, strategy, entry_price, stop_loss, take_profit,
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
) -> None:
    pnl, effective_close_price, fees = _net_pnl(direction, entry, close_price, qty)

    total_pnl = realized_pnl + pnl
    r_multiple = total_pnl / risk_amount if risk_amount and risk_amount > 0 else Decimal("0")

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("""
            UPDATE trades
            SET status = 'CLOSED',
                realized_pnl = ?,
                r_multiple = ?,
                closed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (str(total_pnl), str(r_multiple), trade_id))
        conn.commit()
        conn.close()
        emoji = "🔴" if reason == "stop_loss" else "🟢"
        logger.info(
            "%s %s #%d закрыта по %s @ $%s | PnL: %+.4f USDT | R: %.2f",
            emoji, symbol, trade_id, reason, close_price, float(total_pnl), float(r_multiple),
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
) -> None:
    pnl, effective_close_price, fees = _net_pnl(direction, entry, close_price, qty)
    r_multiple = pnl / risk_amount if risk_amount and risk_amount > 0 else Decimal("0")

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        ensure_shadow_trades_table(conn)
        row = conn.execute("SELECT metadata FROM shadow_trades WHERE id = ?", (trade_id,)).fetchone()
        metadata = _metadata(row[0] if row else "{}")
        metadata["shadow_close_price"] = str(close_price)
        metadata["shadow_effective_close_price"] = str(effective_close_price)
        metadata["shadow_fees"] = str(fees)
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
            str(pnl),
            str(r_multiple),
            reason,
            json.dumps(metadata, ensure_ascii=False),
            trade_id,
        ))
        conn.commit()
        conn.close()
        logger.info(
            "SHADOW %s #%d %s closed by %s @ $%s | PnL: %+.4f USDT | R: %.2f",
            symbol, trade_id, direction, reason, close_price, float(pnl), float(r_multiple),
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
) -> None:
    target_name = str(target.get("name", "TP"))
    target_price = Decimal(str(target["price"]))
    target_qty = min(Decimal(str(target.get("quantity", qty))), qty)
    if target_qty <= 0:
        return
    pnl, effective_target_price, fees = _net_pnl(direction, entry, target_price, target_qty)

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
        "effective_exit_price": str(effective_target_price),
        "fees": str(fees),
        "slippage_bps": str(SLIPPAGE_BPS),
        "taker_fee_bps": str(TAKER_FEE_BPS),
    })
    next_stop = stop_loss
    if target.get("move_stop_to_breakeven"):
        next_stop = entry * (Decimal("1.0002") if direction == "SHORT" else Decimal("0.9998"))

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
            "🟡 %s #%d %s @ $%s | qty=%s | Частичный PnL: %+.4f | Остаток=%s | SL=%s",
            symbol, trade_id, target_name, target_price, target_qty, float(pnl), remaining_qty, next_stop,
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


def _target_hit(direction: str, current: Decimal, price: Decimal) -> bool:
    return current <= price if direction == "SHORT" else current >= price


def _net_pnl(direction: str, entry: Decimal, exit_price: Decimal, qty: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    effective_exit = _exit_price_with_slippage(direction, exit_price)
    if direction == "LONG":
        gross = (effective_exit - entry) * qty
    else:
        gross = (entry - effective_exit) * qty
    fees = (entry * qty + effective_exit * qty) * TAKER_FEE_BPS / Decimal("10000")
    return gross - fees, effective_exit, fees


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

            current = await get_current_price(client, symbol)
            if current is None:
                continue

            filled_targets = set(metadata.get("filled_partial_targets") or [])
            partial_targets = metadata.get("partial_take_profits") or []
            partial_closed = False
            for target in partial_targets:
                name = str(target.get("name", "TP"))
                if name in filled_targets:
                    continue
                price = Decimal(str(target["price"]))
                if _target_hit(direction, current, price):
                    close_partial_target(trade_id, symbol, direction, entry, target, qty, sl, metadata, risk_amount)
                    partial_closed = True
                    break
            if partial_closed:
                continue

            if direction == "SHORT":
                if current >= sl:
                    logger.info("❌ %s: цена %s >= SL %s", symbol, current, sl)
                    close_position(
                        trade_id, symbol, direction, entry, sl, qty, "stop_loss", risk_amount, realized_pnl
                    )
                elif current <= tp:
                    logger.info("✅ %s: цена %s <= TP %s", symbol, current, tp)
                    close_position(
                        trade_id, symbol, direction, entry, tp, qty, "take_profit", risk_amount, realized_pnl
                    )
            else:
                if current <= sl:
                    logger.info("❌ %s: цена %s <= SL %s", symbol, current, sl)
                    close_position(
                        trade_id, symbol, direction, entry, sl, qty, "stop_loss", risk_amount, realized_pnl
                    )
                elif current >= tp:
                    logger.info("✅ %s: цена %s >= TP %s", symbol, current, tp)
                    close_position(
                        trade_id, symbol, direction, entry, tp, qty, "take_profit", risk_amount, realized_pnl
                    )

            logger.debug(
                "%s %s: цена=$%s вход=$%s SL=$%s TP=$%s",
                symbol, direction, current, entry, sl, tp,
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

            current = await get_current_price(client, symbol)
            if current is None:
                continue

            if direction == "SHORT":
                if current >= sl:
                    close_shadow_position(trade_id, symbol, direction, entry, sl, qty, "stop_loss", risk_amount)
                elif current <= tp:
                    close_shadow_position(trade_id, symbol, direction, entry, tp, qty, "take_profit", risk_amount)
            else:
                if current <= sl:
                    close_shadow_position(trade_id, symbol, direction, entry, sl, qty, "stop_loss", risk_amount)
                elif current >= tp:
                    close_shadow_position(trade_id, symbol, direction, entry, tp, qty, "take_profit", risk_amount)


async def main() -> None:
    logger.info("Paper Monitor v2 запущен. База: %s", DB_PATH)
    logger.info("Проверка каждые %d сек.", CHECK_INTERVAL_SEC)
    while True:
        try:
            await check_positions()
            await check_shadow_positions()
        except Exception as e:
            logger.error("Ошибка цикла: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
