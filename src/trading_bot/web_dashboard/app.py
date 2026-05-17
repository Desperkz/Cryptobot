from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from trading_bot.bot import TradingBot
from trading_bot.config import AppConfig
from trading_bot.models import TradingMode


def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI(title="Binance USD-M Safety Bot")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return DASHBOARD_HTML

    @app.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        _require_authorized(request)
        bot = TradingBot(config)
        try:
            config_warnings = config.validate()
        except Exception as exc:
            config_warnings = [str(exc)]
        api_status = "unknown"
        try:
            await bot.binance.ping()
            api_status = "ok"
        except Exception as exc:
            api_status = f"error: {exc}"
        finally:
            await bot.binance.close()
            await bot.coingecko.close()
            await bot.telegram.close()
        return {
            "mode": config.mode.value,
            "mainnet_unlocked": config.safety.enable_mainnet_live
            and config.safety.mainnet_confirmation == config.safety.required_mainnet_confirmation,
            "emergency_stop": bot.emergency_stop_active(),
            "binance_api_status": api_status,
            "warnings": config_warnings,
            "log_tail": _tail(config.logging.file),
        }

    @app.post("/api/emergency-stop")
    async def emergency_stop(request: Request) -> dict[str, str]:
        _require_authorized(request)
        bot = TradingBot(config)
        bot.activate_emergency_stop()
        await bot.telegram.risk_warning("Emergency stop activated from dashboard.")
        await bot.binance.close()
        await bot.coingecko.close()
        await bot.telegram.close()
        return {"status": "emergency_stop_active"}

    @app.delete("/api/emergency-stop")
    async def clear_emergency_stop(request: Request) -> dict[str, str]:
        _require_authorized(request)
        path = Path(config.safety.emergency_stop_file)
        if path.exists():
            path.unlink()
        return {"status": "emergency_stop_cleared"}

    @app.post("/api/mode")
    async def request_mode(request: Request, mode: str = Form(...)) -> dict[str, str]:
        _require_authorized(request)
        requested = TradingMode(mode)
        if requested == TradingMode.MAINNET_LIVE:
            raise HTTPException(status_code=403, detail="MAINNET_LIVE cannot be enabled from dashboard.")
        if requested not in {TradingMode.DRY_RUN, TradingMode.TESTNET_LIVE}:
            raise HTTPException(status_code=400, detail="Dashboard only supports DRY_RUN / TESTNET_LIVE request.")
        path = Path("data/requested_mode.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(requested.value, encoding="utf-8")
        return {"status": "mode_change_requested", "mode": requested.value, "note": "Edit config.yaml and restart to apply."}

    return app


def _tail(path: str, lines: int = 80) -> list[str]:
    log_path = Path(path)
    if not log_path.exists():
        return []
    return log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-lines:]


def _require_authorized(request: Request) -> None:
    token = os.getenv("BOT_CONTROL_TOKEN", "")
    if not token:
        return
    auth = request.headers.get("Authorization", "")
    header_token = request.headers.get("X-Bot-Control-Token", "")
    bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    query_token = request.query_params.get("token", "")
    if any(secrets.compare_digest(candidate, token) for candidate in (bearer, header_token, query_token) if candidate):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Binance USD-M Safety Bot</title>
  <style>
    :root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; }
    body { margin: 0; background: #f7f7f2; color: #1c2520; }
    header { padding: 24px 32px; background: #19302b; color: #f7f7f2; }
    main { padding: 24px 32px; display: grid; gap: 18px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
    section { border: 1px solid #cdd3c7; border-radius: 8px; padding: 18px; background: #ffffff; }
    h1 { margin: 0; font-size: 24px; }
    h2 { margin: 0 0 12px; font-size: 16px; }
    button, select { min-height: 38px; border-radius: 6px; border: 1px solid #879084; padding: 0 12px; background: #ffffff; color: #1c2520; }
    button.danger { background: #b42318; color: white; border-color: #b42318; }
    pre { max-height: 360px; overflow: auto; white-space: pre-wrap; font-size: 12px; }
    .metric { display: flex; justify-content: space-between; border-bottom: 1px solid #edf0e9; padding: 8px 0; gap: 16px; }
    .warn { color: #9a3412; }
  </style>
</head>
<body>
  <header><h1>Binance USD-M Safety Bot</h1></header>
  <main>
    <section>
      <h2>Status</h2>
      <div id="status"></div>
    </section>
    <section>
      <h2>Controls</h2>
      <p><button class="danger" onclick="emergencyStop()">Emergency stop</button></p>
      <form onsubmit="requestMode(event)">
        <select name="mode">
          <option value="DRY_RUN">DRY_RUN</option>
          <option value="TESTNET_LIVE">TESTNET_LIVE</option>
        </select>
        <button type="submit">Request mode</button>
      </form>
    </section>
    <section>
      <h2>Warnings</h2>
      <pre id="warnings"></pre>
    </section>
    <section>
      <h2>Logs</h2>
      <pre id="logs"></pre>
    </section>
  </main>
  <script>
    function authHeaders() {
      const token = window.localStorage.getItem('botControlToken') || '';
      return token ? { 'X-Bot-Control-Token': token } : {};
    }
    async function apiFetch(url, options = {}) {
      const headers = { ...(options.headers || {}), ...authHeaders() };
      const res = await fetch(url, { ...options, headers });
      if (res.status !== 401) return res;
      const token = window.prompt('Control token');
      if (!token) return res;
      window.localStorage.setItem('botControlToken', token);
      return fetch(url, { ...options, headers: { ...(options.headers || {}), ...authHeaders() } });
    }
    async function loadStatus() {
      const res = await apiFetch('/api/status');
      const data = await res.json();
      document.getElementById('status').innerHTML = [
        ['Mode', data.mode],
        ['Emergency stop', data.emergency_stop],
        ['Binance API', data.binance_api_status],
        ['Mainnet unlocked', data.mainnet_unlocked]
      ].map(([k,v]) => `<div class="metric"><strong>${k}</strong><span>${v}</span></div>`).join('');
      document.getElementById('warnings').textContent = (data.warnings || []).join('\\n');
      document.getElementById('logs').textContent = (data.log_tail || []).join('\\n');
    }
    async function emergencyStop() {
      await apiFetch('/api/emergency-stop', { method: 'POST' });
      await loadStatus();
    }
    async function requestMode(event) {
      event.preventDefault();
      const body = new FormData(event.target);
      await apiFetch('/api/mode', { method: 'POST', body });
      await loadStatus();
    }
    loadStatus();
    setInterval(loadStatus, 10000);
  </script>
</body>
</html>
"""
