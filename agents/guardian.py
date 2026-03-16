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

XRP_PRICE_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "xrp_price_cache.json"
)

# Baseline values for grid bots
FUTURES_GRID_BASELINE_PNL = 231.47

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


def _load_xrp_price_cache() -> dict:
    """Load the XRP price cache from disk."""
    try:
        if os.path.exists(XRP_PRICE_CACHE_PATH):
            with open(XRP_PRICE_CACHE_PATH) as f:
                return json.load(f)
    except Exception as exc:
        logger.warning(f"XRP price cache read failed: {exc}")
    return {}


def _save_xrp_price_cache(data: dict) -> None:
    """Save the XRP price cache to disk."""
    try:
        os.makedirs(os.path.dirname(XRP_PRICE_CACHE_PATH), exist_ok=True)
        with open(XRP_PRICE_CACHE_PATH, "w") as f:
            json.dump(data, f)
    except Exception as exc:
        logger.warning(f"XRP price cache write failed: {exc}")


async def monitor_binance_bots(bot_data: dict, discord: "DiscordAlerts") -> int:
    """Check grid bot thresholds and send alerts to #guardian-alerts.

    READ ONLY — no orders, no trades, no API writes. Only alerts.
    Returns the number of alerts sent.
    """
    alert_count = 0
    now = datetime.now(timezone.utc).isoformat()

    futures = bot_data.get("futures_grid", {})
    spot = bot_data.get("spot_grid", {})
    xrp_price = bot_data.get("xrp_price", 0.0)

    futures_pnl = futures.get("unrealized_pnl", FUTURES_GRID_BASELINE_PNL)
    spot_total_profit = spot.get("total_profit", 0.0)
    spot_status = spot.get("status", "UNKNOWN")

    # ── Futures Grid Alerts ──
    if futures_pnl < 150:
        embed = {
            "title": "🚨 Futures Grid P&L CRITICAL",
            "description": (
                f"**P&L: ${futures_pnl:,.2f}** — Down significantly from "
                f"${FUTURES_GRID_BASELINE_PNL:.2f} baseline.\n"
                f"Consider reviewing position."
            ),
            "color": COLOR_RED,
            "timestamp": now,
            "footer": {"text": "PolyBot Guardian — READ ONLY"},
        }
        await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
        alert_count += 1
        logger.warning(f"CRITICAL: Futures Grid P&L at ${futures_pnl:.2f}")
    elif futures_pnl < 200:
        embed = {
            "title": "⚠️ Futures Grid P&L Warning",
            "description": (
                f"**P&L: ${futures_pnl:,.2f}** — Approaching risk threshold.\n"
                f"Baseline was ${FUTURES_GRID_BASELINE_PNL:.2f}."
            ),
            "color": COLOR_YELLOW,
            "timestamp": now,
            "footer": {"text": "PolyBot Guardian — READ ONLY"},
        }
        await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
        alert_count += 1
        logger.warning(f"WARNING: Futures Grid P&L at ${futures_pnl:.2f}")
    elif futures_pnl >= 400:
        embed = {
            "title": "🚀 Futures Grid P&L — Exceptional!",
            "description": (
                f"**P&L: ${futures_pnl:,.2f}** — Exceptional performance!\n"
                f"Up ${futures_pnl - FUTURES_GRID_BASELINE_PNL:.2f} from baseline."
            ),
            "color": COLOR_GREEN,
            "timestamp": now,
            "footer": {"text": "PolyBot Guardian — READ ONLY"},
        }
        await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
        alert_count += 1
        logger.info(f"Futures Grid P&L at exceptional ${futures_pnl:.2f}")
    elif futures_pnl >= 300:
        embed = {
            "title": "🎯 Futures Grid Hitting $300+ P&L!",
            "description": (
                f"**P&L: ${futures_pnl:,.2f}** — Consider taking some profit.\n"
                f"Up ${futures_pnl - FUTURES_GRID_BASELINE_PNL:.2f} from baseline."
            ),
            "color": COLOR_GREEN,
            "timestamp": now,
            "footer": {"text": "PolyBot Guardian — READ ONLY"},
        }
        await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
        alert_count += 1
        logger.info(f"Futures Grid P&L milestone at ${futures_pnl:.2f}")

    # ── Spot Grid Alerts ──
    if spot_total_profit < 0:
        embed = {
            "title": "🚨 Spot Grid Now in Loss",
            "description": f"**Total profit: ${spot_total_profit:,.2f}** — Grid bot is in a loss position.",
            "color": COLOR_RED,
            "timestamp": now,
            "footer": {"text": "PolyBot Guardian — READ ONLY"},
        }
        await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
        alert_count += 1
        logger.warning(f"Spot Grid in loss: ${spot_total_profit:.2f}")

    if spot_status == "STOPPED":
        embed = {
            "title": "🚨 Spot Grid Bot Has STOPPED",
            "description": "The XRP/USDT Spot Grid Bot has **STOPPED** running.",
            "color": COLOR_RED,
            "timestamp": now,
            "footer": {"text": "PolyBot Guardian — READ ONLY"},
        }
        await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
        alert_count += 1
        logger.warning("Spot Grid bot has stopped.")

    if spot_total_profit > 5.00:
        embed = {
            "title": "✅ Spot Grid Profit Milestone",
            "description": f"**Total profit: ${spot_total_profit:,.2f}** — Milestone reached!",
            "color": COLOR_GREEN,
            "timestamp": now,
            "footer": {"text": "PolyBot Guardian — READ ONLY"},
        }
        await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
        alert_count += 1
        logger.info(f"Spot Grid profit milestone: ${spot_total_profit:.2f}")

    # ── XRP Price Movement Alert ──
    if xrp_price and xrp_price > 0:
        cache = _load_xrp_price_cache()
        last_price = cache.get("price", 0.0)

        if last_price and last_price > 0:
            price_change_pct = abs(xrp_price - last_price) / last_price * 100
            if price_change_pct >= 5.0:
                direction = "+" if xrp_price >= last_price else "-"
                embed = {
                    "title": "📊 XRP Price Movement Alert",
                    "description": (
                        f"XRP moved **{direction}{price_change_pct:.1f}%** in last cycle.\n"
                        f"Previous: **${last_price:.4f}** → Current: **${xrp_price:.4f}**"
                    ),
                    "color": COLOR_YELLOW,
                    "timestamp": now,
                    "footer": {"text": "PolyBot Guardian — READ ONLY"},
                }
                await discord._post_channel_message(GUARDIAN_CHANNEL, embed)
                alert_count += 1
                logger.info(f"XRP price moved {direction}{price_change_pct:.1f}%: ${last_price:.4f} → ${xrp_price:.4f}")

        # Always update the cache with latest price
        _save_xrp_price_cache({"price": xrp_price, "timestamp": now})

    return alert_count


