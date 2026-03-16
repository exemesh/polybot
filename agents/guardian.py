"""
Sentinel Agent — Polymarket risk monitoring → #guardian-alerts
Posts ONLY on actual alerts. No all-clear messages. Deduplicates per day.
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from utils.discord_alerts import DiscordAlerts

logger = logging.getLogger("polybot.guardian")

GUARDIAN_CHANNEL = "1483029707329896471"

BOT_CONTROL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "bot_control.json"
)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SENT_ALERTS_PATH = os.path.join(DATA_DIR, "guardian_sent_alerts.json")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polybot.db")

# Alert thresholds
DAILY_LOSS_CRITICAL_PCT = 8.0

# Discord embed colors
COLOR_RED = 0xFF4444
COLOR_YELLOW = 16776960   # 0xFFFF00
COLOR_GREEN = 0x00C851


def _load_sent_alerts() -> dict:
    """Load the sent alerts registry for today."""
    try:
        if os.path.exists(SENT_ALERTS_PATH):
            with open(SENT_ALERTS_PATH) as f:
                data = json.load(f)
                # Reset if from a previous day
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if data.get("date") == today:
                    return data
    except Exception as exc:
        logger.warning(f"Failed to load sent alerts: {exc}")
    return {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "alerts": []}


def _save_sent_alerts(data: dict) -> None:
    """Persist the sent alerts registry."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SENT_ALERTS_PATH, "w") as f:
            json.dump(data, f)
    except Exception as exc:
        logger.warning(f"Failed to save sent alerts: {exc}")


def _already_sent(sent_data: dict, alert_key: str) -> bool:
    return alert_key in sent_data.get("alerts", [])


def _mark_sent(sent_data: dict, alert_key: str) -> None:
    sent_data.setdefault("alerts", [])
    if alert_key not in sent_data["alerts"]:
        sent_data["alerts"].append(alert_key)


def load_bot_control(path: str) -> dict:
    """Load the polybot bot_control.json file."""
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as exc:
        logger.warning(f"bot_control.json read failed: {exc}")
    return {}


def fetch_daily_pnl_from_db(db_path: str) -> float | None:
    """Read today's P&L from the SQLite DB. Returns None on error."""
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

        daily_pnl = 0.0
        for row in rows:
            d = dict(row)
            pnl = float(d.get("pnl") or d.get("profit") or d.get("realized_pnl") or 0.0)
            daily_pnl += pnl

        conn.close()
        return daily_pnl
    except Exception as exc:
        logger.error(f"DB daily P&L read error: {exc}")
        return None


async def run_guardian() -> None:
    """Main Sentinel agent: monitor Polymarket risk and post only actual alerts."""
    logger.info("Sentinel agent starting...")

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord = DiscordAlerts(bot_token=bot_token)

    control, daily_pnl = await asyncio.gather(
        asyncio.to_thread(load_bot_control, BOT_CONTROL_PATH),
        asyncio.to_thread(fetch_daily_pnl_from_db, DB_PATH),
        return_exceptions=True,
    )

    if isinstance(control, Exception):
        logger.error(f"Sentinel: control load error: {control}")
        control = {}
    if isinstance(daily_pnl, Exception):
        logger.error(f"Sentinel: daily P&L fetch error: {daily_pnl}")
        daily_pnl = None

    sent_data = _load_sent_alerts()
    alert_count = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── 1. Bot control file checks ──
    if control:
        trading_enabled = control.get("trading_enabled", True)
        halt_reason = control.get("halt_reason")
        mode = control.get("mode", "unknown")

        if not trading_enabled:
            alert_key = "trading_halted"
            if not _already_sent(sent_data, alert_key):
                embed = {
                    "title": "Sentinel Alert — PolyBot Trading HALTED",
                    "description": (
                        f"Trading is currently **disabled** in bot_control.json.\n"
                        f"Mode: {mode}\n"
                        f"Halt Reason: {halt_reason or 'Not specified'}"
                    ),
                    "color": COLOR_YELLOW,
                    "timestamp": now_iso,
                    "footer": {"text": "PolyBot Sentinel"},
                }
                await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
                _mark_sent(sent_data, alert_key)
                alert_count += 1
                logger.warning("Sentinel: trading is halted — alert sent.")
            else:
                logger.info("Sentinel: trading_halted alert already sent today — skipping.")

    # ── 2. Daily P&L critical loss check ──
    if daily_pnl is not None and daily_pnl < 0:
        loss_pct = abs(daily_pnl)
        # Alert if daily loss exceeds threshold (using absolute USDC loss as proxy)
        if loss_pct >= DAILY_LOSS_CRITICAL_PCT:
            alert_key = f"daily_loss_critical_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
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
                    "footer": {"text": "PolyBot Sentinel"},
                }
                await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
                _mark_sent(sent_data, alert_key)
                alert_count += 1
                logger.warning(f"Sentinel: daily P&L critical loss alert sent (${daily_pnl:.4f}).")
            else:
                logger.info("Sentinel: daily loss alert already sent today — skipping.")

    _save_sent_alerts(sent_data)
    logger.info(f"Sentinel check complete. {alert_count} alert(s) sent to #guardian-alerts.")
