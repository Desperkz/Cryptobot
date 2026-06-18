"""Legacy backtest entrypoint kept only as a deprecation guard.

The old implementation carried hand-written strategy copies that diverged from
the production strategy engine. Running it could produce false readiness
evidence. Use `walkforward.py`, which now uses production signals, 15m
execution candles, production exit profiles, and the shared realistic execution
model.
"""

from __future__ import annotations


def main() -> None:
    raise SystemExit(
        "backtest_real.py is deprecated and intentionally disabled. "
        "Use walkforward.py for production-aligned validation."
    )


if __name__ == "__main__":
    main()
