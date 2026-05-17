"""
Paper Trading Monitor — отдельный процесс который мониторит открытые позиции
и закрывает их по стопу/тейку на основе реальных цен с Binance.

Запуск: python3 paper_monitor.py
Запускать параллельно с ботом через systemd.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [paper_monitor] %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = Path("/root/bot/data/trading_bot.sqlite3")
CHECK_INTERVAL_SEC = 15  # проверяем каждые 15 секунд
BASE_URL = "https://fapi.binance.com"


async def get_current_price(client: httpx.AsyncClient, symbol: str) -> Decimal | None:
    try:
        resp = await client.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5)
        data = resp.json()
        return Decimal(str(data["price"]))
    except Exception as e:
        logger.warning("Не удалось получить цену %s: %s", symbol, e)
        return None


def get_open_positions() -> list[dict]:
    """Читает открытые paper позиции из БД."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            SELECT id, symbol, direction, entry_price, stop_loss, take_profit, quantity
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


def close_position(trade_id: int, symbol: str, direction: str,
                   entry: Decimal, close_price: Decimal,
                   qty: Decimal, reason: str) -> None:
    """Закрывает позицию в БД с расчётом PnL."""
    if direction == "LONG":
        pnl = (close_price - entry) * qty
    else:
        pnl = (entry - close_price) * qty

    # R-multiple: PnL / риск (риск = |entry - stop|)
    r_multiple = -1.0 if reason == "stop_loss" else 1.1

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("""
            UPDATE trades
            SET status = 'CLOSED',
                realized_pnl = ?,
                r_multiple = ?
            WHERE id = ?
        """, (float(pnl), r_multiple, trade_id))
        conn.commit()
        conn.close()

        emoji = "🔴" if reason == "stop_loss" else "🟢"
        logger.info(
            "%s %s #%d закрыта по %s @ $%s | PnL: %+.4f USDT",
            emoji, symbol, trade_id, reason, close_price, float(pnl)
        )
    except Exception as e:
        logger.error("Ошибка закрытия позиции #%d: %s", trade_id, e)


def close_partial_tp1(trade_id: int, symbol: str, direction: str,
                       entry: Decimal, tp1_price: Decimal, qty: Decimal) -> None:
    """TP1 — закрываем 50% позиции и переносим стоп в безубыток."""
    half_qty = qty / 2
    if direction == "LONG":
        pnl = (tp1_price - entry) * half_qty
    else:
        pnl = (entry - tp1_price) * half_qty

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        # Обновляем: уменьшаем количество, переносим стоп в безубыток
        breakeven = float(entry) * (1.0002 if direction == "SHORT" else 0.9998)
        conn.execute("""
            UPDATE trades
            SET quantity = ?,
                stop_loss = ?,
                realized_pnl = realized_pnl + ?
            WHERE id = ?
        """, (float(half_qty), breakeven, float(pnl), trade_id))
        conn.commit()
        conn.close()
        logger.info(
            "🟡 %s #%d TP1 @ $%s | Частичный PnL: %+.4f | Стоп → безубыток $%.4f",
            symbol, trade_id, tp1_price, float(pnl), breakeven
        )
    except Exception as e:
        logger.error("Ошибка TP1 #%d: %s", trade_id, e)


async def check_positions() -> None:
    """Основной цикл мониторинга позиций."""
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

            current = await get_current_price(client, symbol)
            if current is None:
                continue

            # TP1 уровень (0.6 * расстояние до TP)
            tp_dist = abs(tp - entry)
            tp1 = entry - tp_dist * Decimal("0.6") if direction == "SHORT" else entry + tp_dist * Decimal("0.6")

            if direction == "SHORT":
                # Стоп: цена выросла выше SL
                if current >= sl:
                    logger.info("❌ %s: цена %s >= SL %s — закрываем по стопу", symbol, current, sl)
                    close_position(trade_id, symbol, direction, entry, sl, qty, "stop_loss")

                # TP2: цена упала до тейка
                elif current <= tp:
                    logger.info("✅ %s: цена %s <= TP %s — закрываем по тейку", symbol, current, tp)
                    close_position(trade_id, symbol, direction, entry, tp, qty, "take_profit")

            else:  # LONG
                # Стоп: цена упала ниже SL
                if current <= sl:
                    logger.info("❌ %s: цена %s <= SL %s — закрываем по стопу", symbol, current, sl)
                    close_position(trade_id, symbol, direction, entry, sl, qty, "stop_loss")

                # TP2: цена выросла до тейка
                elif current >= tp:
                    logger.info("✅ %s: цена %s >= TP %s — закрываем по тейку", symbol, current, tp)
                    close_position(trade_id, symbol, direction, entry, tp, qty, "take_profit")

            logger.debug(
                "%s %s: цена=$%s вход=$%s SL=$%s TP=$%s",
                symbol, direction, current, entry, sl, tp
            )


async def main() -> None:
    logger.info("Paper Trading Monitor запущен. Проверка каждые %d сек.", CHECK_INTERVAL_SEC)
    while True:
        try:
            await check_positions()
        except Exception as e:
            logger.error("Ошибка цикла: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
