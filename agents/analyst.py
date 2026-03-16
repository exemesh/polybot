"""
Analyst Agent — Combined P&L reports (Binance + Polymarket DB) → #analyst-dashboard
"""

import asyncio
import hashlib
import hmac
import logging
import os
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import httpx

from utils.discord_alerts import DiscordAlerts

logger = logging.getLogger("polybot.analyst")

ANALYST_CHANNEL = "1483029691689341110"

BINANCE_SPOT_URL = "https://api.binance.com"
BINANCE_FUTURES_URL = "https://fapi.binance.com"

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polybot.db")


def _binance_sign(params: dict, secret: str) -> str:
    query = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig


async def fetch_binance_balance(api_key: str, secret: str) -> dict:
    """Fetch combined Binance portfolio value."""
    result = {"usdt_balance": 0.0, "total_usd": 0.0, "futures_wallet": 0.0}
    if not api_key or not secret:
        return result

    async with httpx.AsyncClient(timeout=15) as client:
        # Spot account
        ts = int(time.time() * 1000)
        qs = _binance_sign({"timestamp": ts}, secret)
        try:
            resp = await client.get(
                f"{BINANCE_SPOT_URL}/api/v3/account?{qs}",
                headers={"X-MBX-APIKEY": api_key},
                timeout=15,
            )
            if resp.status_code == 200:
                account = resp.json()
                for b in account.get("balances", []):
                    if b["asset"] == "USDT":
                        result["usdt_balance"] = float(b["free"]) + float(b["locked"])
                    elif b["asset"] == "USDC":
                        result["usdt_balance"] += float(b["free"]) + float(b["locked"])
        except Exception as exc:
            logger.warning(f"Binance spot balance fetch failed: {exc}")

        # Futures wallet balance
        ts = int(time.time() * 1000)
        qs = _binance_sign({"timestamp": ts}, secret)
        try:
            resp2 = await client.get(
                f"{BINANCE_FUTURES_URL}/fapi/v2/balance?{qs}",
                headers={"X-MBX-APIKEY": api_key},
                timeout=15,
            )
            if resp2.status_code == 200:
                for wallet in resp2.json():
                    if wallet.get("asset") in ("USDT", "USDC"):
                        result["futures_wallet"] += float(wallet.get("balance", 0))
        except Exception as exc:
            logger.warning(f"Binance futures balance fetch failed: {exc}")

    result["total_usd"] = result["usdt_balance"] + result["futures_wallet"]
    return result


def fetch_trade_stats_from_db(db_path: str) -> dict:
    """Read trade history from the polybot SQLite database and compute stats."""
    stats = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "avg_profit": 0.0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "total_pnl": 0.0,
        "daily_pnl": 0.0,
        "weekly_pnl": 0.0,
        "strategy_stats": {},
        "daily_breakdown": [],
        "error": None,
    }
    if not os.path.exists(db_path):
        stats["error"] = f"DB not found: {db_path}"
        return stats

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Detect available tables
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        # Look for a trades or positions table
        trade_table = None
        for candidate in ("trades", "positions", "closed_positions"):
            if candidate in tables:
                trade_table = candidate
                break

        if not trade_table:
            stats["error"] = f"No trade table found. Tables: {tables}"
            conn.close()
            return stats

        # Fetch all closed/resolved trades
        try:
            rows = cur.execute(
                f"SELECT * FROM {trade_table} WHERE status IN ('resolved','closed','won','lost') "
                f"OR pnl IS NOT NULL ORDER BY created_at DESC LIMIT 1000"
            ).fetchall()
        except Exception:
            rows = cur.execute(f"SELECT * FROM {trade_table} LIMIT 1000").fetchall()

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=7)

        profits = []
        strategy_map: dict[str, list[float]] = {}

        for row in rows:
            d = dict(row)
            pnl = float(d.get("pnl") or d.get("profit") or d.get("realized_pnl") or 0.0)
            profits.append(pnl)

            strategy = d.get("strategy", d.get("strategy_name", "unknown"))
            strategy_map.setdefault(strategy, []).append(pnl)

            # Parse timestamp
            ts_raw = d.get("created_at") or d.get("closed_at") or d.get("resolved_at") or ""
            try:
                if ts_raw:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= today_start:
                        stats["daily_pnl"] += pnl
                    if ts >= week_start:
                        stats["weekly_pnl"] += pnl
            except Exception:
                pass

        stats["total_trades"] = len(profits)
        stats["wins"] = sum(1 for p in profits if p > 0)
        stats["losses"] = sum(1 for p in profits if p <= 0)
        stats["win_rate"] = (stats["wins"] / stats["total_trades"] * 100) if profits else 0.0
        stats["avg_profit"] = sum(profits) / len(profits) if profits else 0.0
        stats["best_trade"] = max(profits) if profits else 0.0
        stats["worst_trade"] = min(profits) if profits else 0.0
        stats["total_pnl"] = sum(profits)

        # Strategy breakdown
        for strat, pnls in strategy_map.items():
            stats["strategy_stats"][strat] = {
                "count": len(pnls),
                "total": round(sum(pnls), 4),
                "avg": round(sum(pnls) / len(pnls), 4) if pnls else 0,
                "wins": sum(1 for p in pnls if p > 0),
            }

        conn.close()
    except Exception as exc:
        stats["error"] = str(exc)
        logger.error(f"DB read error: {exc}")

    return stats


