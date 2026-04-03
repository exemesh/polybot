#!/usr/bin/env python3
"""
Exemesh Command Centre — port 8767
Exposed via Cloudflare tunnel at https://exemesh.dev

One-stop dashboard covering:
  - Polybot: status, P&L, open positions, recent logs, pause/resume
  - Binancebot: open positions, deployed capital, funding earned
  - Infrastructure: launchd service health, disk, last-run timestamps
  - Apex Status: iframe/link to port 4000 Apex panel

Auto-refreshes every 30 seconds.
"""

import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOME = Path.home()
# Resolve actual polybot location — check nanoclaw workspace first, fallback to legacy path
_NANO_BASE = HOME / "nanoclaw/groups/discord_main"
POLY_BASE    = _NANO_BASE / "polybot" if (_NANO_BASE / "polybot").exists() else HOME / "polybot"
BOT_CTRL     = POLY_BASE / "data/bot_control.json"
POLY_LOG     = POLY_BASE / "logs/polybot.log"
POLY_DB      = POLY_BASE / "data/polybot.db"
BIN_BASE     = _NANO_BASE / "binancebot" if (_NANO_BASE / "binancebot").exists() else HOME / "bots/binancebot"
BIN_STATUS   = BIN_BASE / "data/status.json"
BIN_LOG      = BIN_BASE / "logs/binancebot.log"


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def fetch_clob_price(token_id: str) -> float:
    """Fetch live mid price from Polymarket CLOB API. Returns 0.0 on failure."""
    if not token_id or "|" in token_id:
        return 0.0
    try:
        import urllib.request
        url = f"https://clob.polymarket.com/price?token_id={token_id}&side=BUY"
        req = urllib.request.Request(url, headers={"User-Agent": "exemesh/1.0"})
        with urllib.request.urlopen(req, timeout=2) as r:
            data = json.loads(r.read())
            return float(data.get("price", 0))
    except Exception:
        return 0.0


def read_portfolio_from_db() -> dict:
    """Read portfolio stats directly from polybot.db SQLite."""
    result = {
        "portfolio_value": 0, "cash": 0, "deployed": 0,
        "total_pnl": 0, "win_rate": 0, "wins": 0, "losses": 0,
        "positions": [],
    }
    if not POLY_DB.exists():
        return result
    try:
        import sqlite3
        conn = sqlite3.connect(str(POLY_DB))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Realized P&L from closed trades (live only)
        row = cur.execute(
            "SELECT COALESCE(SUM(pnl),0) as total, "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) as losses "
            "FROM trades WHERE dry_run=0 AND pnl IS NOT NULL "
            "AND status IN ('won','lost','resolved')"
        ).fetchone()
        total_pnl = float(row["total"] or 0)
        wins      = int(row["wins"] or 0)
        losses    = int(row["losses"] or 0)
        closed    = wins + losses
        win_rate  = (wins / closed * 100) if closed > 0 else 0

        # Open positions
        open_rows = cur.execute(
            "SELECT market_question, side, token_id, price, size_usd "
            "FROM trades WHERE dry_run=0 AND status='open'"
        ).fetchall()
        deployed = sum(float(r["size_usd"] or 0) for r in open_rows)

        # Fetch live prices for uP&L (timeout 2s each, skip on failure)
        positions = []
        for r in open_rows:
            entry = float(r["price"] or 0)
            live  = fetch_clob_price(r["token_id"] or "")
            curr  = live if live > 0 else entry
            positions.append({
                "question": r["market_question"] or "",
                "side": r["side"] or "",
                "size": float(r["size_usd"] or 0),
                "price": entry,
                "current_price": curr,
            })

        # Try to read initial capital from polybot .env (correct path)
        try:
            import sys
            sys.path.insert(0, str(POLY_BASE))
            from dotenv import load_dotenv as _lde
            _lde(POLY_BASE / ".env")
            initial = float(os.getenv("INITIAL_CAPITAL", "0") or 0)
        except Exception:
            initial = 0.0

        # Best-effort portfolio value: CLOB balance from bot_control > env > fallback
        ctrl_snap = read_json(BOT_CTRL)
        clob_bal = float(ctrl_snap.get("usdc_balance") or ctrl_snap.get("clob_balance") or 0)
        if clob_bal > 0:
            portfolio_value = clob_bal + deployed
        elif initial > 0:
            portfolio_value = initial + total_pnl
        else:
            portfolio_value = deployed + max(total_pnl, 0)
        cash = max(portfolio_value - deployed, 0)

        result.update({
            "portfolio_value": round(portfolio_value, 2),
            "cash": round(cash, 2),
            "deployed": round(deployed, 2),
            "total_pnl": round(total_pnl, 4),
            "win_rate": round(win_rate, 1),
            "wins": wins,
            "losses": losses,
            "positions": positions,
        })
        conn.close()
    except Exception as e:
        result["error"] = str(e)
    return result


