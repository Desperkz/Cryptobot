from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import typer
import uvicorn

from trading_bot.backtester.engine import BacktestEngine, load_candles_csv
from trading_bot.bot import TradingBot
from trading_bot.analytics import PerformanceAnalyzer, SelfLearningEngine
from trading_bot.config import ConfigError, load_config
from trading_bot.data_provider import BinanceUSDMClient
from trading_bot.logger import configure_logging
from trading_bot.models import SymbolFilters
from trading_bot.ml import MLSignalFilter
from trading_bot.risk_manager import RiskManager
from trading_bot.web_dashboard import create_app


app = typer.Typer(help="Safety-first Binance USD-M Futures trading bot.")


def _load() -> tuple:
    config = load_config()
    configure_logging(config.logging.level, config.logging.file)
    warnings = config.validate()
    return config, warnings


@app.command()
def status() -> None:
    """Show current configuration and safety status."""
    config, warnings = _load()
    payload = {
        "mode": config.mode.value,
        "mainnet_unlocked": config.safety.enable_mainnet_live
        and config.safety.mainnet_confirmation == config.safety.required_mainnet_confirmation,
        "emergency_stop_file": config.safety.emergency_stop_file,
        "emergency_stop": Path(config.safety.emergency_stop_file).exists(),
        "risk_per_trade_pct": str(config.risk.risk_per_trade_pct),
        "max_leverage": config.risk.max_leverage,
        "initial_equity_usdt": str(config.account.initial_equity_usdt) if config.account.initial_equity_usdt else None,
        "warnings": warnings,
    }
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


@app.command("check-keys")
def check_keys() -> None:
    """Check whether required keys are present without printing secrets."""
    config = load_config()
    keys = {
        "BINANCE_TESTNET_API_KEY": bool(config.secrets.binance_testnet_api_key),
        "BINANCE_TESTNET_API_SECRET": bool(config.secrets.binance_testnet_api_secret),
        "BINANCE_API_KEY": bool(config.secrets.binance_api_key),
        "BINANCE_API_SECRET": bool(config.secrets.binance_api_secret),
        "TELEGRAM_BOT_TOKEN": bool(config.secrets.telegram_bot_token),
        "TELEGRAM_CHAT_ID": bool(config.secrets.telegram_chat_id),
    }
    typer.echo(json.dumps(keys, indent=2, ensure_ascii=False))


@app.command("check-testnet")
def check_testnet() -> None:
    """Check Binance Futures testnet/demo REST availability."""
    config = load_config()

    async def _run() -> None:
        client = BinanceUSDMClient(
            base_url=config.exchange.testnet_base_url,
            api_key=config.secrets.binance_testnet_api_key,
            api_secret=config.secrets.binance_testnet_api_secret,
        )
        try:
            ping = await client.ping()
            server_time = await client.server_time()
            typer.echo(json.dumps({"ping": ping, "server_time": server_time}, indent=2))
        finally:
            await client.close()

    asyncio.run(_run())


