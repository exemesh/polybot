"""
Sage Agent — Daily Polymarket P&L report → #analyst-dashboard
Posts at 8AM UTC and 5PM UTC only, once per slot per day.
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from utils.discord_alerts import DiscordAlerts

logger = logging.getLogger("polybot.analyst")

ANALYST_CHANNEL = "1483029691689341110"

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polybot.db")

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
LAST_POSTS_PATH = os.path.join(DATA_DIR, "sage_last_posts.json")

# Post slots (UTC hours)
POST_HOURS = {8, 17}


def _load_last_posts() -> dict:
    """Return dict with last_8am_post and last_5pm_post dates (YYYY-MM-DD), or None."""
    try:
        if os.path.exists(LAST_POSTS_PATH):
            with open(LAST_POSTS_PATH) as f:
                return json.load(f)
    except Exception as exc:
        logger.warning(f"Failed to load last post timestamps: {exc}")
    return {}


def _save_last_posts(data: dict) -> None:
    """Persist last post dates."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LAST_POSTS_PATH, "w") as f:
            json.dump(data, f)
    except Exception as exc:
        logger.warning(f"Failed to save last post timestamps: {exc}")


def _slot_key(hour: int) -> str:
    """Return the last_posts dict key for a given hour."""
    if hour == 8:
        return "last_8am_post"
    elif hour == 17:
        return "last_5pm_post"
    return f"last_{hour}h_post"


def _already_posted_today(last_posts: dict, hour: int) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return last_posts.get(_slot_key(hour)) == today


def _mark_posted_today(last_posts: dict, hour: int) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_posts[_slot_key(hour)] = today
    last_posts["updated_at"] = datetime.now(timezone.utc).isoformat()


