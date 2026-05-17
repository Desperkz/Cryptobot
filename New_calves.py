import requests, csv, time

def download_klines(symbol, interval, start_ms, end_ms, output_file):
    url = "https://fapi.binance.com/fapi/v1/klines"
    rows = []
    current = start_ms
    while current < end_ms:
        resp = requests.get(url, params={
            "symbol": symbol,
            "interval": interval,
            "startTime": current,
            "endTime": end_ms,
            "limit": 1500
        })
        data = resp.json()
        if not data:
            break
        rows.extend(data)
        current = data[-1][6] + 1
        time.sleep(0.1)

    with open(output_file, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time","open","high","low","close","volume",
                    "close_time","quote_volume","trades",
                    "taker_buy_base","taker_buy_quote","ignore"])
        w.writerows(rows)
    print(f"Готово: {output_file} ({len(rows)} свечей)")

start = 1672531200000  # 2023-01-01
end   = 1735689600000  # 2025-01-01

download_klines("BTCUSDT", "1h",  start, end, "data/BTCUSDT_1h.csv")
download_klines("BTCUSDT", "4h",  start, end, "data/BTCUSDT_4h.csv")