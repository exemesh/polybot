"""
Guardian Agent — Risk monitoring for Binance positions + polybot state → #guardian-alerts
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timezone

import httpx

from utils.discord_alerts import DiscordAlerts

logger = logging.getLogger("polybot.guardian")

GUARDIAN_CHANNEL = "1483029707329896471"

BINANCE_SPOT_URL = "https://api.binance.com"
BINANCE_FUTURES_URL = "https://fapi.binance.com"

BOT_CONTROL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "bot_control.json"
)

# Alert thresholds
WARN_AGAINST_PCT = 5.0      # warn if position moves >5% against entry
CRITICAL_AGAINST_PCT = 10.0 # critical if >10% against entry
TAKE_PROFIT_PCT = 20.0      # suggest TP if position up >20%
DAILY_LOSS_CRITICAL_PCT = 8.0  # critical daily loss threshold

# Discord embed colors
COLOR_RED = 0xFF4444
COLOR_YELLOW = 16776960   # 0xFFFF00
COLOR_GREEN = 0x00C851


def _binance_sign(params: dict, secret: str) -> str:
    query = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig


async def fetch_futures_positions(api_key: str, secret: str) -> list[dict]:
    """Fetch active futures positions from Binance."""
    if not api_key or not secret:
        return []
    ts = int(time.time() * 1000)
    qs = _binance_sign({"timestamp": ts}, secret)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{BINANCE_FUTURES_URL}/fapi/v2/positionRisk?{qs}",
                headers={"X-MBX-APIKEY": api_key},
                timeout=15,
            )
            if resp.status_code == 200:
                return [p for p in resp.json() if float(p.get("positionAmt", 0)) != 0]
    except Exception as exc:
        logger.warning(f"Futures position fetch failed: {exc}")
    return []


async def fetch_futures_account(api_key: str, secret: str) -> dict:
    """Fetch futures account info (includes daily PnL)."""
    if not api_key or not secret:
        return {}
    ts = int(time.time() * 1000)
    qs = _binance_sign({"timestamp": ts}, secret)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{BINANCE_FUTURES_URL}/fapi/v2/account?{qs}",
                headers={"X-MBX-APIKEY": api_key},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:
        logger.warning(f"Futures account fetch failed: {exc}")
    return {}


def load_bot_control(path: str) -> dict:
    """Load the polybot bot_control.json file."""
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as exc:
        logger.warning(f"bot_control.json read failed: {exc}")
    return {}


def _analyze_positions(positions: list[dict]) -> list[dict]:
    """Analyze futures positions and return a list of alert dicts."""
    alerts = []
    for p in positions:
        symbol = p.get("symbol", "Unknown")
        entry = float(p.get("entryPrice", 0))
        mark = float(p.get("markPrice", 0))
        amt = float(p.get("positionAmt", 0))
        unrealized = float(p.get("unRealizedProfit", 0))

        if entry <= 0 or mark <= 0:
            continue

        pnl_pct = (mark - entry) / entry * 100
        if amt < 0:  # SHORT position — flip sign
            pnl_pct = -pnl_pct

        if pnl_pct <= -CRITICAL_AGAINST_PCT:
            alerts.append({
                "level": "CRITICAL",
                "symbol": symbol,
                "pnl_pct": pnl_pct,
                "unrealized": unrealized,
                "entry": entry,
                "mark": mark,
                "message": (
                    f"**{symbol}** is down **{abs(pnl_pct):.2f}%** from entry.\n"
                    f"Entry: ${entry:,.4f} | Mark: ${mark:,.4f} | uPnL: ${unrealized:+.2f}\n"
                    f"Consider setting a stop loss immediately."
                ),
                "color": COLOR_RED,
            })
        elif pnl_pct <= -WARN_AGAINST_PCT:
            alerts.append({
                "level": "WARN",
                "symbol": symbol,
                "pnl_pct": pnl_pct,
                "unrealized": unrealized,
                "entry": entry,
                "mark": mark,
                "message": (
                    f"**{symbol}** is down **{abs(pnl_pct):.2f}%** from entry.\n"
                    f"Entry: ${entry:,.4f} | Mark: ${mark:,.4f} | uPnL: ${unrealized:+.2f}\n"
                    f"Monitor closely."
                ),
                "color": COLOR_YELLOW,
            })
        elif pnl_pct >= TAKE_PROFIT_PCT:
            alerts.append({
                "level": "TAKE_PROFIT",
                "symbol": symbol,
                "pnl_pct": pnl_pct,
                "unrealized": unrealized,
                "entry": entry,
                "mark": mark,
                "message": (
                    f"**{symbol}** is up **{pnl_pct:.2f}%** — consider taking profit!\n"
                    f"Entry: ${entry:,.4f} | Mark: ${mark:,.4f} | uPnL: ${unrealized:+.2f}"
                ),
                "color": COLOR_GREEN,
            })

    return alerts


async def run_guardian() -> None:
    """Main Guardian agent: monitor risk and post alerts to #guardian-alerts."""
    logger.info("Guardian agent starting...")

    api_key = os.getenv("BINANCE_API_KEY", "")
    secret = os.getenv("BINANCE_SECRET_KEY", "")
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord = DiscordAlerts(bot_token=bot_token)

    # Fetch data concurrently
    positions, account, control = await asyncio.gather(
        fetch_futures_positions(api_key, secret),
        fetch_futures_account(api_key, secret),
        asyncio.to_thread(load_bot_control, BOT_CONTROL_PATH),
        return_exceptions=True,
    )

    if isinstance(positions, Exception):
        logger.error(f"Guardian: positions fetch error: {positions}")
        positions = []
    if isinstance(account, Exception):
        logger.error(f"Guardian: account fetch error: {account}")
        account = {}
    if isinstance(control, Exception):
        logger.error(f"Guardian: control load error: {control}")
        control = {}

    alert_count = 0

    # ── 1. Binance position risk alerts ──
    if positions:
        position_alerts = _analyze_positions(positions)
        for alert in position_alerts:
            embed = {
                "title": f"Guardian Alert — {alert['level']}: {alert['symbol']}",
                "description": alert["message"],
                "color": alert["color"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "PolyBot Guardian"},
            }
            await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
            alert_count += 1
    else:
        # No active futures positions — send a clean status
        embed = {
            "title": "Guardian Status — No Active Futures Positions",
            "description": "No Binance futures positions to monitor.",
            "color": COLOR_GREEN,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "PolyBot Guardian"},
        }
        await discord._post_channel_message(GUARDIAN_CHANNEL, embed)

    # ── 2. Futures daily loss check (from account totalUnrealizedProfit vs walletBalance) ──
    if account:
        wallet_balance = float(account.get("totalWalletBalance", 0) or 0)
        total_unrealized = float(account.get("totalUnrealizedProfit", 0) or 0)
        if wallet_balance > 0 and total_unrealized < 0:
            loss_pct = abs(total_unrealized) / wallet_balance * 100
            if loss_pct >= DAILY_LOSS_CRITICAL_PCT:
                embed = {
                    "title": "CRITICAL — Futures Daily Loss Exceeded",
                    "description": (
                        f"Unrealized loss is **{loss_pct:.2f}%** of wallet balance.\n"
                        f"Wallet Balance: ${wallet_balance:.2f} | "
                        f"Total uPnL: ${total_unrealized:+.2f}\n"
                        f"Consider closing positions immediately."
                    ),
                    "color": COLOR_RED,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": {"text": "PolyBot Guardian"},
                }
                await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
                alert_count += 1

    # ── 3. Polybot control file checks ──
    if control:
        trading_enabled = control.get("trading_enabled", True)
        halt_reason = control.get("halt_reason")
        mode = control.get("mode", "unknown")

        if not trading_enabled:
            embed = {
                "title": "Guardian Alert — PolyBot Trading HALTED",
                "description": (
                    f"Trading is currently **disabled** in bot_control.json.\n"
                    f"Mode: {mode}\n"
                    f"Halt Reason: {halt_reason or 'Not specified'}"
                ),
                "color": COLOR_YELLOW,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "PolyBot Guardian"},
            }
            await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
            alert_count += 1

    logger.info(f"Guardian check complete. {alert_count} alert(s) sent to #guardian-alerts.")
