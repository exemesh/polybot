"""
Weekly Performance Report — posted every Sunday at 18:00 UTC via nanoclaw.

Covers the full past 7 days for both Polybot (Polymarket) and Kalbot (Kalshi).
Sends to the Pia webhook (#pia channel).
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

logger = logging.getLogger("polybot.weekly_report")

_ROOT        = Path(__file__).parent.parent
DATA_DIR     = _ROOT / "data"
DB_PATH      = DATA_DIR / "polybot.db"
KALBOT_STATUS = _ROOT.parent / "kalbot" / "data" / "status.json"
KALBOT_PORT  = _ROOT.parent / "kalbot" / "data" / "portfolio.json"

COLOR_GOLD   = 0xFFD700
COLOR_RED    = 0xFF4444
COLOR_GREEN  = 0x00C851

WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_PIA", "")

STRATEGY_LABELS = {
    "swarm_forecaster":      "SwarmForecaster",
    "news_arb":              "NewsArb",
    "P1_SentimentSpike":     "P1 · SentimentSpike",
    "P2_OverreactionFader":  "P2 · OverreactionFader",
    "P3_LiquiditySniper":    "P3 · LiquiditySniper",
    "K1_FairValue":          "K1 · FairValue",
    "K2_Resolution":         "K2 · Resolution",
    "K3_SpreadCapture":      "K3 · SpreadCapture",
}


# ── Data readers ──────────────────────────────────────────────────────────────

def fetch_polybot_week(since: datetime) -> dict:
    """Pull 7-day polybot stats from SQLite."""
    result = {
        "total_trades": 0, "wins": 0, "losses": 0,
        "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
        "avg_edge": 0.0, "strategy_breakdown": {},
        "balance": 0.0, "error": None,
    }
    if not DB_PATH.exists():
        result["error"] = "DB not found"
        return result
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        since_str = since.isoformat()

        # Closed/resolved trades this week
        rows = cur.execute("""
            SELECT strategy, pnl, edge_pct, market_question, status, timestamp
            FROM trades
            WHERE dry_run = 0
              AND pnl IS NOT NULL
              AND timestamp >= ?
            ORDER BY timestamp DESC
        """, (since_str,)).fetchall()

        edges = []
        for r in rows:
            pnl = float(r["pnl"] or 0)
            result["total_trades"] += 1
            result["total_pnl"] += pnl
            if pnl > 0:
                result["wins"] += 1
            elif pnl < 0:
                result["losses"] += 1
            result["best_trade"]  = max(result["best_trade"],  pnl)
            result["worst_trade"] = min(result["worst_trade"], pnl)
            if r["edge_pct"]:
                edges.append(float(r["edge_pct"]))

            strat = STRATEGY_LABELS.get(r["strategy"], r["strategy"] or "unknown")
            sb = result["strategy_breakdown"].setdefault(strat, {"trades": 0, "pnl": 0.0, "wins": 0})
            sb["trades"] += 1
            sb["pnl"]    = round(sb["pnl"] + pnl, 4)
            if pnl > 0:
                sb["wins"] += 1

        result["avg_edge"] = round(sum(edges) / len(edges), 1) if edges else 0.0
        result["total_pnl"] = round(result["total_pnl"], 4)

        # Current balance from latest snapshot
        snap = cur.execute(
            "SELECT total_value FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if snap:
            result["balance"] = round(float(snap[0]), 2)

        conn.close()
    except Exception as e:
        result["error"] = str(e)
    return result


def fetch_kalbot_week() -> dict:
    """Pull kalbot stats from status.json and portfolio closed trades."""
    result = {
        "total_trades": 0, "wins": 0, "losses": 0,
        "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
        "open_positions": 0, "balance": 0.0,
        "strategy_breakdown": {}, "error": None,
    }
    try:
        if KALBOT_STATUS.exists():
            status = json.loads(KALBOT_STATUS.read_text())
            result["balance"]        = status.get("balance_usd", 0.0)
            result["total_pnl"]      = status.get("total_pnl", 0.0)
            result["open_positions"] = status.get("open_positions", 0)

        if KALBOT_PORT.exists():
            port = json.loads(KALBOT_PORT.read_text())
            closed = port.get("closed", [])
            for trade in closed:
                pnl = float(trade.get("pnl", 0) or 0)
                result["total_trades"] += 1
                if pnl > 0:
                    result["wins"] += 1
                elif pnl < 0:
                    result["losses"] += 1
                result["best_trade"]  = max(result["best_trade"],  pnl)
                result["worst_trade"] = min(result["worst_trade"], pnl)

                strat = STRATEGY_LABELS.get(trade.get("strategy", ""), "K1 · FairValue")
                sb = result["strategy_breakdown"].setdefault(strat, {"trades": 0, "pnl": 0.0, "wins": 0})
                sb["trades"] += 1
                sb["pnl"]    = round(sb["pnl"] + pnl, 4)
                if pnl > 0:
                    sb["wins"] += 1

    except Exception as e:
        result["error"] = str(e)
    return result


# ── Report builder ────────────────────────────────────────────────────────────

def _win_rate(wins: int, total: int) -> str:
    return f"{wins/total*100:.0f}%" if total > 0 else "—"


def _pnl_bar(pnl: float) -> str:
    """Simple ASCII bar for weekly P&L."""
    if pnl == 0:
        return "`[············]  $0.00`"
    width = 12
    cap = max(abs(pnl), 1.0)
    filled = min(int(abs(pnl) / cap * width), width)
    char = "+" if pnl >= 0 else "-"
    bar = char * filled + "·" * (width - filled)
    return f"`[{bar}]  ${pnl:+.2f}`"


def build_report_embed(poly: dict, kal: dict, week_start: str, week_end: str) -> dict:
    now = datetime.now(timezone.utc)

    combined_pnl = round(poly["total_pnl"] + kal["total_pnl"], 2)
    combined_trades = poly["total_trades"] + kal["total_trades"]
    color = COLOR_GREEN if combined_pnl >= 0 else COLOR_RED

    fields = []

    # ── Polybot section ───────────────────────────────────────────────────────
    p_wr   = _win_rate(poly["wins"], poly["total_trades"])
    p_desc = (
        f"Balance: **${poly['balance']:.2f}**\n"
        f"Trades: **{poly['total_trades']}** | W/L: {poly['wins']}/{poly['losses']} | Win Rate: {p_wr}\n"
        f"Total P&L: **${poly['total_pnl']:+.4f}** | Avg Edge: {poly['avg_edge']:.1f}%\n"
        f"Best: ${poly['best_trade']:+.4f} | Worst: ${poly['worst_trade']:+.4f}"
    )
    if poly.get("error"):
        p_desc += f"\n⚠️ `{poly['error']}`"
    fields.append({"name": "📈 Polymarket (PolyBot V3)", "value": p_desc, "inline": False})

    # Polybot strategy breakdown
    if poly["strategy_breakdown"]:
        sorted_strats = sorted(poly["strategy_breakdown"].items(), key=lambda x: x[1]["pnl"], reverse=True)
        lines = [f"• `{s}`: {v['trades']} trades · ${v['pnl']:+.4f} · {_win_rate(v['wins'], v['trades'])}" for s, v in sorted_strats[:5]]
        fields.append({"name": "Strategy Breakdown (Polybot)", "value": "\n".join(lines) or "No data", "inline": False})

    # ── Kalbot section ────────────────────────────────────────────────────────
    k_wr   = _win_rate(kal["wins"], kal["total_trades"])
    k_desc = (
        f"Balance: **${kal['balance']:.2f}** | Open Positions: {kal['open_positions']}\n"
        f"Closed Trades: **{kal['total_trades']}** | W/L: {kal['wins']}/{kal['losses']} | Win Rate: {k_wr}\n"
        f"Total P&L: **${kal['total_pnl']:+.4f}**\n"
        f"Best: ${kal['best_trade']:+.4f} | Worst: ${kal['worst_trade']:+.4f}"
    )
    if kal.get("error"):
        k_desc += f"\n⚠️ `{kal['error']}`"
    fields.append({"name": "🗳️ Kalshi (KalBot V3)", "value": k_desc, "inline": False})

    if kal["strategy_breakdown"]:
        sorted_strats = sorted(kal["strategy_breakdown"].items(), key=lambda x: x[1]["pnl"], reverse=True)
        lines = [f"• `{s}`: {v['trades']} trades · ${v['pnl']:+.4f} · {_win_rate(v['wins'], v['trades'])}" for s, v in sorted_strats[:5]]
        fields.append({"name": "Strategy Breakdown (Kalbot)", "value": "\n".join(lines) or "No data", "inline": False})

    # ── Combined summary ──────────────────────────────────────────────────────
    fields.append({
        "name": "━━━━━━━━━━━━━━━━━━━━━━━━",
        "value": (
            f"**Combined Weekly P&L: {_pnl_bar(combined_pnl)}**\n"
            f"Total Trades: {combined_trades} | Week: {week_start} → {week_end}"
        ),
        "inline": False,
    })

    return {
        "title": f"📋 Weekly Performance Report",
        "description": f"**{week_start} — {week_end} UTC**\nPolymarket V3 MIROFISH · Kalshi V3 · Full week summary",
        "color": color,
        "fields": fields,
        "timestamp": now.isoformat(),
        "footer": {"text": "Pia — Weekly Report · Every Sunday 18:00 UTC"},
    }


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_weekly_report() -> None:
    """Generate and post the weekly performance report."""
    logger.info("Weekly report: generating...")

    now        = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    start_str  = week_start.strftime("%Y-%m-%d")
    end_str    = now.strftime("%Y-%m-%d")

    poly = await asyncio.to_thread(fetch_polybot_week, week_start)
    kal  = await asyncio.to_thread(fetch_kalbot_week)

    embed = build_report_embed(poly, kal, start_str, end_str)

    webhook = os.getenv("DISCORD_WEBHOOK_PIA", "")
    if not webhook:
        logger.error("Weekly report: DISCORD_WEBHOOK_PIA not set — cannot post")
        return

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            webhook,
            json={
                "username": "Pia · Weekly Report",
                "embeds": [embed],
            },
        )
        if resp.is_success:
            logger.info(f"Weekly report posted — poly_pnl=${poly['total_pnl']:+.4f} kal_pnl=${kal['total_pnl']:+.4f}")
        else:
            logger.error(f"Weekly report Discord post failed: {resp.status_code} {resp.text}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_weekly_report())
