"""Causal SQZ hypotheses for offline research, with no order capability."""
from __future__ import annotations
import math

HOUR = 3_600_000


def features(rows):
    """Full causal hourly EMA histories; each feature is available only at close."""
    result = []
    fast = slow = atr = None
    for i, row in enumerate(rows):
        o, high, low, close = (float(row[j]) for j in (1, 2, 3, 4))
        if (int(row[0]) % HOUR or int(row[6]) != int(row[0])+HOUR-1
                or not all(math.isfinite(x) for x in (o, high, low, close))
                or not 0 < low <= min(o, close) <= max(o, close) <= high
                or (i and int(row[0])-int(rows[i-1][0]) != HOUR)):
            raise ValueError('INVALID_OR_GAPPED_HOURLY_HISTORY')
        tr = max(high-low, abs(high-float(rows[i-1][4])), abs(low-float(rows[i-1][4]))) if i else high-low
        fast = close if fast is None else fast+(close-fast)*2/51
        slow = close if slow is None else slow+(close-slow)*2/201
        atr = tr if atr is None else atr+(tr-atr)*2/15
        result.append({'available_ms': int(row[6])+1, 'close': close,
                       'ema50': fast, 'ema200': slow, 'atr14': atr,
                       'slope24': fast-result[i-24]['ema50'] if i >= 24 else None,
                       'return24': close/float(rows[i-24][4])-1 if i >= 24 else None,
                       'warm': i >= 499})
    return result


def decide(coin, btc, direction, symbol):
    """Pre-fixed trend/relative-strength gate; has no outcome or future inputs."""
    if direction not in ('LONG', 'SHORT'):
        raise ValueError('INVALID_DIRECTION')
    sign = 1 if direction == 'LONG' else -1
    if not coin['warm'] or not btc['warm']:
        return {'accepted': False, 'regime': 'UNKNOWN', 'reasons': ['WARMUP']}
    gap = coin['ema50']-coin['ema200']
    regime = 'RANGE' if abs(gap) < .5*coin['atr14'] else ('UP' if gap > 0 else 'DOWN')
    conditions = {'COIN_TREND': sign*gap >= .5*coin['atr14'] and sign*coin['slope24'] > 0,
                  'BTC_ALIGNMENT': sign*(btc['close']-btc['ema200']) > 0,
                  'RELATIVE_STRENGTH': symbol == 'BTCUSDT' or sign*(coin['return24']-btc['return24']) >= 0}
    return {'accepted': all(conditions.values()), 'regime': regime,
            'reasons': [key for key, value in conditions.items() if not value],
            'coin_feature_ms': coin['available_ms'], 'btc_feature_ms': btc['available_ms']}


def structure_stop(hourly, direction, previous_stop):
    """Five COMPLETED hourly bars; caller activates only after the 1.6R partial."""
    if len(hourly) != 5 or direction not in ('LONG', 'SHORT'):
        raise ValueError('INVALID_STRUCTURE_WINDOW')
    level = min(float(x[3]) for x in hourly) if direction == 'LONG' else max(float(x[2]) for x in hourly)
    return max(previous_stop, level) if direction == 'LONG' else min(previous_stop, level)
