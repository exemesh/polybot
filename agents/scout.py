"""
Scout Agent — Binance positions + crypto news + Polymarket opportunities → #scout-intel
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

logger = logging.getLogger("polybot.scout")

# Discord channel IDs
SCOUT_INTEL_CHANNEL = "1483029658072121355"

# Binance base URLs
BINANCE_SPOT_URL = "https://api.binance.com"
BINANCE_FUTURES_URL = "https://fapi.binance.com"

# CryptoPanic free news endpoint
CRYPTOPANIC_URL = (
    "https://cryptopanic.com/api/v1/posts/"
    "?auth_token=anonymous&kind=news&currencies=BTC,ETH"
)

# Polymarket Gamma API
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"


def _binance_sign(params: dict, secret: str) -> str:
    """Return URL-encoded query string with HMAC-SHA256 signature appended."""
    query = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig


async def _binance_get(client: httpx.AsyncClient, base: str, path: str, params: dict,
                       api_key: str, secret: str) -> dict | list:
    params["timestamp"] = int(time.time() * 1000)
    qs = _binance_sign(params, secret)
    url = f"{base}{path}?{qs}"
    headers = {"X-MBX-APIKEY": api_key}
    try:
        resp = await client.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning(f"Binance request {path} failed: {exc}")
        return {}


async def fetch_binance_positions(api_key: str, secret: str) -> dict:
    """Fetch spot balances and futures positions from Binance."""
    result = {"spot_balances": [], "futures_positions": [], "spot_orders": []}
    if not api_key or not secret:
        logger.warning("Binance API credentials not configured.")
        return result

    async with httpx.AsyncClient(timeout=15) as client:
        # Spot open orders
        orders = await _binance_get(
            client, BINANCE_SPOT_URL, "/api/v3/openOrders", {}, api_key, secret
        )
        if isinstance(orders, list):
            result["spot_orders"] = orders

        # Spot account balances
        account = await _binance_get(
            client, BINANCE_SPOT_URL, "/api/v3/account", {}, api_key, secret
        )
        if isinstance(account, dict) and "balances" in account:
            result["spot_balances"] = [
                b for b in account["balances"]
                if float(b.get("free", 0)) > 0 or float(b.get("locked", 0)) > 0
            ]

        # Futures positions
        futures = await _binance_get(
            client, BINANCE_FUTURES_URL, "/fapi/v2/positionRisk", {}, api_key, secret
        )
        if isinstance(futures, list):
            result["futures_positions"] = [
                p for p in futures if float(p.get("positionAmt", 0)) != 0
            ]

    return result


async def fetch_binance_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch current spot prices for a list of symbols (e.g. ['BTCUSDT'])."""
    prices = {}
    if not symbols:
        return prices
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{BINANCE_SPOT_URL}/api/v3/ticker/price",
                timeout=10,
            )
            if resp.status_code == 200:
                for item in resp.json():
                    prices[item["symbol"]] = float(item["price"])
    except Exception as exc:
        logger.warning(f"Failed to fetch Binance prices: {exc}")
    return prices


