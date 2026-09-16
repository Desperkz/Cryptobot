"""Deterministic minute-OHLC paper execution, independent of exchange orders."""
from __future__ import annotations


def simulate(bars, stop, direction, targets, *, complete=False, funding_rate=None):
    """One unit of initial stop risk; pessimistic intrabar sequencing.

    Entry is the first minute's open plus 5bps adverse slippage. Both sides pay
    4bps; exits incur 5bps. Funding uses the entry-time signed 8h rate continuously,
    or an explicitly adverse 1bp/8h buffer when unavailable. No fee tiers assumed.
    An incomplete timeout is marked OPEN, never persisted as realized profit.
    """
    if not bars:
        return {"status": "PENDING"}
    sign = 1 if direction == "LONG" else -1
    raw = float(bars[0][1]); entry = raw * (1 + sign * .0005)
    risk = sign * (entry - stop)
    if risk <= 0:
        return {"status": "INVALID_ENTRY", "reason": "stop_wrong_side_at_entry"}
    qty = 1 / risk; remaining = 1.; gross = fees = slip = funding = 0.
    pending = [{"p": entry + sign * risk * t[0], "f": t[1], "be": t[2], "tr": t[3], "done": False} for t in targets]
    trail = False; ambiguous = 0; entry_ms = int(bars[0][0]); exit_ms = entry_ms
    funding_used = sign * float(funding_rate) if funding_rate is not None else .0001

    def close(price, fraction, when):
        nonlocal remaining, gross, fees, slip, funding, exit_ms
        effective = price * (1 - sign * .0005); q = qty * fraction
        gross += sign * (price - entry) * q
        fees += (entry + effective) * q * .0004
        slip += abs(price - effective) * q
        funding += entry * q * funding_used * max(when - entry_ms, 0) / 28800000
        remaining = max(0., remaining - fraction); exit_ms = when

    reason = "timeout"
    for b in bars:
        o, h, l, c = map(float, b[1:5]); when = int(b[6])
        hit_stop = lambda p: l <= p if sign == 1 else h >= p
        hit_target = lambda p: h >= p if sign == 1 else l <= p
        if hit_stop(stop):
            ambiguous += int(any(hit_target(t['p']) for t in pending if not t['done']))
            close(min(stop, o) if sign == 1 else max(stop, o), remaining, when)
            reason = "stop"; break
        for t in pending:
            if t['done'] or not hit_target(t['p']):
                continue
            close(t['p'], min(t['f'], remaining), when); t['done'] = True
            if remaining <= 1e-12:
                reason = "targets"; break
            if t['be']:
                be = entry * (1 + sign * .0002)
                stop = max(stop, be) if sign == 1 else min(stop, be)
            if t['tr']:
                trail = True
            if hit_stop(stop):
                ambiguous += 1; close(stop, remaining, when); reason = "raised_stop"; break
        if remaining <= 1e-12:
            break
        if trail:
            candidate = h * .996 if sign == 1 else l * 1.004
            stop = max(stop, candidate) if sign == 1 else min(stop, candidate)
            if hit_stop(stop):
                ambiguous += 1; close(stop, remaining, when); reason = "trail"; break
    still_open = remaining > 1e-12 and not complete
    if remaining > 1e-12:
        close(float(bars[-1][4]), remaining, int(bars[-1][6]))
    return {"status": "OPEN" if still_open else "CLOSED", "reason": "mark_to_market" if still_open else reason,
            "R": gross - fees - slip - funding, "gross_R": gross, "fees_R": fees,
            "exit_slippage_R": slip, "entry_slippage_R": abs(entry-raw)*qty,
            "funding_R": funding, "funding_source": "entry_rate_continuous_estimate" if funding_rate is not None else "adverse_buffer",
            "entry_price": entry, "entry_ms": entry_ms, "last_ms": exit_ms, "ambiguous_bars": ambiguous}
