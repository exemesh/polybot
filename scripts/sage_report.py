"""
Sage On-Demand Report Script
Run manually to generate and post a Sage report to Discord immediately.

Usage:
    cd /path/to/polybot
    python3 scripts/sage_report.py

Reads polybot.db for trade data and posts to DISCORD_WEBHOOK_SAGE.
No schedule checks — always posts when run.
"""

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Allow imports from the project root (so core/, utils/ resolve correctly)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from utils.discord_alerts import DiscordAlerts

# ── Paths ────────────────────────────────────────────────────────────────────

DB_PATH = ROOT / "data" / "polybot.db"


# ── DB helpers (adapted from agents/analyst.py) ───────────────────────────────

def fetch_trade_stats(db_path: Path) -> dict:
    """Query polybot.db and return portfolio stats."""
    stats = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "avg_profit": 0.0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "daily_pnl": 0.0,
        "open_positions": 0,
        "closed_trades": 0,
        "recent_trades": [],
        "error": None,
    }

    if not db_path.exists():
        stats["error"] = f"DB not found at {db_path}"
        return stats

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        tables = {
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        trade_table = None
        for candidate in ("trades", "positions", "closed_positions"):
            if candidate in tables:
                trade_table = candidate
                break

        if not trade_table:
            stats["error"] = f"No trade table found. Tables present: {sorted(tables)}"
            conn.close()
            return stats

        # ── Closed trades (realized P&L) ─────────────────────────────────────
        try:
            closed_rows = cur.execute(
                f"SELECT * FROM {trade_table} "
                f"WHERE pnl IS NOT NULL "
                f"AND status IN ('won', 'lost', 'resolved') "
                f"ORDER BY closed_at DESC LIMIT 1000"
            ).fetchall()
        except Exception:
            closed_rows = cur.execute(
                f"SELECT * FROM {trade_table} WHERE pnl IS NOT NULL LIMIT 1000"
            ).fetchall()

        # ── Open positions (unrealized P&L) ──────────────────────────────────
        try:
            open_rows = cur.execute(
                f"SELECT * FROM {trade_table} WHERE status = 'open'"
            ).fetchall()
        except Exception:
            open_rows = []

        # ── Recent trades (last 5, any status) ───────────────────────────────
        try:
            recent_rows = cur.execute(
                f"SELECT * FROM {trade_table} ORDER BY rowid DESC LIMIT 5"
            ).fetchall()
        except Exception:
            recent_rows = []

        conn.close()

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        realized_profits = []
        for row in closed_rows:
            d = dict(row)
            pnl = float(
                d.get("pnl") or d.get("profit") or d.get("realized_pnl") or 0.0
            )
            realized_profits.append(pnl)

            ts_raw = (
                d.get("closed_at")
                or d.get("resolved_at")
                or d.get("created_at")
                or ""
            )
            try:
                if ts_raw:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= today_start:
                        stats["daily_pnl"] += pnl
            except Exception:
                pass

        unrealized_total = 0.0
        for row in open_rows:
            d = dict(row)
            expected = d.get("pnl")
            if expected is not None:
                unrealized_total += float(expected)

        stats["closed_trades"] = len(realized_profits)
        stats["total_trades"] = len(realized_profits)
        stats["open_positions"] = len(open_rows)
        stats["wins"] = sum(1 for p in realized_profits if p > 0)
        stats["losses"] = sum(1 for p in realized_profits if p <= 0)
        stats["win_rate"] = (
            stats["wins"] / stats["total_trades"] * 100
            if realized_profits
            else 0.0
        )
        stats["avg_profit"] = (
            sum(realized_profits) / len(realized_profits) if realized_profits else 0.0
        )
        stats["best_trade"] = max(realized_profits) if realized_profits else 0.0
        stats["worst_trade"] = min(realized_profits) if realized_profits else 0.0
        stats["realized_pnl"] = sum(realized_profits)
        stats["unrealized_pnl"] = unrealized_total

        # Build recent trade list for display
        recent = []
        for row in recent_rows:
            d = dict(row)
            market = str(d.get("market", d.get("market_id", d.get("question", "Unknown"))))[:60]
            side = str(d.get("side", d.get("outcome", "?")))
            pnl_val = d.get("pnl") or d.get("profit") or d.get("realized_pnl")
            pnl_str = f"${float(pnl_val):+.4f}" if pnl_val is not None else "open"
            status = str(d.get("status", "?"))
            recent.append(f"• {market} | {side} | {pnl_str} [{status}]")
        stats["recent_trades"] = recent

    except Exception as exc:
        stats["error"] = str(exc)

    return stats


# ── Report formatting ─────────────────────────────────────────────────────────

def build_report_content(stats: dict) -> str:
    """Format the on-demand Sage report as a Discord code block."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    divider = "━" * 24

    if stats.get("error") and stats["total_trades"] == 0:
        return (
            "🧠 No paper trading data yet — bot is running but no trades recorded. "
            "Check back after the next cycle."
        )

    lines = [
        f"🧠 SAGE ON-DEMAND REPORT",
        divider,
        f"📊 PORTFOLIO SNAPSHOT",
        f"Total Trades: {stats['total_trades']}",
        f"Open Positions: {stats['open_positions']}",
        f"Closed Trades: {stats['closed_trades']}",
        f"Win Rate: {stats['win_rate']:.1f}%",
        f"",
        f"💰 P&L SUMMARY",
        f"Realized P&L: ${stats['realized_pnl']:+.4f}",
        f"Unrealized P&L: ${stats['unrealized_pnl']:+.4f}",
        f"Daily P&L: ${stats['daily_pnl']:+.4f}",
    ]

    if stats["recent_trades"]:
        lines += [
            f"",
            f"📈 RECENT TRADES (last 5)",
        ]
        lines.extend(stats["recent_trades"])
    else:
        lines += [
            f"",
            f"📈 RECENT TRADES",
            f"No trades recorded yet.",
        ]

    if stats.get("error"):
        lines += [f"", f"⚠️ Note: {stats['error']}"]

    lines += [divider, f"Generated: {now_str}"]

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK_SAGE", "")
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")

    if not webhook_url:
        print(
            "WARNING: DISCORD_WEBHOOK_SAGE is not set. "
            "Set it in .env or as an environment variable."
        )

    print(f"Reading database: {DB_PATH}")
    stats = fetch_trade_stats(DB_PATH)

    if stats.get("error"):
        print(f"DB note: {stats['error']}")

    report_text = build_report_content(stats)
    print("\n--- REPORT PREVIEW ---")
    print(report_text)
    print("--- END PREVIEW ---\n")

    discord = DiscordAlerts(bot_token=bot_token)

    # Build embed
    has_data = stats["total_trades"] > 0
    color = 0x00C851 if stats.get("realized_pnl", 0) >= 0 else 0xFF4444
    if not has_data:
        color = 0xFFBB33  # yellow — no data yet

    now = datetime.now(timezone.utc)

    if has_data:
        fields = [
            {
                "name": "Portfolio Snapshot",
                "value": (
                    f"Total Trades: {stats['total_trades']}\n"
                    f"Open Positions: {stats['open_positions']}\n"
                    f"Closed Trades: {stats['closed_trades']}\n"
                    f"Win Rate: {stats['win_rate']:.1f}%"
                ),
                "inline": True,
            },
            {
                "name": "P&L Summary",
                "value": (
                    f"Realized P&L: ${stats['realized_pnl']:+.4f}\n"
                    f"Unrealized P&L: ${stats['unrealized_pnl']:+.4f}\n"
                    f"Daily P&L: ${stats['daily_pnl']:+.4f}"
                ),
                "inline": True,
            },
        ]
        if stats["recent_trades"]:
            fields.append({
                "name": "Recent Trades (last 5)",
                "value": "\n".join(stats["recent_trades"])[:1024],
                "inline": False,
            })
    else:
        fields = [
            {
                "name": "Status",
                "value": (
                    "No paper trading data yet — bot is running but no trades recorded.\n"
                    "Check back after the next cycle."
                ),
                "inline": False,
            }
        ]

    embed = {
        "title": "Sage On-Demand Report",
        "description": f"Polymarket snapshot — {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "color": color,
        "fields": fields,
        "timestamp": now.isoformat(),
        "footer": {"text": "PolyBot Sage Agent — On-Demand"},
    }

    if webhook_url:
        success = await discord.send_webhook(
            webhook_url,
            embed=embed,
            username="Sage",
            avatar_url="https://i.imgur.com/OB0y6MR.png",
        )
        if success:
            print("Report posted to Discord via DISCORD_WEBHOOK_SAGE.")
        else:
            print("Failed to post report to Discord. Check webhook URL and logs.")
    else:
        print("No webhook URL set — report not posted to Discord.")


if __name__ == "__main__":
    asyncio.run(main())