def tail_log(path: Path, n: int = 25) -> list[str]:
    if not path.exists():
        return [f"[log not found: {path}]"]
    try:
        r = subprocess.run(["tail", f"-{n}", str(path)], capture_output=True, text=True, timeout=5)
        return r.stdout.splitlines()
    except Exception:
        return []


def launchd_services() -> list[tuple[str, str, bool]]:
    """Return list of (label, short_name, is_loaded) for polybot services."""
    services = [
        ("com.polybot.trader",       "trader (5 min)"),
        ("com.polybot.log-relay",    "log-relay (5 min)"),
        ("com.polybot.infra-health", "infra-health (07:30)"),
        ("com.polybot.dep-watchdog", "dep-watchdog (Sunday)"),
        ("com.polybot.api-server",   "api-server (8766)"),
        ("com.polybot.dashboard",    "dashboard (8767)"),
        ("com.binancebot",           "binancebot (8h)"),
        ("homebrew.mxcl.cloudflared", "cloudflared (tunnel)"),
    ]
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        out = ""
    return [(lbl, name, lbl in out) for lbl, name in services]


def disk_info() -> str:
    try:
        import shutil
        total, used, free = shutil.disk_usage("/")
        return f"{free/(1024**3):.1f} GB free / {total/(1024**3):.0f} GB"
    except Exception:
        return "unknown"


def minutes_since(ts_str: str) -> float | None:
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 60
    except Exception:
        return None


