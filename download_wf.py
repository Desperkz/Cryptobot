import csv, os, time, requests

SYMBOLS = ["BTCUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ETHUSDT"]
INTERVALS = ["15m", "1h", "4h"]
START_MS = 1672531200000  # 2023-01-01
END_MS   = 1746057600000  # 2025-05-01
BASE_URL = "https://fapi.binance.com/fapi/v1/klines"

os.makedirs("data", exist_ok=True)

def download(symbol, interval, start_ms, end_ms, output):
    if os.path.exists(output):
        print(f"  Уже есть: {output}")
        return
    rows = []
    current = start_ms
    while current < end_ms:
        try:
            resp = requests.get(BASE_URL, params={
                "symbol": symbol, "interval": interval,
                "startTime": current, "endTime": end_ms, "limit": 1500,
            }, timeout=15)
            data = resp.json()
        except Exception as e:
            print(f"  Ошибка: {e}, жду 5 сек...")
            time.sleep(5)
            continue
        if not data or isinstance(data, dict):
            break
        rows.extend(data)
        current = data[-1][6] + 1
        time.sleep(0.1)
    with open(output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["open_time","open","high","low","close","volume","close_time","quote_volume"])
        for r in rows:
            w.writerow([r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7]])
    print(f"  Сохранено {len(rows)} баров -> {output}")

for sym in SYMBOLS:
    for tf in INTERVALS:
        out = f"data/{sym}_{tf}.csv"
        print(f"Качаю {sym} {tf}...")
        download(sym, tf, START_MS, END_MS, out)

print("Готово!")