async def run_guardian() -> None:
    """Main Guardian agent: monitor risk and post alerts to #guardian-alerts."""
    logger.info("Guardian agent starting...")

    api_key = os.getenv("BINANCE_API_KEY", "")
    secret = os.getenv("BINANCE_SECRET_KEY", "")
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord = DiscordAlerts(bot_token=bot_token)

    # Import here to avoid circular imports
    from agents.scout import fetch_binance_bots

    # Fetch data concurrently
    positions, account, control, bot_data = await asyncio.gather(
        fetch_futures_positions(api_key, secret),
        fetch_futures_account(api_key, secret),
        asyncio.to_thread(load_bot_control, BOT_CONTROL_PATH),
        fetch_binance_bots(api_key, secret),
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
    if isinstance(bot_data, Exception):
        logger.error(f"Guardian: bot data fetch error: {bot_data}")
        bot_data = None

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

    # ── 4. Binance grid bot monitoring (READ ONLY — alerts only) ──
    if bot_data:
        bot_alerts = await monitor_binance_bots(bot_data, discord)
        alert_count += bot_alerts
    else:
        logger.warning("Guardian: no bot data available for grid bot monitoring.")

    logger.info(f"Guardian check complete. {alert_count} alert(s) sent to #guardian-alerts.")