def build_html() -> str:
    ctrl      = read_json(BOT_CTRL)
    portfolio = read_portfolio_from_db()
    bin_st    = read_json(BIN_STATUS)
    poly_logs = tail_log(POLY_LOG, 25)
    bin_logs  = tail_log(BIN_LOG, 10)
    svcs      = launchd_services()

    # ── Polybot state ────────────────────────────────────────────────────
    mode     = ctrl.get("mode", "unknown")
    enabled  = ctrl.get("trading_enabled", False)
    halt_msg = ctrl.get("halt_reason") or ""
    last_run = ctrl.get("last_bot_run", "")
    mins_ago = minutes_since(last_run)
    last_run_disp = (last_run[:19].replace("T", " ") + " UTC") if last_run else "—"
    stall_warn = (f'<div class="warn">⚠ Last run {mins_ago:.0f} min ago — may be stalled</div>'
                  if mins_ago and mins_ago > 15 else "")

    pv       = portfolio.get("portfolio_value", 0)
    cash     = portfolio.get("cash", 0)
    deployed = portfolio.get("deployed", 0)
    pnl      = portfolio.get("total_pnl", 0)
    wr       = portfolio.get("win_rate", 0)
    wins     = portfolio.get("wins", 0)
    losses   = portfolio.get("losses", 0)
    positions = portfolio.get("positions", [])

    status_color = "#2ecc71" if enabled else "#e74c3c"
    status_text  = f"{'🟢 LIVE' if mode == 'live' else '🟡 DRY'} {'Trading' if enabled else '⛔ HALTED'}"
    pnl_color    = "#2ecc71" if pnl >= 0 else "#e74c3c"

    # Positions table
    pos_rows = ""
    for p in positions:
        mkt    = p.get("question", p.get("condition_id", "—"))[:48]
        side   = p.get("side", "—")
        size   = p.get("size", 0)
        price  = p.get("avg_price", p.get("price", 0))
        curr   = p.get("current_price", price)
        upnl   = (curr - price) * size if "YES" in side else (price - curr) * size
        uc     = "#2ecc71" if upnl >= 0 else "#e74c3c"
        pos_rows += f"""<tr>
          <td title="{mkt}">{mkt[:42]}{'…' if len(mkt) > 42 else ''}</td>
          <td>{side}</td><td>${size:.2f}</td>
          <td>{price:.3f}</td><td>{curr:.3f}</td>
          <td style="color:{uc}">{'+' if upnl >= 0 else ''}{upnl:.2f}</td></tr>"""
    if not pos_rows:
        pos_rows = '<tr><td colspan="6" class="empty">No open positions</td></tr>'

    # Poly log
    err_kw = ["ERROR", "EXCEPTION", "Traceback", "CRITICAL", "halted", "invalid signature", "not enough balance"]
    trade_kw = ["Market order placed", "orderID=", "Trade placed", "ENTER]"]
    poly_log_html = ""
    for line in reversed(poly_logs):
        is_err   = any(k in line for k in err_kw)
        is_trade = any(k in line for k in trade_kw)
        color = "#e74c3c" if is_err else "#2ecc71" if is_trade else "#8b949e"
        le = line.replace("<", "&lt;").replace(">", "&gt;")
        poly_log_html += f'<div style="color:{color}">{le}</div>\n'

    # ── Binancebot state ─────────────────────────────────────────────────
    bin_updated = bin_st.get("updated_at", "")
    bin_mins    = minutes_since(bin_updated)
    bin_stale   = bin_mins and bin_mins > 500  # > ~8h
    bin_disp    = (bin_updated[:19].replace("T", " ") + " UTC") if bin_updated else "awaiting first cycle"
    bin_deployed = bin_st.get("deployed_usdt", 0)
    bin_capital  = bin_st.get("total_capital", 306)
    bin_funding  = bin_st.get("funding_earned_8h", 0)
    bin_positions = bin_st.get("open_positions", [])
    bin_pos_rows = ""
    for p in bin_positions:
        sym  = p.get("symbol", "—")
        size = p.get("size_usdt", 0)
        fund = p.get("funding_collected_usdt", 0)
        ep   = p.get("entry_price", 0)
        bin_pos_rows += f"<tr><td>{sym}</td><td>${size:.2f}</td><td>${ep:.4f}</td><td style='color:#2ecc71'>${fund:.4f}</td></tr>"
    if not bin_pos_rows:
        bin_pos_rows = '<tr><td colspan="4" class="empty">No positions — awaiting next 8h cycle</td></tr>'
    bin_log_html = ""
    for line in reversed(bin_logs):
        is_err = any(k in line for k in err_kw)
        color  = "#e74c3c" if is_err else "#8b949e"
        le     = line.replace("<", "&lt;").replace(">", "&gt;")
        bin_log_html += f'<div style="color:{color}">{le}</div>\n'

    # ── Infrastructure ───────────────────────────────────────────────────
    svc_rows = ""
    for lbl, name, loaded in svcs:
        dot = "🟢" if loaded else "🔴"
        svc_rows += f"<tr><td>{dot}</td><td style='font-family:monospace;font-size:12px'>{lbl}</td><td style='color:#8b949e'>{name}</td></tr>"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Exemesh Command Centre</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:0}}
    nav{{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;align-items:center;gap:20px;position:sticky;top:0;z-index:10}}
    nav .brand{{font-weight:700;font-size:16px;color:#fff}}
    nav a{{color:#8b949e;text-decoration:none;font-size:13px;padding:5px 10px;border-radius:6px;transition:.15s}}
    nav a:hover,nav a.active{{background:#21262d;color:#e6edf3}}
    nav .time{{margin-left:auto;color:#8b949e;font-size:12px}}
    .wrap{{padding:20px;max-width:1200px;margin:0 auto}}
    .section{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:16px}}
    .section h2{{font-size:13px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:14px;display:flex;align-items:center;gap:8px}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:16px}}
    .card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}}
    .card .lbl{{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px}}
    .card .val{{font-size:22px;font-weight:600}}
    .card .sub{{color:#8b949e;font-size:11px;margin-top:4px}}
    .badge{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600;background:{status_color}22;color:{status_color};border:1px solid {status_color}55}}
    table{{width:100%;border-collapse:collapse;font-size:13px}}
    th{{text-align:left;padding:7px 10px;color:#8b949e;border-bottom:1px solid #30363d;font-weight:500}}
    td{{padding:6px 10px;border-bottom:1px solid #21262d}}
    tr:last-child td{{border-bottom:none}}
    .empty{{text-align:center;color:#666;padding:16px!important}}
    .log-box{{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px;max-height:260px;overflow-y:auto;font-family:monospace;font-size:11px;line-height:1.5}}
    .btn{{padding:7px 16px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:600;margin-right:8px}}
    .btn-pause{{background:#e74c3c22;color:#e74c3c;border:1px solid #e74c3c55}}
    .btn-resume{{background:#2ecc7122;color:#2ecc71;border:1px solid #2ecc7155}}
    .btn-apex{{background:#007bff22;color:#58a6ff;border:1px solid #58a6ff55}}
    .btn:hover{{opacity:.8}}
    .warn{{color:#f0a500;font-size:12px;margin-top:6px}}
    .halt{{color:#e74c3c;font-size:12px;margin-top:6px}}
    #ctrl-msg{{color:#8b949e;font-size:12px;margin-top:8px}}
    .tab-content{{display:none}} .tab-content.active{{display:block}}
    footer{{color:#8b949e;font-size:11px;text-align:center;padding:20px}}
  </style>
</head>
<body>

<nav>
  <span class="brand">🔱 Exemesh</span>
  <a href="#" onclick="showTab('polybot');return false" class="active" id="tab-polybot">Polybot</a>
  <a href="#" onclick="showTab('infra');return false" id="tab-infra">Infrastructure</a>
  <span style="padding:5px 12px;font-size:12px;color:#888">exemesh.dev</span>
  <span class="time">⟳ 30s &nbsp;·&nbsp; {now}</span>
</nav>

<div class="wrap">

<!-- ══ POLYBOT TAB ══════════════════════════════════════════════════════ -->
<div class="tab-content active" id="polybot">

  <div class="grid">
    <div class="card">
      <div class="lbl">Status</div>
      <div class="val"><span class="badge">{status_text}</span></div>
      <div class="sub">Last run: {last_run_disp}</div>
      {stall_warn}
      {'<div class="halt">⚠ ' + halt_msg + '</div>' if halt_msg else ''}
    </div>
    <div class="card">
      <div class="lbl">Portfolio</div>
      <div class="val">${pv:.2f}</div>
      <div class="sub">Cash ${cash:.2f} · Deployed ${deployed:.2f}</div>
    </div>
    <div class="card">
      <div class="lbl">Total P&amp;L</div>
      <div class="val" style="color:{pnl_color}">{'+' if pnl >= 0 else ''}${pnl:.2f}</div>
      <div class="sub">{wins}W / {losses}L · Win rate {wr:.0f}%</div>
    </div>
    <div class="card">
      <div class="lbl">Open Positions</div>
      <div class="val">{len(positions)}</div>
      <div class="sub">Awaiting resolution</div>
    </div>
  </div>

  <div class="section">
    <h2>⚙ Bot Control</h2>
    <button class="btn btn-pause" onclick="control('pause')">⏸ Pause</button>
    <button class="btn btn-resume" onclick="control('resume')">▶ Resume</button>
    <div id="ctrl-msg"></div>
  </div>

  <div class="section">
    <h2>📋 Open Positions ({len(positions)})</h2>
    <table>
      <thead><tr><th>Market</th><th>Side</th><th>Size</th><th>Entry</th><th>Current</th><th>uP&amp;L</th></tr></thead>
      <tbody>{pos_rows}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>📜 Polybot Logs</h2>
    <div class="log-box">{poly_log_html}</div>
  </div>
</div>

<!-- ══ INFRA TAB ════════════════════════════════════════════════════════ -->
<div class="tab-content" id="infra">

  <div class="grid">
    <div class="card">
      <div class="lbl">Disk</div>
      <div class="val" style="font-size:18px">{disk_info()}</div>
    </div>
    <div class="card">
      <div class="lbl">Polybot Last Run</div>
      <div class="val" style="font-size:16px">{f'{mins_ago:.0f} min ago' if mins_ago else '—'}</div>
      <div class="sub">{'🟢 on schedule' if mins_ago and mins_ago < 10 else '⚠ check launchd' if mins_ago and mins_ago > 15 else ''}</div>
    </div>
    <div class="card">
      <div class="lbl">Dashboard</div>
      <div class="val" style="font-size:14px"><a href="https://exemesh.dev" target="_blank" style="color:#58a6ff">exemesh.dev ↗</a></div>
      <div class="sub">live dashboard</div>
    </div>
  </div>

  <div class="section">
    <h2>🔧 launchd Services</h2>
    <table>
      <thead><tr><th></th><th>Service</th><th>Schedule</th></tr></thead>
      <tbody>{svc_rows}</tbody>
    </table>
  </div>

</div>

</div><!-- /wrap -->

<footer>Exemesh · Apex 🔱 · exemesh.dev · auto-refresh 30s</footer>

<script>
function showTab(name) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('nav a[id^=tab]').forEach(el => el.classList.remove('active'));
  document.getElementById(name).classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
}}
async function control(action) {{
  const token = prompt("API token:");
  if (!token) return;
  const msg = document.getElementById("ctrl-msg");
  msg.textContent = "Sending...";
  try {{
    const r = await fetch("http://localhost:8766/" + action, {{
      method:"POST", headers:{{"Authorization":"Bearer " + token}}
    }});
    const d = await r.json();
    msg.textContent = r.ok ? "✅ " + JSON.stringify(d) : "❌ " + JSON.stringify(d);
    if (r.ok) setTimeout(() => location.reload(), 1500);
  }} catch(e) {{ msg.textContent = "❌ " + e.message; }}
}}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/dashboard", "/polybot", "/infra"):
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
    print(f"Exemesh Command Centre running on port {port} → https://exemesh.dev")
    server.serve_forever()
