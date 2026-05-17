#!/usr/bin/env python3
"""Простой HTTP сервер для отдачи базы данных дашборду."""
import http.server
import os

PORT = 8888
DB_PATH = "/root/bot/data/trading_bot.sqlite3"

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/db":
            if os.path.exists(DB_PATH):
                data = open(DB_PATH, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", len(data))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # отключаем логи

if __name__ == "__main__":
    with http.server.HTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Serving on port {PORT}")
        httpd.serve_forever()