def fetch_trade_stats_from_db(db_path: str) -> dict:
    """Read trade history from the polybot SQLite database and compute stats.

    Separates realized P&L (closed trades only) from unrealized P&L (open positions).
    Win rate is computed from closed trades only to avoid inflated metrics.
    """
    stats = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "avg_profit": 0.0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "total_pnl": 0.0,           # Realized P&L only (closed trades)
        "unrealized_pnl": 0.0,      # Unrealized P&L (open positions with expected pnl)
        "daily_pnl": 0.0,
        "weekly_pnl": 0.0,
        "open_positions": 0,
        "closed_trades": 0,
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

        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        trade_table = None
        for candidate in ("trades", "positions", "closed_positions"):
            if candidate in tables:
                trade_table = candidate
                break

        if not trade_table:
            stats["error"] = f"No trade table found. Tables: {tables}"
            conn.close()
            return stats

        # Closed trades only for realized P&L stats — exclude dry-run paper trades
        try:
            closed_rows = cur.execute(
                f"SELECT * FROM {trade_table} WHERE dry_run = 0 AND pnl IS NOT NULL "
                f"AND status IN ('won', 'lost', 'resolved') "
                f"ORDER BY closed_at DESC LIMIT 1000"
            ).fetchall()
        except Exception:
            # Fallback if dry_run column doesn't exist in this schema
            try:
                closed_rows = cur.execute(
                    f"SELECT * FROM {trade_table} WHERE pnl IS NOT NULL "
                    f"AND status IN ('won', 'lost', 'resolved') "
                    f"ORDER BY closed_at DESC LIMIT 1000"
                ).fetchall()
            except Exception:
                closed_rows = cur.execute(
                    f"SELECT * FROM {trade_table} WHERE pnl IS NOT NULL LIMIT 1000"
                ).fetchall()

        # Open positions for unrealized P&L — exclude dry-run paper trades
        try:
            open_rows = cur.execute(
                f"SELECT * FROM {trade_table} WHERE dry_run = 0 AND status = 'open'"
            ).fetchall()
        except Exception:
            try:
                open_rows = cur.execute(
                    f"SELECT * FROM {trade_table} WHERE status = 'open'"
                ).fetchall()
            except Exception:
                open_rows = []

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=7)

        realized_profits = []
        strategy_map: dict[str, list[float]] = {}

        for row in closed_rows:
            d = dict(row)
            pnl = float(d.get("pnl") or d.get("profit") or d.get("realized_pnl") or 0.0)
            realized_profits.append(pnl)

            strategy = d.get("strategy", d.get("strategy_name", "unknown"))
            strategy_map.setdefault(strategy, []).append(pnl)

            ts_raw = d.get("closed_at") or d.get("created_at") or d.get("resolved_at") or ""
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

        # Unrealized P&L from open positions (expected pnl field, if set)
        unrealized_total = 0.0
        open_count = len(open_rows)
        for row in open_rows:
            d = dict(row)
            expected = d.get("pnl")
            if expected is not None:
                unrealized_total += float(expected)

        stats["total_trades"] = len(realized_profits)
        stats["closed_trades"] = len(realized_profits)
        stats["open_positions"] = open_count
        stats["wins"] = sum(1 for p in realized_profits if p > 0)
        stats["losses"] = sum(1 for p in realized_profits if p <= 0)
        stats["win_rate"] = (stats["wins"] / stats["total_trades"] * 100) if realized_profits else 0.0
        stats["avg_profit"] = sum(realized_profits) / len(realized_profits) if realized_profits else 0.0
        stats["best_trade"] = max(realized_profits) if realized_profits else 0.0
        stats["worst_trade"] = min(realized_profits) if realized_profits else 0.0
        stats["total_pnl"] = sum(realized_profits)    # Realized P&L only
        stats["unrealized_pnl"] = unrealized_total    # Open positions expected P&L

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
    """
    Main Sage agent: compile Polymarket stats and post at 8AM UTC and 5PM UTC.
    Each slot (8AM, 5PM) is tracked separately — only posts once per slot per day.
    """
    logger.info("Sage agent starting...")

    now = datetime.now(timezone.utc)
    current_hour = now.hour

    # Only run during designated post hours
    if current_hour not in POST_HOURS:
        logger.info(f"Sage: current UTC hour is {current_hour}, not a post slot (8 or 17) — skipping.")
        return

    # Check if this specific slot has already been posted today
    last_posts = _load_last_posts()
    if _already_posted_today(last_posts, current_hour):
        slot_label = "8AM" if current_hour == 8 else "5PM"
        logger.info(f"Sage: {slot_label} UTC slot already posted today — skipping.")
        return

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    webhook_url = os.getenv("DISCORD_WEBHOOK_SAGE", "")
    discord = DiscordAlerts(bot_token=bot_token)

    db_stats = await asyncio.to_thread(fetch_trade_stats_from_db, DB_PATH)

    if isinstance(db_stats, Exception):
        logger.error(f"DB stats failed: {db_stats}")
        db_stats = {"error": str(db_stats), "total_trades": 0}

    realized_pnl = db_stats.get("total_pnl", 0.0)
    unrealized_pnl = db_stats.get("unrealized_pnl", 0.0)
    slot_label = "8AM" if current_hour == 8 else "5PM"

    fields = [
        {
            "name": "Realized P&L (Closed Trades Only)",
            "value": (
                f"Realized P&L: ${realized_pnl:+.4f}\n"
                f"Daily Realized: ${db_stats.get('daily_pnl', 0):+.4f}\n"
                f"Weekly Realized: ${db_stats.get('weekly_pnl', 0):+.4f}"
            ),
            "inline": True,
        },
        {
            "name": "Unrealized (Open Positions)",
            "value": (
                f"Unrealized P&L: ${unrealized_pnl:+.4f}\n"
                f"Open Positions: {db_stats.get('open_positions', 0)}\n"
                f"Closed Trades: {db_stats.get('closed_trades', 0)}"
            ),
            "inline": True,
        },
    ]

    if db_stats.get("error"):
        fields.append({
            "name": "Trade Statistics",
            "value": f"DB error: {db_stats['error']}",
            "inline": False,
        })
    else:
        total_trades = db_stats.get("closed_trades", db_stats.get("total_trades", 0))
        win_rate = db_stats.get("win_rate", 0.0)
        avg_profit = db_stats.get("avg_profit", 0.0)
        best = db_stats.get("best_trade", 0.0)
        worst = db_stats.get("worst_trade", 0.0)

        fields.append({
            "name": "Trade Statistics (Closed Trades Only)",
            "value": (
                f"Closed Trades: {total_trades}\n"
                f"Wins: {db_stats.get('wins', 0)} | Losses: {db_stats.get('losses', 0)}\n"
                f"Win Rate: {win_rate:.1f}%  ← closed trades only\n"
                f"Avg Profit: ${avg_profit:+.4f}\n"
                f"Best Trade: ${best:+.4f} | Worst: ${worst:+.4f}"
            ),
            "inline": False,
        })

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

    daily = db_stats.get("daily_pnl", 0.0)
    weekly = db_stats.get("weekly_pnl", 0.0)
    max_abs = max(abs(daily), abs(weekly), 0.01)
    chart = (
        f"Today  {_ascii_bar(daily, max_abs)} ${daily:+.4f}  (realized)\n"
        f"7-Day  {_ascii_bar(weekly, max_abs)} ${weekly:+.4f}  (realized)"
    )
    fields.append({
        "name": "Realized P&L Chart (ASCII)",
        "value": f"```\n{chart}\n```",
        "inline": False,
    })

    color = 0x00C851 if realized_pnl >= 0 else 0xFF4444

    embed = {
        "title": f"Sage Dashboard Report — {slot_label} UTC",
        "description": f"Polymarket snapshot — {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "color": color,
        "fields": fields,
        "timestamp": now.isoformat(),
        "footer": {"text": "PolyBot Sage Agent"},
    }

    if webhook_url:
        await discord.send_webhook(
            webhook_url,
            embed=embed,
            username="Sage",
            avatar_url="https://i.imgur.com/OB0y6MR.png",
        )
    else:
        await discord._post_channel_message(ANALYST_CHANNEL, embed)
    logger.info(f"Sage report posted to #sage-analytics ({slot_label} UTC slot).")

    _mark_posted_today(last_posts, current_hour)
    _save_last_posts(last_posts)