def _ascii_bar(value: float, max_val: float, width: int = 12) -> str:
    """Generate a simple ASCII bar for inline display."""
    if max_val == 0:
        return "[" + " " * width + "]"
    filled = int(abs(value) / max_val * width)
    filled = min(filled, width)
    char = "+" if value >= 0 else "-"
    return "[" + char * filled + " " * (width - filled) + "]"


async def run_analyst() -> None:
    """Main Analyst agent: compile stats and post to #analyst-dashboard."""
    logger.info("Analyst agent starting...")

    api_key = os.getenv("BINANCE_API_KEY", "")
    secret = os.getenv("BINANCE_SECRET_KEY", "")
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord = DiscordAlerts(bot_token=bot_token)

    binance_bal, db_stats = await asyncio.gather(
        fetch_binance_balance(api_key, secret),
        asyncio.to_thread(fetch_trade_stats_from_db, DB_PATH),
        return_exceptions=True,
    )

    if isinstance(binance_bal, Exception):
        logger.error(f"Binance balance failed: {binance_bal}")
        binance_bal = {"usdt_balance": 0.0, "futures_wallet": 0.0, "total_usd": 0.0}
    if isinstance(db_stats, Exception):
        logger.error(f"DB stats failed: {db_stats}")
        db_stats = {"error": str(db_stats), "total_trades": 0}

    binance_total = binance_bal.get("total_usd", 0.0)
    poly_pnl = db_stats.get("total_pnl", 0.0)

    # Portfolio value section
    fields = [
        {
            "name": "Binance Portfolio",
            "value": (
                f"Spot: ${binance_bal.get('usdt_balance', 0):.2f}\n"
                f"Futures: ${binance_bal.get('futures_wallet', 0):.2f}\n"
                f"Total: ${binance_total:.2f}"
            ),
            "inline": True,
        },
        {
            "name": "Polymarket P&L",
            "value": (
                f"Total PnL: ${poly_pnl:+.4f}\n"
                f"Daily PnL: ${db_stats.get('daily_pnl', 0):+.4f}\n"
                f"Weekly PnL: ${db_stats.get('weekly_pnl', 0):+.4f}"
            ),
            "inline": True,
        },
    ]

    # Trade statistics
    if db_stats.get("error"):
        fields.append({
            "name": "Trade Statistics",
            "value": f"DB error: {db_stats['error']}",
            "inline": False,
        })
    else:
        total_trades = db_stats.get("total_trades", 0)
        win_rate = db_stats.get("win_rate", 0.0)
        avg_profit = db_stats.get("avg_profit", 0.0)
        best = db_stats.get("best_trade", 0.0)
        worst = db_stats.get("worst_trade", 0.0)

        fields.append({
            "name": "Trade Statistics",
            "value": (
                f"Total Trades: {total_trades}\n"
                f"Wins: {db_stats.get('wins', 0)} | Losses: {db_stats.get('losses', 0)}\n"
                f"Win Rate: {win_rate:.1f}%\n"
                f"Avg Profit: ${avg_profit:+.4f}\n"
                f"Best Trade: ${best:+.4f} | Worst: ${worst:+.4f}"
            ),
            "inline": False,
        })

    # Strategy performance
    strategy_stats = db_stats.get("strategy_stats", {})
    if strategy_stats:
        sorted_strats = sorted(strategy_stats.items(), key=lambda x: x[1]["total"], reverse=True)
        best_strats = sorted_strats[:3]
        worst_strats = sorted_strats[-2:] if len(sorted_strats) > 3 else []

        best_lines = [
            f"• {s}: ${v['total']:+.4f} ({v['wins']}/{v['count']} wins)"
            for s, v in best_strats
        ]
        fields.append({
            "name": "Top Performing Strategies",
            "value": "\n".join(best_lines) or "N/A",
            "inline": False,
        })

        if worst_strats:
            worst_lines = [
                f"• {s}: ${v['total']:+.4f} ({v['wins']}/{v['count']} wins)"
                for s, v in worst_strats
            ]
            fields.append({
                "name": "Worst Performing Strategies",
                "value": "\n".join(worst_lines),
                "inline": False,
            })

    # Daily P&L ASCII chart (last 7 days — placeholder, real breakdown needs more DB query)
    daily = db_stats.get("daily_pnl", 0.0)
    weekly = db_stats.get("weekly_pnl", 0.0)
    max_abs = max(abs(daily), abs(weekly), 0.01)
    chart = (
        f"Today  {_ascii_bar(daily, max_abs)} ${daily:+.4f}\n"
        f"7-Day  {_ascii_bar(weekly, max_abs)} ${weekly:+.4f}"
    )
    fields.append({
        "name": "P&L Chart (ASCII)",
        "value": f"```\n{chart}\n```",
        "inline": False,
    })

    combined_pnl = poly_pnl
    color = 0x00C851 if combined_pnl >= 0 else 0xFF4444

    embed = {
        "title": "Analyst Dashboard Report",
        "description": f"Combined portfolio snapshot — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "PolyBot Analyst Agent"},
    }

    await discord._post_channel_message(ANALYST_CHANNEL, embed)
    logger.info("Analyst report posted to #analyst-dashboard.")
