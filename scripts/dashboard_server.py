#!/usr/bin/env python3
"""
PolyBot Live Dashboard — port 8767
Exposed via Cloudflare tunnel at https://exemesh.dev

Single-page dashboard showing:
  - Bot status (live/paused, last run)
  - Portfolio value, cash, P&L, win rate
  - Open positions table
  - Last 20 log lines (errors highlighted)
  - Pause / Resume buttons (calls api.exemesh.dev)

Auto-refreshes every 30 seconds.
No auth required to VIEW — control buttons call the API which requires a token.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOME = Path.home()
BOT_CTRL  = HOME / "polybot/data/bot_control.json"
LOG_FILE  = HOME / "polybot/logs/polybot.log"
PORTFOLIO = HOME / "polybot/data/portfolio.json"


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def tail_log(n: int = 20) -> list[str]:
    if not LOG_FILE.exists():
        return ["log file not found"]
    try:
        r = subprocess.run(["tail", f"-{n}", str(LOG_FILE)], capture_output=True, text=True)
        return r.stdout.splitlines()
    except Exception:
        return []


def build_html() -> str:
    ctrl      = read_json(BOT_CTRL)
    portfolio = read_json(PORTFOLIO)
    log_lines = tail_log(20)

    mode     = ctrl.get("mode", "unknown")
    enabled  = ctrl.get("trading_enabled", False)
    halt_msg = ctrl.get("halt_reason") or ""
    last_run = ctrl.get("last_bot_run", "—")
    if last_run and last_run != "—":
        last_run = last_run[:19].replace("T", " ") + " UTC"

    pv       = portfolio.get("portfolio_value", 0)
    cash     = portfolio.get("cash", 0)
    deployed = portfolio.get("deployed", 0)
    pnl      = portfolio.get("total_pnl", 0)
    wr       = portfolio.get("win_rate", 0)
    wins     = portfolio.get("wins", 0)
    losses   = portfolio.get("losses", 0)
    positions = portfolio.get("positions", [])

    status_color = "#2ecc71" if enabled else "#e74c3c"
    status_text  = f"{'🟢 LIVE' if mode == 'live' else '🟡 DRY'} — {'Trading' if enabled else 'HALTED'}"
    pnl_color    = "#2ecc71" if pnl >= 0 else "#e74c3c"
    pnl_sign     = "+" if pnl >= 0 else ""

    # Build positions rows
    pos_rows = ""
    for p in positions:
        market  = p.get("question", p.get("condition_id", "—"))[:50]
        side    = p.get("side", "—")
        size    = p.get("size", 0)
        price   = p.get("avg_price", p.get("price", 0))
        current = p.get("current_price", price)
        upnl    = (current - price) * size if side == "YES" else (price - current) * size
        upnl_c  = "#2ecc71" if upnl >= 0 else "#e74c3c"
        pos_rows += f"""
        <tr>
          <td title="{market}">{market[:40]}{'…' if len(market) > 40 else ''}</td>
          <td>{side}</td>
          <td>${size:.2f}</td>
          <td>{price:.3f}</td>
          <td>{current:.3f}</td>
          <td style="color:{upnl_c}">{'+' if upnl >= 0 else ''}{upnl:.2f}</td>
        </tr>"""

    if not pos_rows:
        pos_rows = '<tr><td colspan="6" style="text-align:center;color:#666">No open positions</td></tr>'

    # Build log rows
    error_kw = ["ERROR", "EXCEPTION", "Traceback", "CRITICAL", "halted", "invalid signature"]
    log_html = ""
    for line in reversed(log_lines):
        is_err = any(k in line for k in error_kw)
        color  = "#e74c3c" if is_err else "#ccc"
        line_e = line.replace("<", "&lt;").replace(">", "&gt;")
        log_html += f'<div style="color:{color};font-family:monospace;font-size:11px;padding:1px 0">{line_e}</div>\n'

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="30">
  <title>PolyBot Dashboard — exemesh.dev</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 20px; }}
    h1 {{ font-size: 22px; margin-bottom: 4px; }}
    .subtitle {{ color: #8b949e; font-size: 13px; margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }}
    .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }}
    .card .label {{ color: #8b949e; font-size: 12px; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; }}
    .card .value {{ font-size: 24px; font-weight: 600; }}
    .card .sub {{ color: #8b949e; font-size: 12px; margin-top: 4px; }}
    .status-badge {{ display:inline-block; padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: 600; background: {status_color}22; color: {status_color}; border: 1px solid {status_color}55; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th {{ text-align: left; padding: 8px 10px; color: #8b949e; border-bottom: 1px solid #30363d; font-weight: 500; }}
    td {{ padding: 7px 10px; border-bottom: 1px solid #21262d; }}
    tr:last-child td {{ border-bottom: none; }}
    .section {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
    .section h2 {{ font-size: 14px; color: #8b949e; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 12px; }}
    .log-box {{ background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 10px; max-height: 260px; overflow-y: auto; }}
    .btn {{ padding: 8px 18px; border-radius: 6px; border: none; cursor: pointer; font-size: 13px; font-weight: 600; }}
    .btn-pause {{ background: #e74c3c22; color: #e74c3c; border: 1px solid #e74c3c55; }}
    .btn-resume {{ background: #2ecc7122; color: #2ecc71; border: 1px solid #2ecc7155; }}
    .btn:hover {{ opacity: .8; }}
    .halt {{ color: #e74c3c; font-size: 12px; margin-top: 6px; }}
    footer {{ color: #8b949e; font-size: 11px; text-align: center; margin-top: 20px; }}
  </style>
</head>
<body>

<h1>🔱 PolyBot Dashboard</h1>
<div class="subtitle">exemesh.dev &nbsp;·&nbsp; auto-refresh every 30s &nbsp;·&nbsp; {now}</div>

<div class="grid">
  <div class="card">
    <div class="label">Status</div>
    <div class="value"><span class="status-badge">{status_text}</span></div>
    <div class="sub">Last run: {last_run}</div>
    {'<div class="halt">⚠ ' + halt_msg + '</div>' if halt_msg else ''}
  </div>
  <div class="card">
    <div class="label">Portfolio Value</div>
    <div class="value">${pv:.2f}</div>
    <div class="sub">Cash: ${cash:.2f} &nbsp;|&nbsp; Deployed: ${deployed:.2f}</div>
  </div>
  <div class="card">
    <div class="label">Total P&amp;L</div>
    <div class="value" style="color:{pnl_color}">{pnl_sign}${pnl:.2f}</div>
    <div class="sub">{wins}W / {losses}L &nbsp;|&nbsp; Win rate: {wr:.0f}%</div>
  </div>
  <div class="card">
    <div class="label">Open Positions</div>
    <div class="value">{len(positions)}</div>
    <div class="sub">Waiting for resolution</div>
  </div>
</div>

<div class="section">
  <h2>Bot Control</h2>
  <button class="btn btn-pause" onclick="control('pause')">⏸ Pause Trading</button>
  &nbsp;
  <button class="btn btn-resume" onclick="control('resume')">▶ Resume Trading</button>
  <div id="ctrl-msg" style="color:#8b949e;font-size:12px;margin-top:8px"></div>
</div>

<div class="section">
  <h2>Open Positions ({len(positions)})</h2>
  <table>
    <thead><tr><th>Market</th><th>Side</th><th>Size</th><th>Avg Price</th><th>Current</th><th>uP&amp;L</th></tr></thead>
    <tbody>{pos_rows}</tbody>
  </table>
</div>

<div class="section">
  <h2>Recent Logs (last 20 lines)</h2>
  <div class="log-box">{log_html}</div>
</div>

<footer>PolyBot · Apex 🔱 · exemesh.dev</footer>

<script>
async function control(action) {{
  const token = prompt("Enter API token:");
  if (!token) return;
  const msg = document.getElementById("ctrl-msg");
  msg.textContent = "Sending...";
  try {{
    const r = await fetch("https://api.exemesh.dev/" + action, {{
      method: "POST",
      headers: {{ "Authorization": "Bearer " + token }}
    }});
    const d = await r.json();
    msg.textContent = r.ok ? "✅ Done — " + JSON.stringify(d) : "❌ " + JSON.stringify(d);
    if (r.ok) setTimeout(() => location.reload(), 1500);
  }} catch(e) {{
    msg.textContent = "❌ Error: " + e.message;
  }}
}}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/dashboard"):
            html = build_html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(html))
            self.end_headers()
            self.wfile.write(html)
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", 8767))
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"PolyBot Dashboard running on port {port}")
    server.serve_forever()
