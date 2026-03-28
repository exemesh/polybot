#!/usr/bin/env python3
"""
PolyBot Control API — port 8766
Exposed via Cloudflare tunnel at https://api.exemesh.dev

Endpoints:
  GET  /status          — bot health, balance, mode, open positions
  POST /pause           — halt trading (sets trading_enabled=false)
  POST /resume          — resume trading (sets trading_enabled=true)
  GET  /positions       — open positions from portfolio
  GET  /logs            — last 50 lines of polybot.log
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# ── Paths ────────────────────────────────────────────────────────────────────
HOME = Path.home()
BOT_CTRL = HOME / "polybot/data/bot_control.json"
LOG_FILE  = HOME / "polybot/logs/polybot.log"
PORTFOLIO = HOME / "polybot/data/portfolio.json"

# Simple bearer token — set in .env or env var, defaults to a hard value
API_TOKEN = os.getenv("API_TOKEN", "polybot-apex-2026")


# ── Helpers ──────────────────────────────────────────────────────────────────
def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def write_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2))


def tail_log(n: int = 50) -> list[str]:
    if not LOG_FILE.exists():
        return ["log file not found"]
    try:
        r = subprocess.run(["tail", f"-{n}", str(LOG_FILE)], capture_output=True, text=True)
        return r.stdout.splitlines()
    except Exception as e:
        return [str(e)]


def get_status() -> dict:
    ctrl = read_json(BOT_CTRL)
    portfolio = read_json(PORTFOLIO)

    # Process check
    r = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True, text=True)
    process_running = bool(r.stdout.strip())

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bot": {
            "mode": ctrl.get("mode", "unknown"),
            "trading_enabled": ctrl.get("trading_enabled", False),
            "halt_reason": ctrl.get("halt_reason"),
            "last_run": ctrl.get("last_bot_run"),
            "process_running": process_running,
        },
        "portfolio": {
            "value": portfolio.get("portfolio_value", 0),
            "cash": portfolio.get("cash", 0),
            "deployed": portfolio.get("deployed", 0),
            "pnl": portfolio.get("total_pnl", 0),
            "win_rate": portfolio.get("win_rate", 0),
            "open_positions": len(portfolio.get("positions", [])),
        },
    }


# ── Request handler ──────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress default HTTP logs

    def send_json(self, code: int, data: dict):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def auth_ok(self) -> bool:
        token = self.headers.get("Authorization", "")
        return token == f"Bearer {API_TOKEN}"

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            self.send_json(200, {"status": "ok"})
            return

        if not self.auth_ok():
            self.send_json(401, {"error": "Unauthorized"})
            return

        if path == "/status":
            self.send_json(200, get_status())

        elif path == "/positions":
            portfolio = read_json(PORTFOLIO)
            positions = portfolio.get("positions", [])
            self.send_json(200, {"positions": positions, "count": len(positions)})

        elif path == "/logs":
            lines = tail_log(50)
            self.send_json(200, {"lines": lines, "count": len(lines)})

        elif path == "/control":
            self.send_json(200, read_json(BOT_CTRL))

        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if not self.auth_ok():
            self.send_json(401, {"error": "Unauthorized"})
            return

        ctrl = read_json(BOT_CTRL)

        if path == "/pause":
            ctrl["trading_enabled"] = False
            ctrl["halt_reason"] = "paused via API"
            ctrl["updated_by"] = "apex-api"
            ctrl["updated_at"] = datetime.now(timezone.utc).isoformat()
            write_json(BOT_CTRL, ctrl)
            self.send_json(200, {"ok": True, "trading_enabled": False})

        elif path == "/resume":
            ctrl["trading_enabled"] = True
            ctrl["halt_reason"] = None
            ctrl["updated_by"] = "apex-api"
            ctrl["updated_at"] = datetime.now(timezone.utc).isoformat()
            write_json(BOT_CTRL, ctrl)
            self.send_json(200, {"ok": True, "trading_enabled": True})

        else:
            self.send_json(404, {"error": "Not found"})


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("API_PORT", 8766))
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"PolyBot API running on port {port}")
    server.serve_forever()