async def fetch_crypto_news() -> list[dict]:
    """Fetch top crypto news headlines from CryptoPanic (no auth needed)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(CRYPTOPANIC_URL)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("results", [])[:10]
    except Exception as exc:
        logger.warning(f"CryptoPanic fetch failed: {exc}")
    return []


async def fetch_polymarket_opportunities() -> list[dict]:
    """Scan Polymarket Gamma API for high-edge open markets."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{POLYMARKET_GAMMA_URL}/markets",
                params={
                    "closed": "false",
                    "limit": 50,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            if resp.status_code == 200:
                markets = resp.json()
                opportunities = []
                for m in markets:
                    # Look for markets with prices near extremes (potential edge)
                    best_ask = float(m.get("bestAsk", 0.5) or 0.5)
                    best_bid = float(m.get("bestBid", 0.5) or 0.5)
                    spread = round(best_ask - best_bid, 4)
                    vol = float(m.get("volume24hr", 0) or 0)
                    if vol > 100:  # Only liquid markets
                        opportunities.append({
                            "question": m.get("question", "")[:120],
                            "best_bid": best_bid,
                            "best_ask": best_ask,
                            "spread": spread,
                            "volume_24h": vol,
                        })
                # Sort by volume descending, return top 3
                opportunities.sort(key=lambda x: x["volume_24h"], reverse=True)
                return opportunities[:3]
    except Exception as exc:
        logger.warning(f"Polymarket Gamma fetch failed: {exc}")
    return []


def _build_position_fields(positions: dict, prices: dict) -> list[dict]:
    """Build Discord embed fields for Binance positions."""
    fields = []

    # Futures positions
    for p in positions.get("futures_positions", []):
        symbol = p.get("symbol", "")
        entry = float(p.get("entryPrice", 0))
        current = prices.get(symbol, float(p.get("markPrice", 0)))
        amt = float(p.get("positionAmt", 0))
        unrealized_pnl = float(p.get("unRealizedProfit", 0))
        pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0.0
        side = "LONG" if amt > 0 else "SHORT"
        fields.append({
            "name": f"Futures {side}: {symbol}",
            "value": (
                f"Entry: ${entry:,.4f} | Now: ${current:,.4f}\n"
                f"Amt: {amt} | uPnL: ${unrealized_pnl:+.2f} ({pnl_pct:+.2f}%)"
            ),
            "inline": False,
        })

    # Spot balances (non-trivial amounts only)
    spot_summary = []
    for b in positions.get("spot_balances", []):
        asset = b.get("asset", "")
        free = float(b.get("free", 0))
        locked = float(b.get("locked", 0))
        total = free + locked
        # Estimate USD value if possible
        symbol_usdt = f"{asset}USDT"
        price = prices.get(symbol_usdt, 0)
        usd_val = total * price if price else 0
        spot_summary.append(f"{asset}: {total:.6f}" + (f" (≈${usd_val:.2f})" if usd_val else ""))

    if spot_summary:
        fields.append({
            "name": "Spot Balances",
            "value": "\n".join(spot_summary) or "None",
            "inline": False,
        })
    elif not positions.get("futures_positions"):
        fields.append({
            "name": "Binance Positions",
            "value": "No active positions or balances found.",
            "inline": False,
        })

    return fields


async def run_scout() -> None:
    """Main Scout agent: gather intel and post to #scout-intel channel."""
    logger.info("Scout agent starting...")

    api_key = os.getenv("BINANCE_API_KEY", "")
    secret = os.getenv("BINANCE_SECRET_KEY", "")
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord = DiscordAlerts(bot_token=bot_token)

    # Gather all data concurrently
    binance_data, news, opps = await asyncio.gather(
        fetch_binance_positions(api_key, secret),
        fetch_crypto_news(),
        fetch_polymarket_opportunities(),
        return_exceptions=True,
    )

    # Handle exceptions from gather
    if isinstance(binance_data, Exception):
        logger.error(f"Binance fetch failed: {binance_data}")
        binance_data = {"spot_balances": [], "futures_positions": [], "spot_orders": []}
    if isinstance(news, Exception):
        logger.error(f"News fetch failed: {news}")
        news = []
    if isinstance(opps, Exception):
        logger.error(f"Polymarket fetch failed: {opps}")
        opps = []

    # Fetch current prices for any futures symbols
    futures_symbols = [p.get("symbol", "") for p in binance_data.get("futures_positions", [])]
    spot_symbols = [f"{b.get('asset', '')}USDT" for b in binance_data.get("spot_balances", [])]
    prices = await fetch_binance_prices(list(set(futures_symbols + spot_symbols)))

    # Build embed fields
    fields = _build_position_fields(binance_data, prices)

    # Top 3 news headlines
    news_lines = []
    for article in news[:3]:
        title = article.get("title", "")[:100]
        source = article.get("source", {}).get("title", "Unknown")
        news_lines.append(f"• **{title}** — {source}")
    fields.append({
        "name": "Top Crypto News",
        "value": "\n".join(news_lines) if news_lines else "No news fetched.",
        "inline": False,
    })

    # Top 3 Polymarket opportunities
    opp_lines = []
    for opp in opps[:3]:
        q = opp.get("question", "")[:80]
        bid = opp.get("best_bid", 0)
        ask = opp.get("best_ask", 0)
        vol = opp.get("volume_24h", 0)
        opp_lines.append(f"• {q}\n  Bid: {bid:.3f} | Ask: {ask:.3f} | 24h Vol: ${vol:,.0f}")
    fields.append({
        "name": "Polymarket Opportunities",
        "value": "\n".join(opp_lines) if opp_lines else "No opportunities found.",
        "inline": False,
    })

    # Compose and send the embed
    open_orders = len(binance_data.get("spot_orders", []))
    futures_count = len(binance_data.get("futures_positions", []))
    spot_count = len(binance_data.get("spot_balances", []))

    embed = {
        "title": "Scout Intel Report",
        "description": (
            f"Binance scan complete — {futures_count} futures position(s), "
            f"{spot_count} spot balance(s), {open_orders} open order(s)"
        ),
        "color": 0x007BFF,  # Blue
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "PolyBot Scout Agent"},
    }

    await discord._post_channel_message(SCOUT_INTEL_CHANNEL, embed)
    logger.info("Scout report posted to #scout-intel.")
