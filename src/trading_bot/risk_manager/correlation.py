"""
Correlation filter — блокирует вход если новый сигнал сильно коррелирует
с уже открытой позицией. Защищает от двойного риска на связанных монетах.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from trading_bot.models import Candle, Direction, Position

logger = logging.getLogger(__name__)


@dataclass
class CorrelationFilter:
    threshold: float = 0.7        # корреляция выше — блокируем
    lookback: int = 48             # баров 1h = 2 суток
    _cache: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def update(self, symbol: str, candles_1h: list[Candle]) -> None:
        """Обновляем кэш доходностей для символа."""
        closes = [float(c.close) for c in candles_1h[-self.lookback - 1:]]
        if len(closes) < 2:
            return
        returns = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
        ]
        self._cache[symbol] = returns[-self.lookback:]

    def correlation(self, sym_a: str, sym_b: str) -> float | None:
        """Корреляция Пирсона между двумя символами (sample, n-1)."""
        a = self._cache.get(sym_a)
        b = self._cache.get(sym_b)
        if not a or not b:
            return None
        n = min(len(a), len(b))
        if n < 10:
            return None
        a, b = a[-n:], b[-n:]
        mean_a = sum(a) / n
        mean_b = sum(b) / n
        cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
        # Sample std (n-1) — более точная оценка при конечной выборке
        std_a = (sum((x - mean_a) ** 2 for x in a) / (n - 1)) ** 0.5
        std_b = (sum((x - mean_b) ** 2 for x in b) / (n - 1)) ** 0.5
        if std_a == 0 or std_b == 0:
            return None
        # Коэффициент Пирсона: cov / ((n-1) * std_a * std_b)
        return cov / ((n - 1) * std_a * std_b)

    def allow_entry(
        self,
        symbol: str,
        direction: Direction,
        active_positions: list[Position],
    ) -> tuple[bool, str]:
        """
        Проверяем новый сигнал против открытых позиций.
        Возвращает (allowed, reason).
        """
        for pos in active_positions:
            if pos.symbol == symbol:
                continue
            corr = self.correlation(symbol, pos.symbol)
            if corr is None:
                continue
            # Высокая положительная корреляция + одинаковое направление = двойной риск
            if corr >= self.threshold and pos.direction == direction:
                reason = (
                    f"Corr({symbol},{pos.symbol})={corr:.2f} >= {self.threshold} "
                    f"— same direction {direction.value}, entry blocked."
                )
                logger.info("Correlation filter: %s", reason)
                return False, reason
            # Высокая отрицательная корреляция + противоположное направление = тот же риск
            if corr <= -self.threshold and pos.direction != direction:
                reason = (
                    f"Corr({symbol},{pos.symbol})={corr:.2f} <= -{self.threshold} "
                    f"— opposite direction hedge risk, entry blocked."
                )
                logger.info("Correlation filter: %s", reason)
                return False, reason
        return True, ""
