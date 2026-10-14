import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from trading_bot.bot import TradingBot
from trading_bot.config import Secrets, load_config
from trading_bot.models import TradingMode

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('mode', [TradingMode.PAPER_TRADING, TradingMode.DRY_RUN, TradingMode.BACKTEST])
def test_non_live_bot_uses_public_mainnet_without_credentials(mode, tmp_path):
    cfg = load_config(ROOT / 'config.yaml', env_path=tmp_path / 'absent.env')
    cfg = replace(cfg, mode=mode, secrets=Secrets(
        binance_api_key='sentinel-mainnet-key', binance_api_secret='sentinel-mainnet-secret',
        binance_testnet_api_key='sentinel-demo-key', binance_testnet_api_secret='sentinel-demo-secret',
    ))

    async def check():
        bot = TradingBot(cfg)
        try:
            assert bot.binance.base_url == 'https://fapi.binance.com'
            assert bot.binance.api_key is None
            assert bot.binance.api_secret is None
        finally:
            await bot.stop()
    asyncio.run(check())


@pytest.mark.parametrize('mode', [TradingMode.TESTNET_LIVE, TradingMode.MAINNET_LIVE])
def test_live_market_data_stays_on_its_execution_venue(mode, tmp_path):
    cfg = replace(load_config(ROOT / 'config.yaml', env_path=tmp_path / 'absent.env'), mode=mode)
    assert cfg.market_data_base_url == cfg.rest_base_url


def test_mainnet_cutover_has_new_research_cohorts_and_epoch(tmp_path):
    cfg = load_config(ROOT / 'config.yaml', env_path=tmp_path / 'absent.env')
    assert cfg.analytics.stats_epoch.startswith('2026-10-14T')
    for field in ('shadow_revalidation_cohort', 'shadow_gate_counterfactual_cohort',
                  'shadow_parallel_lab_cohort', 'shadow_conditional_lab_cohort',
                  'shadow_conditional_lab_v2_cohort', 'p8_shadow_cohort'):
        assert '2026-10-14-mainnet' in getattr(cfg.strategy, field)
    assert cfg.mode == TradingMode.PAPER_TRADING
    assert not cfg.safety.enable_mainnet_live
