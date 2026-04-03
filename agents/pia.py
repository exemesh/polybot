"""
Pia — Analytics & Risk Agent → #pia
  • P&L reports at 8AM and 5PM UTC
  • Risk alerts on emergencies only (halt, critical loss)
Deduplicates all alerts and reports per day.
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import httpx

from utils.discord_alerts import DiscordAlerts

logger = logging.getLogger("polybot.pia")

# Single Pia channel for both analytics and risk alerts
PIA_CHANNEL = "1483029691689341110"   # #pia

POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "polybot.db")
BOT_CONTROL_PATH = os.path.join(DATA_DIR, "bot_control.json")

# Kalbot status.json — sibling directory
_POLYBOT_ROOT = os.path.dirname(os.path.dirname(__file__))
KALBOT_STATUS_PATH = os.path.join(_POLYBOT_ROOT, "..", "kalbot", "data", "status.json")

ANALYTICS_LAST_POSTS_PATH = os.path.join(DATA_DIR, "pia_analytics_last_posts.json")
RISK_SENT_ALERTS_PATH = os.path.join(DATA_DIR, "pia_sent_alerts.json")

# Analytics post slots (UTC hours)
ANALYTICS_POST_HOURS = {8, 17}

# Risk thresholds
DAILY_LOSS_CRITICAL_PCT = 8.0

# Embed colors
COLOR_GREEN = 0x00C851
COLOR_RED = 0xFF4444
COLOR_YELLOW = 0xFFFF00


# ── Analytics: last-posts helpers ───────────────────────────────────────────

def _load_analytics_posts() -> dict:
    try:
        if os.path.exists(ANALYTICS_LAST_POSTS_PATH):
            with open(ANALYTICS_LAST_POSTS_PATH) as f:
                return json.load(f)
    except Exception as exc:
        logger.warning(f"Failed to load analytics post timestamps: {exc}")
    return {}


def _save_analytics_posts(data: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(ANALYTICS_LAST_POSTS_PATH, "w") as f:
            json.dump(data, f)
    except Exception as exc:
        logger.warning(f"Failed to save analytics post timestamps: {exc}")


def _analytics_slot_key(hour: int) -> str:
    return "last_8am_post" if hour == 8 else "last_5pm_post"


def _analytics_already_posted(last_posts: dict, hour: int) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return last_posts.get(_analytics_slot_key(hour)) == today


def _analytics_mark_posted(last_posts: dict, hour: int) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_posts[_analytics_slot_key(hour)] = today
    last_posts["updated_at"] = datetime.now(timezone.utc).isoformat()


# ── Risk: sent-alerts helpers ────────────────────────────────────────────────

def _load_sent_alerts() -> dict:
    try:
        if os.path.exists(RISK_SENT_ALERTS_PATH):
            with open(RISK_SENT_ALERTS_PATH) as f:
                data = json.load(f)
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if data.get("date") == today:
                    return data
    except Exception as exc:
        logger.warning(f"Failed to load sent alerts: {exc}")
    return {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "alerts": []}


def _save_sent_alerts(data: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(RISK_SENT_ALERTS_PATH, "w") as f:
            json.dump(data, f)
    except Exception as exc:
        logger.warning(f"Failed to save sent alerts: {exc}")


def _already_sent(sent_data: dict, alert_key: str) -> bool:
    return alert_key in sent_data.get("alerts", [])


def _mark_sent(sent_data: dict, alert_key: str) -> None:
    sent_data.setdefault("alerts", [])
    if alert_key not in sent_data["alerts"]:
        sent_data["alerts"].append(alert_key)


# ── Database helpers ─────────────────────────────────────────────────────────

def load_kalbot_status() -> dict:
    """Load kalbot's status.json for cross-bot reporting."""
    path = os.path.normpath(KALBOT_STATUS_PATH)
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as exc:
        logger.warning(f"Pia: kalbot status.json read failed: {exc}")
    return {}


