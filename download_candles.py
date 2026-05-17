"""
Скачивает свечи для нескольких монет и таймфреймов.
Сохраняет в папку data/.

Использование:
    python download_candles.py
"""

import csv
import os
import time

import requests

SYMBOLS = ["ETHUSDT", "SOLUSDT", "BNBUSDT"]  # BTC уже есть
INTERVALS = ["15m", "1h", "4h"]

START_MS = 1672531200000  # 2023-01-01
END_MS   = 1735689600000  # 2025-01-01

BASE_URL = "https://fapi.binance.com/fapi/v1/klines"


def download(symbol: str, interval: str, start_ms: int, end_ms: int, output: str) -> None:
    if os.path.exists(output):
        print(f"  Уже есть: {output} — пропускаю")
        return

    rows = []
    current = start_ms
    while current < end_ms:
        try:
            resp = requests.get(BASE_URL, params={
                "symbol": symbol,
                "interval": interval,
                "startTime": current,
                "endTime": end_ms,
                "limit": 1500,
            }, timeout=15)
            data = resp.json()
        except Exception as e:
            print(f"  Ошибка запроса: {e}, жду 5 сек...")
            time.sleep(5)
            continue

        if not data or isinstance(data, dict):
            break

        rows.extend(data)
        current = data[-1][6] + 1  # close_time + 1ms
        time.sleep(0.15)

    os.makedirs(os.path.dirname(output) if os.path.dirname(output) else ".", exist_ok=True)
    with open(output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["open_time", "open", "high", "low", "close", "volume",
                    "close_time", "quote_volume", "trades",
                    "taker_buy_base", "taker_buy_quote", "ignore"])
        w.writerows(rows)

    print(f"  ✅ {output} ({len(rows)} свечей)")


def main() -> None:
    print("Скачиваю свечи...\n")
    for symbol in SYMBOLS:
        print(f"[ {symbol} ]")
        for interval in INTERVALS:
            output = f"data/{symbol}_{interval}.csv"
            download(symbol, interval, START_MS, END_MS, output)
        print()
    print("Готово! Теперь запускай: python backtest_multi.py")


if __name__ == "__main__":
    main()
