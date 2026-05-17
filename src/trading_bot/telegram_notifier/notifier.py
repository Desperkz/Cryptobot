from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str | None, chat_id: str | None, enabled: bool = True) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled and bool(token and chat_id)
        self._client = httpx.AsyncClient(timeout=10)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, text: str, **metadata: Any) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            response = await self._client.post(url, json=payload)
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)

    async def startup(self, mode: str) -> None:
        mode_ru = {
            "PAPER_TRADING": "📄 Бумажная торговля",
            "TESTNET_LIVE": "🧪 Тестнет",
            "MAINNET_LIVE": "💰 Реальная торговля",
        }.get(mode, mode)
        await self.send(
            f"🟢 <b>Бот v2 запущен</b>\n"
            f"Режим: {mode_ru}"
        )

    async def shutdown(self) -> None:
        await self.send("🔴 <b>Бот v2 остановлен</b>")

    async def universe(self, symbols: list[str]) -> None:
        # Не спамим — universe обновляется каждый цикл
        pass

    async def style(self, symbol: str, style: str) -> None:
        pass

    async def signal(self, symbol: str, direction: str, style: str, reason: str) -> None:
        dir_emoji = "🟢📈" if direction == "LONG" else "🔴📉"
        dir_ru = "ЛОНГ" if direction == "LONG" else "ШОРТ"
        # Извлекаем стратегию из reason
        strat = "SQZ" if "SQUEEZE" in reason else "МР"
        await self.send(
            f"{dir_emoji} <b>Сигнал: {symbol}</b>\n"
            f"Направление: <b>{dir_ru}</b> | Стратегия: <b>{strat}</b>\n"
            f"<code>{reason[:200]}</code>"
        )

    async def trade_opened(
        self,
        symbol: str,
        quantity: str,
        entry: str,
        stop: str,
        take_profit: str,
    ) -> None:
        await self.send(
            f"✅ <b>Позиция открыта: {symbol}</b>\n"
            f"Количество: <code>{quantity}</code>\n"
            f"Вход: <code>${entry}</code>\n"
            f"Стоп: <code>${stop}</code>\n"
            f"Тейк: <code>${take_profit}</code>"
        )

    async def trade_closed(self, symbol: str, pnl: str) -> None:
        try:
            pnl_val = float(pnl)
            emoji = "💰" if pnl_val > 0 else "💸"
            sign = "+" if pnl_val >= 0 else ""
            pnl_str = f"{sign}{pnl_val:.2f} USDT"
        except Exception:
            emoji = "📊"
            pnl_str = pnl
        await self.send(
            f"{emoji} <b>Позиция закрыта: {symbol}</b>\n"
            f"PnL: <b>{pnl_str}</b>"
        )

    async def api_error(self, message: str) -> None:
        await self.send(
            f"⚠️ <b>Ошибка API</b>\n"
            f"<code>{message[:300]}</code>"
        )

    async def daily_loss_limit(self) -> None:
        await self.send(
            "🚨 <b>Достигнут дневной лимит убытков</b>\n"
            "Торговля приостановлена до следующего UTC дня."
        )

    async def daily_report(self, pnl: str) -> None:
        try:
            pnl_val = float(pnl)
            emoji = "📈" if pnl_val >= 0 else "📉"
            sign = "+" if pnl_val >= 0 else ""
            pnl_str = f"{sign}{pnl_val:.2f} USDT"
        except Exception:
            emoji = "📊"
            pnl_str = pnl
        await self.send(
            f"{emoji} <b>Дневной отчёт</b>\n"
            f"PnL за день: <b>{pnl_str}</b>"
        )

    async def risk_warning(self, message: str) -> None:
        await self.send(
            f"⚠️ <b>Предупреждение риска</b>\n"
            f"{message[:300]}"
        )

    async def squeeze_alert(self, symbol: str, bars: int, momentum: float) -> None:
        """Уведомление когда squeeze достигает критического порога."""
        direction = "вверх 📈" if momentum > 0 else "вниз 📉"
        await self.send(
            f"🔔 <b>Squeeze Alert: {symbol}</b>\n"
            f"Баров сжатия: <b>{bars}</b>\n"
            f"Моментум: {direction} ({momentum:.3f})\n"
            f"Возможен пробой!"
        )

    async def filter_rejection(self, symbol: str, direction: str, filter_type: str, reason: str) -> None:
        """Уведомление об отклонённом сигнале (только для важных случаев)."""
        # Отправляем только для важных отказов — не спамим каждым UTC/корреляцией
        if filter_type not in {"RISK", "OI"}:
            return
        dir_ru = "ЛОНГ" if direction == "LONG" else "ШОРТ"
        await self.send(
            f"🚫 <b>Сигнал отклонён: {symbol} {dir_ru}</b>\n"
            f"Фильтр: <b>{filter_type}</b>\n"
            f"{reason[:200]}"
        )