def load_bot_control(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as exc:
        logger.warning(f"bot_control.json read failed: {exc}")
    return {}


def fetch_trade_stats_from_db(db_path: str) -> dict:
    """Read trade history from polybot SQLite and compute stats.
    Separates realized P&L (closed trades) from unrealized (open positions).
    """
    stats = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "avg_profit": 0.0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "total_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "daily_pnl": 0.0,
        "weekly_pnl": 0.0,
        "open_positions": 0,
        "closed_trades": 0,
        "strategy_stats": {},
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

        # Closed trades (realized P&L)
        try:
            closed_rows = cur.execute(
                f"SELECT * FROM {trade_table} WHERE dry_run = 0 AND pnl IS NOT NULL "
                f"AND status IN ('won', 'lost', 'resolved') ORDER BY closed_at DESC LIMIT 1000"
            ).fetchall()
        except Exception:
            try:
                closed_rows = cur.execute(
                    f"SELECT * FROM {trade_table} WHERE pnl IS NOT NULL "
                    f"AND status IN ('won', 'lost', 'resolved') ORDER BY closed_at DESC LIMIT 1000"
                ).fetchall()
            except Exception:
                closed_rows = cur.execute(
                    f"SELECT * FROM {trade_table} WHERE pnl IS NOT NULL LIMIT 1000"
                ).fetchall()

        # Open positions (unrealized P&L)
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

        # Open position details
        open_position_details = []
        for row in open_rows:
            d = dict(row)
            token_id = d.get("token_id", "")
            entry_price = float(d.get("price") or 0.0)
            size_usd = float(d.get("size_usd") or 0.0)
            side = (d.get("side") or "BUY").upper()
            question = d.get("market_question", "")[:60]
            shares = (size_usd / entry_price) if entry_price > 0 else 0.0
            open_position_details.append({
                "token_id": token_id,
                "entry_price": entry_price,
                "size_usd": size_usd,
                "shares": shares,
                "side": side,
                "question": question,
                "current_price": entry_price,
            })

        stats["total_trades"] = len(realized_profits)
        stats["closed_trades"] = len(realized_profits)
        stats["open_positions"] = len(open_rows)
        stats["open_position_details"] = open_position_details
        stats["wins"] = sum(1 for p in realized_profits if p > 0)
        stats["losses"] = sum(1 for p in realized_profits if p <= 0)
        stats["win_rate"] = (stats["wins"] / stats["total_trades"] * 100) if realized_profits else 0.0
        stats["avg_profit"] = sum(realized_profits) / len(realized_profits) if realized_profits else 0.0
        stats["best_trade"] = max(realized_profits) if realized_profits else 0.0
        stats["worst_trade"] = min(realized_profits) if realized_profits else 0.0
        stats["total_pnl"] = sum(realized_profits)
        stats["unrealized_pnl"] = 0.0  # updated below after live price fetch

        for strat, pnls in strategy_map.items():
            stats["strategy_stats"][strat] = {
                "count": len(pnls),
                "total": round(sum(pnls), 4),
                "avg": round(sum(pnls) / len(pnls), 4) if pnls else 0,
                "wins": sum(1 for p in pnls if p > 0),
            }

        conn.close()
        stats["_open_position_details_raw"] = open_position_details
    except Exception as exc:
        stats["error"] = str(exc)
        logger.error(f"DB read error: {exc}")

    return stats


def fetch_daily_pnl_from_db(db_path: str) -> float | None:
    """Read today's P&L from SQLite for risk checks. Returns None on error."""
    if not os.path.exists(db_path):
        return None
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
            conn.close()
            return None

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        try:
            rows = cur.execute(
                f"SELECT pnl, profit, realized_pnl, created_at, closed_at, resolved_at FROM {trade_table} "
                f"WHERE (created_at >= ? OR closed_at >= ? OR resolved_at >= ?)",
                (today_start, today_start, today_start),
            ).fetchall()
        except Exception:
            rows = []

        daily_pnl = sum(
            float(dict(r).get("pnl") or dict(r).get("profit") or dict(r).get("realized_pnl") or 0.0)
            for r in rows
        )
        conn.close()
        return daily_pnl
    except Exception as exc:
        logger.error(f"DB daily P&L read error: {exc}")
        return None


# ── Analytics: live price enrichment ────────────────────────────────────────

async def _enrich_open_positions(open_positions: list[dict]) -> tuple[list[dict], float]:
    """Fetch live prices from Gamma API and compute unrealized P&L."""
    if not open_positions:
        return open_positions, 0.0

    token_ids = [p["token_id"] for p in open_positions if p["token_id"]]
    price_map: dict[str, float] = {}

    if token_ids:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for tid in token_ids:
                    try:
                        r = await client.get(
                            f"{POLYMARKET_GAMMA_URL}/markets",
                            params={"clob_token_ids": tid, "limit": 1},
                        )
                        if r.status_code == 200:
                            data = r.json()
                            if data:
                                m = data[0] if isinstance(data, list) else data
                                price = float(m.get("bestBid") or m.get("lastTradePrice") or 0)
                                if price > 0:
                                    price_map[tid] = price
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Pia: live price fetch failed: {e}")

    unrealized_total = 0.0
    for pos in open_positions:
        tid = pos["token_id"]
        if tid in price_map:
            pos["current_price"] = price_map[tid]
        entry = pos["entry_price"]
        current = pos["current_price"]
        shares = pos["shares"]
        if entry > 0 and shares > 0:
            unrealized_total += (current - entry) * shares

    return open_positions, round(unrealized_total, 4)


# ── Analytics helpers ────────────────────────────────────────────────────────

def _ascii_bar(value: float, max_val: float, width: int = 12) -> str:
    if max_val == 0:
        return "[" + " " * width + "]"
    filled = min(int(abs(value) / max_val * width), width)
    char = "+" if value >= 0 else "-"
    return "[" + char * filled + " " * (width - filled) + "]"


async def _run_analytics(discord: DiscordAlerts, current_hour: int, now: datetime) -> None:
    """Post the P&L analytics report for the current time slot."""
    if current_hour not in ANALYTICS_POST_HOURS:
        logger.info(f"Pia: UTC hour {current_hour} is not an analytics slot (8 or 17) — skipping.")
        return

    last_posts = _load_analytics_posts()
    if _analytics_already_posted(last_posts, current_hour):
        slot_label = "8AM" if current_hour == 8 else "5PM"
        logger.info(f"Pia: {slot_label} analytics slot already posted today — skipping.")
        return

    db_stats = await asyncio.to_thread(fetch_trade_stats_from_db, DB_PATH)
    if isinstance(db_stats, Exception):
        db_stats = {"error": str(db_stats), "total_trades": 0}

    # Enrich open positions with live prices
    open_positions = db_stats.get("_open_position_details_raw") or db_stats.get("open_position_details", [])
    if open_positions:
        open_positions, unrealized_pnl = await _enrich_open_positions(open_positions)
        db_stats["unrealized_pnl"] = unrealized_pnl
        db_stats["open_position_details"] = open_positions

    realized_pnl = db_stats.get("total_pnl", 0.0)
    unrealized_pnl = db_stats.get("unrealized_pnl", 0.0)
    slot_label = "8AM" if current_hour == 8 else "5PM"

    open_details = db_stats.get("open_position_details", [])
    if open_details:
        pos_lines = []
        for p in open_details[:6]:
            entry = p.get("entry_price", 0)
            current = p.get("current_price", entry)
            upnl = (current - entry) * p.get("shares", 0)
            arrow = "📈" if upnl >= 0 else "📉"
            pos_lines.append(
                f"{arrow} {p.get('question', '?')[:50]}\n"
                f"  Entry: {entry:.3f} → Now: {current:.3f} | uP&L: ${upnl:+.2f}"
            )
        open_positions_text = "\n".join(pos_lines) or "No open positions"
    else:
        open_positions_text = f"{db_stats.get('open_positions', 0)} open positions (prices unavailable)"

    fields = [
        {
            "name": "Realized P&L (Closed Trades)",
            "value": (
                f"Realized P&L: ${realized_pnl:+.4f}\n"
                f"Daily Realized: ${db_stats.get('daily_pnl', 0):+.4f}\n"
                f"Weekly Realized: ${db_stats.get('weekly_pnl', 0):+.4f}"
            ),
            "inline": True,
        },
        {
            "name": "Unrealized (Live Prices)",
            "value": (
                f"Unrealized P&L: ${unrealized_pnl:+.4f}\n"
                f"Open Positions: {db_stats.get('open_positions', 0)}\n"
                f"Closed Trades: {db_stats.get('closed_trades', 0)}"
            ),
            "inline": True,
        },
        {
            "name": "Open Positions (Live)",
            "value": open_positions_text[:1000],
            "inline": False,
        },
    ]

    if db_stats.get("error"):
        fields.append({"name": "Trade Statistics", "value": f"DB error: {db_stats['error']}", "inline": False})
    else:
        total_trades = db_stats.get("closed_trades", db_stats.get("total_trades", 0))
        win_rate = db_stats.get("win_rate", 0.0)
        avg_profit = db_stats.get("avg_profit", 0.0)
        best = db_stats.get("best_trade", 0.0)
        worst = db_stats.get("worst_trade", 0.0)
        fields.append({
            "name": "Trade Statistics (Closed Trades)",
            "value": (
                f"Closed Trades: {total_trades}\n"
                f"Wins: {db_stats.get('wins', 0)} | Losses: {db_stats.get('losses', 0)}\n"
                f"Win Rate: {win_rate:.1f}%\n"
                f"Avg Profit: ${avg_profit:+.4f}\n"
                f"Best: ${best:+.4f} | Worst: ${worst:+.4f}"
            ),
            "inline": False,
        })

    strategy_stats = db_stats.get("strategy_stats", {})
    if strategy_stats:
        sorted_strats = sorted(strategy_stats.items(), key=lambda x: x[1]["total"], reverse=True)
        best_strats = sorted_strats[:3]
        worst_strats = sorted_strats[-2:] if len(sorted_strats) > 3 else []
        fields.append({
            "name": "Top Strategies",
            "value": "\n".join(f"• {s}: ${v['total']:+.4f} ({v['wins']}/{v['count']} wins)" for s, v in best_strats) or "N/A",
            "inline": False,
        })
        if worst_strats:
            fields.append({
                "name": "Worst Strategies",
                "value": "\n".join(f"• {s}: ${v['total']:+.4f} ({v['wins']}/{v['count']} wins)" for s, v in worst_strats),
                "inline": False,
            })

    daily = db_stats.get("daily_pnl", 0.0)
    weekly = db_stats.get("weekly_pnl", 0.0)
    max_abs = max(abs(daily), abs(weekly), 0.01)
    fields.append({
        "name": "Realized P&L Chart",
        "value": (
            f"```\n"
            f"Today  {_ascii_bar(daily, max_abs)} ${daily:+.4f}  (realized)\n"
            f"7-Day  {_ascii_bar(weekly, max_abs)} ${weekly:+.4f}  (realized)\n"
            f"```"
        ),
        "inline": False,
    })

    # ── Kalbot section ────────────────────────────────────────────────────────
    kalbot_status = await asyncio.to_thread(load_kalbot_status)
    if kalbot_status:
        k_balance   = kalbot_status.get("balance_usd", 0.0)
        k_daily     = kalbot_status.get("daily_pnl", 0.0)
        k_total     = kalbot_status.get("total_pnl", 0.0)
        k_win_rate  = kalbot_status.get("win_rate_pct", 0.0)
        k_open      = kalbot_status.get("open_positions", 0)
        k_trades    = kalbot_status.get("trades_today", 0)
        k_dry       = kalbot_status.get("dry_run", True)
        k_updated   = kalbot_status.get("updated_at", "unknown")
        k_mode      = "DRY RUN" if k_dry else "LIVE"
        fields.append({
            "name": "─── Kalshi (KalBot) ───",
            "value": (
                f"Mode: **{k_mode}** | Balance: **${k_balance:.2f}**\n"
                f"Daily P&L: ${k_daily:+.2f} | Total P&L: ${k_total:+.2f}\n"
                f"Win Rate: {k_win_rate:.1f}% | Open: {k_open} | Trades Today: {k_trades}\n"
                f"Last update: {k_updated[:16]}"
            ),
            "inline": False,
        })
    else:
        fields.append({
            "name": "─── Kalshi (KalBot) ───",
            "value": "Status unavailable — kalbot may not have run yet",
            "inline": False,
        })

    color = COLOR_GREEN if realized_pnl >= 0 else COLOR_RED
    embed = {
        "title": f"Pia Analytics Report — {slot_label} UTC",
        "description": f"Polymarket + Kalshi snapshot — {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "color": color,
        "fields": fields,
        "timestamp": now.isoformat(),
        "footer": {"text": "Pia — Analytics & Risk"},
    }

    webhook_url = os.getenv("DISCORD_WEBHOOK_PIA", "")
    if webhook_url:
        await discord.send_webhook(
            webhook_url, embed=embed,
            username="Pia",
            avatar_url="https://i.imgur.com/OB0y6MR.png",
        )
    else:
        await discord._post_channel_message(PIA_CHANNEL, embed)
    logger.info(f"Pia analytics report posted ({slot_label} UTC).")

    _analytics_mark_posted(last_posts, current_hour)
    _save_analytics_posts(last_posts)


# ── Risk monitoring ──────────────────────────────────────────────────────────

async def _run_risk_monitor(discord: DiscordAlerts, now: datetime) -> None:
    """Check for trading halts and critical P&L loss. Post alerts only when needed."""
    control, daily_pnl = await asyncio.gather(
        asyncio.to_thread(load_bot_control, BOT_CONTROL_PATH),
        asyncio.to_thread(fetch_daily_pnl_from_db, DB_PATH),
        return_exceptions=True,
    )

    if isinstance(control, Exception):
        logger.error(f"Pia: control load error: {control}")
        control = {}
    if isinstance(daily_pnl, Exception):
        logger.error(f"Pia: daily P&L fetch error: {daily_pnl}")
        daily_pnl = None

    sent_data = _load_sent_alerts()
    alert_count = 0
    now_iso = now.isoformat()
    webhook_url = os.getenv("DISCORD_WEBHOOK_PIA", "")

    # ── 1. Bot halt check ──────────────────────────────────────────────────
    if control:
        trading_enabled = control.get("trading_enabled", True)
        halt_reason = control.get("halt_reason")
        mode = control.get("mode", "unknown")

        if not trading_enabled:
            alert_key = "trading_halted"
            if not _already_sent(sent_data, alert_key):
                embed = {
                    "title": "Pia Alert — PolyBot Trading HALTED",
                    "description": (
                        f"Trading is currently **disabled** in bot_control.json.\n"
                        f"Mode: {mode}\n"
                        f"Halt Reason: {halt_reason or 'Not specified'}"
                    ),
                    "color": COLOR_YELLOW,
                    "timestamp": now_iso,
                    "footer": {"text": "PolyBot Pia — Risk"},
                }
                if webhook_url:
                    await discord.send_webhook(
                        webhook_url, embed=embed,
                        username="Pia",
                        avatar_url="https://i.imgur.com/OB0y6MR.png",
                    )
                else:
                    await discord._post_channel_message(PIA_CHANNEL, embed)
                _mark_sent(sent_data, alert_key)
                alert_count += 1
                logger.warning("Pia: trading is halted — alert sent.")
            else:
                logger.info("Pia: trading_halted alert already sent today — skipping.")

    # ── 2. Critical daily loss check ───────────────────────────────────────
    if daily_pnl is not None and daily_pnl < 0:
        loss_abs = abs(daily_pnl)
        if loss_abs >= DAILY_LOSS_CRITICAL_PCT:
            alert_key = f"daily_loss_critical_{now.strftime('%Y-%m-%d')}"
            if not _already_sent(sent_data, alert_key):
                embed = {
                    "title": "CRITICAL — Polymarket Daily Loss Exceeded Threshold",
                    "description": (
                        f"Today's realized loss: **${abs(daily_pnl):+.4f}**\n"
                        f"This exceeds the ${DAILY_LOSS_CRITICAL_PCT:.2f} alert threshold.\n"
                        f"Consider reviewing open positions."
                    ),
                    "color": COLOR_RED,
                    "timestamp": now_iso,
                    "footer": {"text": "PolyBot Pia — Risk"},
                }
                if webhook_url:
                    await discord.send_webhook(
                        webhook_url, embed=embed,
                        username="Pia",
                        avatar_url="https://i.imgur.com/OB0y6MR.png",
                    )
                else:
                    await discord._post_channel_message(PIA_CHANNEL, embed)
                _mark_sent(sent_data, alert_key)
                alert_count += 1
                logger.warning(f"Pia: daily P&L critical loss alert sent (${daily_pnl:.4f}).")
            else:
                logger.info("Pia: daily loss alert already sent today — skipping.")

    # ── 3. Kalbot daily loss check ─────────────────────────────────────────
    kalbot_status = await asyncio.to_thread(load_kalbot_status)
    if kalbot_status:
        k_daily = kalbot_status.get("daily_pnl", 0.0)
        if k_daily is not None and k_daily < -DAILY_LOSS_CRITICAL_PCT:
            alert_key = f"kalbot_daily_loss_{now.strftime('%Y-%m-%d')}"
            if not _already_sent(sent_data, alert_key):
                embed = {
                    "title": "CRITICAL — Kalshi Daily Loss Exceeded Threshold",
                    "description": (
                        f"KalBot today's loss: **${abs(k_daily):.4f}**\n"
                        f"This exceeds the ${DAILY_LOSS_CRITICAL_PCT:.2f} alert threshold.\n"
                        f"Check KalBot open positions."
                    ),
                    "color": COLOR_RED,
                    "timestamp": now_iso,
                    "footer": {"text": "Pia — Risk"},
                }
                if webhook_url:
                    await discord.send_webhook(
                        webhook_url, embed=embed, username="Pia",
                        avatar_url="https://i.imgur.com/OB0y6MR.png",
                    )
                else:
                    await discord._post_channel_message(PIA_CHANNEL, embed)
                _mark_sent(sent_data, alert_key)
                alert_count += 1
                logger.warning(f"Pia: KalBot daily loss alert sent (${k_daily:.4f}).")

    _save_sent_alerts(sent_data)
    logger.info(f"Pia risk check complete. {alert_count} alert(s) sent.")


# ── Main entry point ─────────────────────────────────────────────────────────

async def run_pia() -> None:
    """
    Main Pia Analytics & Risk agent.

    Each run:
      1. Run risk monitor — post alerts for trading halts or critical losses (always).
      2. At 8AM or 5PM UTC (once per slot), post the full P&L analytics report.
    """
    logger.info("Pia agent starting...")

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord = DiscordAlerts(bot_token=bot_token)
    now = datetime.now(timezone.utc)

    # Risk check runs every cycle (emergency-only, deduplicated)
    await _run_risk_monitor(discord, now)

    # Analytics report runs only at scheduled slots
    await _run_analytics(discord, now.hour, now)

    logger.info("Pia agent complete.")
