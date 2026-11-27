"""Collect one-minute public Binance candles through the original close."""
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, build_opener

WINDOWS = Path('/root/vwap_entry_research_20261127/windows.json')
BASE = 'https://fapi.binance.com/fapi/v1/klines'
SYMBOLS = {'BTCUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT', 'DOGEUSDT', 'LTCUSDT'}
MINUTE = 60000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise ValueError('REDIRECT_REFUSED')


def collect(requests):
    opener = build_opener(NoRedirect())
    rows = []
    if not 0 < len(requests) <= 100:
        raise ValueError('INVALID_WINDOW_COUNT')
    for window in requests:
        symbol, start, end = (window[key] for key in ('symbol', 'start_ms', 'end_ms'))
        if symbol not in SYMBOLS or not 0 < start <= end or start % MINUTE or end % MINUTE != MINUTE - 1:
            raise ValueError('INVALID_WINDOW')
        count = (end + 1 - start) // MINUTE
        if count > 480 or end >= int(time.time() * 1000):
            raise ValueError('INVALID_WINDOW_DURATION')
        query = urlencode({'symbol': symbol, 'interval': '1m', 'startTime': start, 'endTime': end, 'limit': count})
        for attempt in range(3):
            try:
                with opener.open(BASE + '?' + query, timeout=8) as response:
                    payload = response.read(256 * 1024 + 1)
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
        if len(payload) > 256 * 1024:
            raise ValueError('RESPONSE_TOO_LARGE')
        candles = json.loads(payload)
        if not isinstance(candles, list) or len(candles) != count:
            raise ValueError('INCOMPLETE_MINUTE_WINDOW')
        rows.append({**window, 'sha256': hashlib.sha256(payload).hexdigest(),
                     'bars': [bar[:7] for bar in candles]})
        time.sleep(.15)
    return rows


if __name__ == '__main__':
    raw = WINDOWS.read_bytes()
    windows = collect(json.loads(raw))
    print(json.dumps({'requests_sha256': hashlib.sha256(raw).hexdigest(),
                      'endpoint': BASE, 'windows': windows}, separators=(',', ':'), allow_nan=False))
