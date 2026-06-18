from __future__ import annotations

import pytest

import backtest_real


def test_legacy_backtest_real_is_disabled() -> None:
    with pytest.raises(SystemExit, match="Use walkforward.py"):
        backtest_real.main()
