"""
Disaster Mode — система защиты от катастрофических сбоев.

Обнаруживает и реагирует на:
- Binance API outage (недоступность REST)
- WebSocket freeze (зависание потока данных)
- Exchange abnormal state (аномальный спред, отсутствие ликвидности)
- Sudden liquidation cascade (каскадные ликвидации на рынке)
- Consecutive losses breach (серия потерь сверх лимита)

При обнаружении угрозы:
1. Блокирует новые входы
2. Уведомляет в Telegram
3. В LIVE режиме — принудительно закрывает позиции
4. Логирует событие с полным контекстом
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)


class DisasterLevel(Enum):
    NONE = "none"
    WARNING = "warning"       # Предупреждение — входы ограничены
    CRITICAL = "critical"     # Критический — входы заблокированы
    EMERGENCY = "emergency"   # Аварийный — позиции закрываются


_LEVEL_RANK = {
    DisasterLevel.NONE: 0,
    DisasterLevel.WARNING: 1,
    DisasterLevel.CRITICAL: 2,
    DisasterLevel.EMERGENCY: 3,
}


class DisasterReason(Enum):
    API_OUTAGE = "api_outage"
    WEBSOCKET_FREEZE = "websocket_freeze"
    EXCHANGE_ANOMALY = "exchange_anomaly"
    LIQUIDATION_CASCADE = "liquidation_cascade"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    MANUAL = "manual"


@dataclass
class DisasterEvent:
    level: DisasterLevel
    reason: DisasterReason
    message: str
    timestamp: float = field(default_factory=time.time)
    auto_resolved: bool = False


@dataclass
class DisasterConfig:
    # API outage
    api_timeout_sec: float = 10.0
    api_max_consecutive_failures: int = 3

    # WebSocket freeze
    ws_stale_after_sec: float = 30.0

    # Exchange anomaly
    max_spread_bps_disaster: float = 50.0   # спред > 50 bps = аномалия
    min_liquidity_usdt: float = 10_000.0    # ликвидность < $10k = аномалия

    # Liquidation cascade
    cascade_funding_rate_threshold: float = 0.003   # funding > 0.3% = каскад
    cascade_price_move_pct: float = 5.0             # движение > 5% за 15m

    # Consecutive losses
    max_consecutive_losses: int = 5
    max_daily_loss_pct: float = 0.08   # 8% в день = disaster

    # Recovery
    recovery_cooldown_sec: float = 300.0   # 5 минут после восстановления
    auto_recover: bool = True


class DisasterDetector:
    """
    Отслеживает состояние системы и рынка.
    Принимает решение о переходе в disaster mode.
    """

    def __init__(
        self,
        config: DisasterConfig,
        warn_callback: Callable[[str], Awaitable[None]] | None = None,
        emergency_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self._warn = warn_callback
        self._emergency = emergency_callback

        self._level = DisasterLevel.NONE
        self._events: list[DisasterEvent] = []
        self._api_failures = 0
        self._last_ws_event_at: float | None = None
        self._consecutive_losses = 0
        self._daily_loss_pct = Decimal("0")
        self._disaster_since: float | None = None
        self._lock = asyncio.Lock()

    @property
    def level(self) -> DisasterLevel:
        return self._level

    @property
    def is_safe(self) -> bool:
        return self._level == DisasterLevel.NONE

    @property
    def blocks_new_entries(self) -> bool:
        return self._level in {DisasterLevel.CRITICAL, DisasterLevel.EMERGENCY}

    @property
    def requires_position_close(self) -> bool:
        return self._level == DisasterLevel.EMERGENCY

    def record_ws_event(self) -> None:
        """Вызывать при каждом событии WebSocket."""
        self._last_ws_event_at = time.monotonic()

    def record_api_success(self) -> None:
        """Вызывать при успешном API запросе."""
        self._api_failures = 0

    def record_api_failure(self) -> None:
        """Вызывать при неудачном API запросе."""
        self._api_failures += 1

    def record_loss(self, loss_pct: Decimal) -> None:
        """Вызывать при каждом закрытии убыточной сделки."""
        self._consecutive_losses += 1
        self._daily_loss_pct += abs(loss_pct)

    def record_win(self) -> None:
        """Вызывать при каждом закрытии прибыльной сделки."""
        self._consecutive_losses = 0

    def reset_daily_stats(self) -> None:
        """Вызывать в начале каждого торгового дня."""
        self._daily_loss_pct = Decimal("0")

    async def check(
        self,
        spread_bps: float | None = None,
        liquidity_usdt: float | None = None,
        funding_rate: float | None = None,
        price_move_15m_pct: float | None = None,
    ) -> DisasterLevel:
        """
        Основная проверка — вызывается каждый цикл.
        Возвращает текущий уровень угрозы.
        """
        async with self._lock:
            prev_level = self._level

            # 1. Проверка API
            if self._api_failures >= self.config.api_max_consecutive_failures:
                await self._set_level(
                    DisasterLevel.CRITICAL,
                    DisasterReason.API_OUTAGE,
                    f"Binance API недоступен: {self._api_failures} подряд неудачных запросов",
                )
                return self._level

            # 2. Проверка WebSocket
            if self._last_ws_event_at is not None:
                ws_age = time.monotonic() - self._last_ws_event_at
                if ws_age > self.config.ws_stale_after_sec:
                    await self._set_level(
                        DisasterLevel.CRITICAL,
                        DisasterReason.WEBSOCKET_FREEZE,
                        f"WebSocket заморожен: нет событий {ws_age:.0f}с",
                    )
                    return self._level

            # 3. Проверка аномалий биржи
            if spread_bps is not None and spread_bps > self.config.max_spread_bps_disaster:
                await self._set_level(
                    DisasterLevel.WARNING,
                    DisasterReason.EXCHANGE_ANOMALY,
                    f"Аномальный спред: {spread_bps:.1f} bps > {self.config.max_spread_bps_disaster}",
                )

            if liquidity_usdt is not None and liquidity_usdt < self.config.min_liquidity_usdt:
                await self._set_level(
                    DisasterLevel.WARNING,
                    DisasterReason.EXCHANGE_ANOMALY,
                    f"Критически низкая ликвидность: ${liquidity_usdt:.0f}",
                )

            # 4. Проверка каскадных ликвидаций
            if funding_rate is not None and abs(funding_rate) > self.config.cascade_funding_rate_threshold:
                await self._set_level(
                    DisasterLevel.WARNING,
                    DisasterReason.LIQUIDATION_CASCADE,
                    f"Экстремальный funding rate: {funding_rate:.4f} — возможен каскад ликвидаций",
                )

            if price_move_15m_pct is not None and abs(price_move_15m_pct) > self.config.cascade_price_move_pct:
                await self._set_level(
                    DisasterLevel.CRITICAL,
                    DisasterReason.LIQUIDATION_CASCADE,
                    f"Резкое движение цены: {price_move_15m_pct:+.1f}% за 15 минут — каскад ликвидаций",
                )

            # 5. Проверка серии потерь
            if self._consecutive_losses >= self.config.max_consecutive_losses:
                await self._set_level(
                    DisasterLevel.CRITICAL,
                    DisasterReason.CONSECUTIVE_LOSSES,
                    f"Серия потерь: {self._consecutive_losses} убытков подряд",
                )

            if self._daily_loss_pct >= Decimal(str(self.config.max_daily_loss_pct)):
                await self._set_level(
                    DisasterLevel.EMERGENCY,
                    DisasterReason.CONSECUTIVE_LOSSES,
                    f"Дневной лимит потерь превышен: -{float(self._daily_loss_pct)*100:.1f}%",
                )

            # Автовосстановление
            if (
                self.config.auto_recover
                and self._level != DisasterLevel.NONE
                and self._level != DisasterLevel.EMERGENCY
                and self._disaster_since is not None
                and time.time() - self._disaster_since > self.config.recovery_cooldown_sec
                and self._api_failures == 0
                and self._consecutive_losses < self.config.max_consecutive_losses
            ):
                logger.info("Disaster mode: автоматическое восстановление")
                event = DisasterEvent(
                    level=DisasterLevel.NONE,
                    reason=DisasterReason.MANUAL,
                    message="Автовосстановление после cooldown периода",
                    auto_resolved=True,
                )
                self._events.append(event)
                self._level = DisasterLevel.NONE
                self._disaster_since = None

            if self._level != prev_level:
                logger.warning(
                    "Disaster level изменился: %s → %s",
                    prev_level.value, self._level.value,
                )

            return self._level

    async def force_emergency(self, reason: str) -> None:
        """Принудительный переход в EMERGENCY режим."""
        async with self._lock:
            await self._set_level(
                DisasterLevel.EMERGENCY,
                DisasterReason.MANUAL,
                f"Принудительный аварийный режим: {reason}",
            )

    async def recover(self) -> None:
        """Ручное восстановление из disaster mode."""
        async with self._lock:
            self._level = DisasterLevel.NONE
            self._api_failures = 0
            self._consecutive_losses = 0
            self._disaster_since = None
            logger.info("Disaster mode: ручное восстановление")

    async def _set_level(
        self,
        level: DisasterLevel,
        reason: DisasterReason,
        message: str,
    ) -> None:
        if _LEVEL_RANK[level] <= _LEVEL_RANK[self._level]:
            # Не понижаем уровень автоматически (только через recover())
            if level == self._level:
                return
            return

        event = DisasterEvent(level=level, reason=reason, message=message)
        self._events.append(event)
        self._level = level

        if self._disaster_since is None:
            self._disaster_since = time.time()

        log_msg = f"[DISASTER:{level.value.upper()}] {reason.value}: {message}"

        if level == DisasterLevel.EMERGENCY:
            logger.critical(log_msg)
            if self._emergency:
                await self._emergency(f"🚨 АВАРИЙНЫЙ РЕЖИМ: {message}")
        elif level == DisasterLevel.CRITICAL:
            logger.error(log_msg)
            if self._warn:
                await self._warn(f"🔴 КРИТИЧЕСКИЙ СБОЙ: {message}")
        elif level == DisasterLevel.WARNING:
            logger.warning(log_msg)
            if self._warn:
                await self._warn(f"⚠️ ПРЕДУПРЕЖДЕНИЕ: {message}")

    def summary(self) -> dict:
        """Сводка состояния для дашборда и логов."""
        return {
            "level": self._level.value,
            "blocks_entries": self.blocks_new_entries,
            "requires_close": self.requires_position_close,
            "api_failures": self._api_failures,
            "consecutive_losses": self._consecutive_losses,
            "daily_loss_pct": float(self._daily_loss_pct),
            "disaster_since": self._disaster_since,
            "ws_stale_sec": (
                time.monotonic() - self._last_ws_event_at
                if self._last_ws_event_at else None
            ),
            "recent_events": [
                {
                    "level": e.level.value,
                    "reason": e.reason.value,
                    "message": e.message,
                    "time": e.timestamp,
                }
                for e in self._events[-10:]
            ],
        }
