"""API для управления ботом и получения данных для дашборда."""
import http.server
import json
import subprocess
import sqlite3
import urllib.request
from datetime import datetime, timezone

PORT = 8889
DB_PATH = "/root/bot/data/trading_bot.sqlite3"
LOG_PATH = "/root/bot/logs/trading_bot.log"
BINANCE_URL = "https://fapi.binance.com/fapi/v1/ticker/price?symbol="


def get_bot_status():
    r = subprocess.run(["systemctl", "is-active", "trading-bot"], capture_output=True, text=True)
    return r.stdout.strip()


def get_current_price(symbol):
    try:
        url = BINANCE_URL + symbol
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
            return float(data["price"])
    except:
        return None


def get_open_positions():
    """Читает открытые позиции из БД и обогащает текущими ценами."""
    positions = []
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            SELECT * FROM trades
            WHERE status IN ('ACCEPTED', 'OPEN', 'ACTIVE')
            ORDER BY id DESC
        """)
        rows = cur.fetchall()
        conn.close()

        for row in rows:
            t = dict(row)
            symbol = t.get("symbol", "")
            entry = float(t.get("entry_price") or 0)
            sl = float(t.get("stop_loss") or 0)
            tp = float(t.get("take_profit") or 0)
            qty = float(t.get("quantity") or 0)
            direction = t.get("direction", "")

            # Метаданные
            meta = {}
            try:
                meta = json.loads(t.get("extra_data") or t.get("metadata") or "{}")
                if isinstance(meta, dict) and "signal_metadata" in meta:
                    meta = meta["signal_metadata"]
            except:
                pass

            # Текущая цена
            current_price = get_current_price(symbol)

            # Считаем PnL
            unrealized_pnl = None
            pnl_pct = None
            if current_price and entry and qty:
                if direction == "LONG":
                    unrealized_pnl = (current_price - entry) * qty
                else:
                    unrealized_pnl = (entry - current_price) * qty
                if entry > 0:
                    pnl_pct = unrealized_pnl / (entry * qty) * 100

            # Расстояние до SL и TP в %
            dist_sl = None
            dist_tp = None
            if current_price and sl and tp and entry:
                if direction == "LONG":
                    dist_sl = (current_price - sl) / entry * 100
                    dist_tp = (tp - current_price) / entry * 100
                else:
                    dist_sl = (sl - current_price) / entry * 100
                    dist_tp = (current_price - tp) / entry * 100

            # Время в позиции
            duration_min = None
            created = t.get("created_at")
            if created:
                try:
                    dt = datetime.fromisoformat(created)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    duration_min = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
                except:
                    pass

            # Плечо из метаданных или дефолт
            leverage = meta.get("leverage", 3)

            # Размер позиции в USDT
            position_size = entry * qty if entry and qty else 0
            margin_used = position_size / leverage if leverage else position_size

            positions.append({
                "id": t.get("id"),
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry,
                "current_price": current_price,
                "stop_loss": sl,
                "take_profit": tp,
                "quantity": qty,
                "position_size_usdt": round(position_size, 2),
                "margin_used": round(margin_used, 2),
                "leverage": leverage,
                "unrealized_pnl": round(unrealized_pnl, 4) if unrealized_pnl is not None else None,
                "pnl_pct": round(pnl_pct, 3) if pnl_pct is not None else None,
                "dist_to_sl_pct": round(dist_sl, 3) if dist_sl is not None else None,
                "dist_to_tp_pct": round(dist_tp, 3) if dist_tp is not None else None,
                "duration_minutes": duration_min,
                "status": t.get("status"),
                "created_at": created,
                "regime": meta.get("regime", "—"),
                "deviation_atr": meta.get("deviation_atr", "—"),
                "rsi": meta.get("rsi", "—"),
                "divergence": meta.get("divergence", "no"),
                "confidence": meta.get("confidence", "—"),
                "mode": t.get("mode", "PAPER_TRADING"),
            })
    except Exception as e:
        positions.append({"error": str(e)})

    return positions


def get_closed_trades(limit=100):
    """Закрытые сделки с полными деталями."""
    trades = []
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            SELECT * FROM trades
            WHERE status = 'CLOSED' OR realized_pnl != 0
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        trades = [dict(r) for r in rows]
    except Exception as e:
        trades = [{"error": str(e)}]
    return trades


def get_recent_logs(n=300):
    events = []
    seen = set()
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        recent = lines[-n:]
        for line in recent:
            line = line.strip()
            if not line:
                continue

            # Дедупликация
            key = line[20:] if len(line) > 20 else line
            if key in seen:
                continue
            seen.add(key)

            parts = line.split(" ", 3)
            if len(parts) < 4:
                continue
            timestamp = parts[0] + " " + parts[1]
            message = parts[3] if len(parts) > 3 else ""

            if any(x in message for x in ["httpcore", "httpx", "aiosqlite", "HTTP Request",
                                           "receive_", "send_request", "response_", "response_body"]):
                continue

            event_type = "info"
            symbol = ""
            decision = ""
            reason = ""

            if "Universe:" in message:
                event_type = "universe"
                try:
                    symbols_str = message.split("Universe:")[1].strip()
                    decision = "Вселенная обновлена"
                    reason = symbols_str[:120]
                except:
                    pass

            elif "Skipping" in message and "spread_bps" in message:
                event_type = "skip"
                try:
                    sym = message.split("Skipping")[1].strip().split(":")[0].strip()
                    symbol = sym
                    spread = message.split("spread_bps=")[1].split(" ")[0][:8]
                    decision = "Пропущен"
                    reason = f"Спред {spread} bps > 8"
                except:
                    pass

            elif "Risk rejected" in message:
                event_type = "rejected"
                try:
                    sym = message.split("Risk rejected")[1].strip().split(":")[0].strip()
                    symbol = sym
                    reason_text = ":".join(message.split(":")[2:]).strip() if message.count(":") >= 2 else ""
                    decision = "Отклонён (риск)"
                    reason = reason_text[:100]
                except:
                    pass

            elif "Entry filter rejected" in message:
                event_type = "filtered"
                try:
                    sym = message.split("Entry filter rejected")[1].strip().split(":")[0].strip()
                    symbol = sym
                    decision = "Фильтр входа"
                    reason = ":".join(message.split(":")[2:]).strip()[:100]
                except:
                    pass

            elif "Trade accepted" in message:
                event_type = "trade"
                decision = "Сделка открыта"
                reason = message.split("Trade accepted:")[-1].strip()[:100]

            elif "Active" in message and "position exists" in message:
                event_type = "skip"
                try:
                    sym = message.split("Active")[1].strip().split(" ")[0]
                    symbol = sym
                    decision = "Уже в позиции"
                    reason = "Пропуск — позиция открыта"
                except:
                    pass

            elif "signal" in message.lower() and "MEAN_REVERSION" in message:
                event_type = "signal"
                decision = "Сигнал найден"
                try:
                    if "symbol" in message:
                        symbol = message.split("symbol")[1].split("'")[1] if "'" in message else ""
                    reason = "MEAN_REVERSION"
                except:
                    pass

            else:
                continue

            events.append({
                "time": timestamp[11:19],
                "type": event_type,
                "symbol": symbol,
                "decision": decision,
                "reason": reason,
            })

    except Exception as e:
        events.append({"time": "—", "type": "error", "symbol": "", "decision": "Ошибка лога", "reason": str(e)})

    return list(reversed(events[-60:]))


def get_stats():
    """Быстрая сводка из БД."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        open_c = conn.execute("SELECT COUNT(*) FROM trades WHERE status IN ('ACCEPTED','OPEN','ACTIVE')").fetchone()[0]
        closed = conn.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED' OR realized_pnl != 0").fetchone()[0]
        wins = conn.execute("SELECT COUNT(*) FROM trades WHERE realized_pnl > 0").fetchone()[0]
        total_pnl = conn.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM trades WHERE status='CLOSED' OR realized_pnl != 0").fetchone()[0]
        signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        conn.close()
        return {
            "total_trades": total,
            "open_trades": open_c,
            "closed_trades": closed,
            "wins": wins,
            "losses": closed - wins,
            "winrate": round(wins / closed * 100, 1) if closed > 0 else 0,
            "total_pnl": round(float(total_pnl), 4),
            "signals": signals,
            "bot_status": get_bot_status(),
        }
    except Exception as e:
        return {"error": str(e)}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        path = self.path.split("?")[0]

        if path == "/status":
            self.wfile.write(json.dumps({"status": get_bot_status()}).encode())

        elif path == "/start":
            subprocess.run(["systemctl", "start", "trading-bot"])
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif path == "/stop":
            subprocess.run(["systemctl", "stop", "trading-bot"])
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif path == "/restart":
            subprocess.run(["systemctl", "restart", "trading-bot"])
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif path == "/positions":
            self.wfile.write(json.dumps({"positions": get_open_positions()}).encode())

        elif path == "/trades":
            self.wfile.write(json.dumps({"trades": get_closed_trades()}).encode())

        elif path == "/logs":
            self.wfile.write(json.dumps({"events": get_recent_logs()}).encode())

        elif path == "/stats":
            self.wfile.write(json.dumps(get_stats()).encode())

        elif path == "/config":
            try:
                import yaml
                with open("/root/bot/config.yaml", "r") as f:
                    cfg = yaml.safe_load(f)
                deposit = cfg.get("account", {}).get("starting_deposit_usdt") or 1000
                self.wfile.write(json.dumps({"starting_deposit_usdt": float(deposit)}).encode())
            except Exception as e:
                self.wfile.write(json.dumps({"starting_deposit_usdt": 1000, "error": str(e)}).encode())

        else:
            self.wfile.write(json.dumps({"error": "unknown"}).encode())

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    with http.server.HTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Bot Control API running on port {PORT}")
        httpd.serve_forever()