@app.command("update-universe")
def update_universe() -> None:
    """Fetch top market-cap Binance USD-M tradable universe."""
    config, _ = _load()

    async def _run() -> None:
        bot = TradingBot(config)
        try:
            assets = await bot.universe.build(force_refresh=True)
            typer.echo(
                json.dumps(
                    [
                        {
                            "symbol": asset.symbol,
                            "rank": asset.market_cap_rank,
                            "volume_24h": str(asset.metrics.quote_volume_24h),
                            "spread_bps": str(asset.metrics.spread_bps),
                        }
                        for asset in assets
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        finally:
            await bot.binance.close()
            await bot.coingecko.close()
            await bot.telegram.close()

    asyncio.run(_run())


@app.command()
def start() -> None:
    """Run the trading loop. Starts in the configured mode."""
    config, _ = _load()
    bot = TradingBot(config)
    asyncio.run(bot.run_forever())


@app.command()
def position() -> None:
    """Show active position from Binance in live modes or local paper book."""
    config, _ = _load()

    async def _run() -> None:
        bot = TradingBot(config)
        try:
            await bot.db.connect()
            await bot._sync_paper_positions()
            positions = await bot.positions.active_positions()
            typer.echo(
                json.dumps(
                    [
                        {
                            "symbol": p.symbol,
                            "direction": p.direction.value,
                            "quantity": str(p.quantity),
                            "entry_price": str(p.entry_price),
                            "mark_price": str(p.mark_price) if p.mark_price else None,
                            "liquidation_price": str(p.liquidation_price) if p.liquidation_price else None,
                            "unrealized_pnl": str(p.unrealized_pnl),
                            "source": p.source,
                        }
                        for p in positions
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        finally:
            await bot.db.close()
            await bot.binance.close()
            await bot.coingecko.close()
            await bot.telegram.close()

    asyncio.run(_run())


@app.command()
def pnl() -> None:
    """Show realized PnL summary from local database."""
    config, _ = _load()

    async def _run() -> None:
        bot = TradingBot(config)
        try:
            await bot.db.connect()
            typer.echo(json.dumps(await bot.db.pnl_summary(), indent=2, ensure_ascii=False))
        finally:
            await bot.db.close()
            await bot.binance.close()
            await bot.coingecko.close()
            await bot.telegram.close()

    asyncio.run(_run())


@app.command()
def analytics(symbol: str | None = typer.Option(None, help="Optional symbol filter, e.g. BTCUSDT.")) -> None:
    """Show post-trade winrate, expectancy, R-multiple, profit factor, and drawdown."""
    config, _ = _load()

    async def _run() -> None:
        bot = TradingBot(config)
        try:
            await bot.db.connect()
            trades = await bot.db.recent_trades(10_000)
            snapshot = PerformanceAnalyzer().summarize(trades, symbol)
            typer.echo(json.dumps(snapshot.__dict__, indent=2, default=str, ensure_ascii=False))
        finally:
            await bot.db.close()
            await bot.binance.close()
            await bot.coingecko.close()
            await bot.telegram.close()

    asyncio.run(_run())


@app.command("self-learning")
def self_learning() -> None:
    """Analyze trade segments and print adaptive recommendations."""
    config, _ = _load()

    async def _run() -> None:
        bot = TradingBot(config)
        try:
            await bot.db.connect()
            trades = await bot.db.recent_trades(10_000)
            engine = SelfLearningEngine(config.analytics)
            payload = engine.adaptive_thresholds(trades)
            typer.echo(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
        finally:
            await bot.db.close()
            await bot.binance.close()
            await bot.coingecko.close()
            await bot.telegram.close()

    asyncio.run(_run())


@app.command("ml-retrain")
def ml_retrain() -> None:
    """Retrain the lightweight offline ML filter from trade history/features."""
    config, _ = _load()

    async def _run() -> None:
        bot = TradingBot(config)
        try:
            await bot.db.connect()
            trades = await bot.db.recent_trades(10_000)
            model = MLSignalFilter(
                model_path=config.ml.model_path,
                min_confidence=config.ml.min_prediction_confidence,
                enabled=True,
                training_data_path=config.ml.training_data_path,
                retrain_min_trades=config.ml.retrain_min_trades,
                decision_min_trades=config.ml.decision_min_trades,
            )
            result = model.retrain_from_trades(trades)
            if not result.get("trained"):
                result = model.retrain_from_history()
            typer.echo(json.dumps(result, indent=2, ensure_ascii=False))
        finally:
            await bot.db.close()
            await bot.binance.close()
            await bot.coingecko.close()
            await bot.telegram.close()

    asyncio.run(_run())


@app.command("ml-validate")
def ml_validate(
    output: str = typer.Option("", help="Where to write the JSON report."),
    test_window: int = typer.Option(50, help="Number of future trades per validation fold."),
) -> None:
    """Run walk-forward validation for the offline ML filter."""
    config, _ = _load()

    async def _run() -> None:
        bot = TradingBot(config)
        try:
            await bot.db.connect()
            trades = await bot.db.recent_trades(10_000)
            model = MLSignalFilter(
                model_path=config.ml.model_path,
                min_confidence=config.ml.min_prediction_confidence,
                enabled=True,
                training_data_path=config.ml.training_data_path,
                retrain_min_trades=config.ml.retrain_min_trades,
                decision_min_trades=config.ml.decision_min_trades,
            )
            report = model.walk_forward_validate_from_trades(
                trades,
                min_train_rows=config.ml.retrain_min_trades,
                test_window=test_window,
            )
            output_path = Path(output or config.ml.validation_report_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
        finally:
            await bot.db.close()
            await bot.binance.close()
            await bot.coingecko.close()
            await bot.telegram.close()

    asyncio.run(_run())


@app.command("emergency-stop")
def emergency_stop() -> None:
    """Activate emergency stop. The bot will stop opening new trades."""
    config = load_config()
    bot = TradingBot(config)
    bot.activate_emergency_stop()
    typer.echo(f"Emergency stop activated: {config.safety.emergency_stop_file}")


@app.command()
def backtest(
    symbol: str = typer.Option(..., help="Symbol, e.g. BTCUSDT."),
    csv: Path = typer.Option(..., help="CSV with open_time,open,high,low,close,volume."),
    equity: float | None = typer.Option(None, help="USDT equity override."),
) -> None:
    """Run a simple local CSV backtest smoke test."""
    config, _ = _load()
    starting_equity = equity or config.account.initial_equity_usdt
    if starting_equity is None:
        raise typer.BadParameter("Set --equity, STARTING_DEPOSIT_USDT, or MANUAL_TENGE_USDT_RATE.")
    filters = SymbolFilters(
        symbol=symbol,
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )
    candles = load_candles_csv(csv)
    result = BacktestEngine(RiskManager(config.risk), filters).run_naive_signal_backtest(
        candles,
        starting_equity,
        min_trades=config.trading.min_backtest_trades,
        min_profit_factor=config.trading.min_backtest_profit_factor,
        min_max_drawdown_pct=config.trading.min_backtest_max_drawdown_pct,
    )
    typer.echo(json.dumps(result.__dict__, indent=2, default=str, ensure_ascii=False))


@app.command()
def serve(host: str = "", port: int = 0) -> None:
    """Run FastAPI dashboard."""
    config, _ = _load()
    host = host or config.web.host
    port = port or config.web.port
    uvicorn.run(create_app(config), host=host, port=port)


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from exc


if __name__ == "__main__":
    main()
