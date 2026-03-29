"""
Recon Agent — Polymarket opportunities → #scout-intel
Buffers opportunities throughout the day and posts digest at 9AM, 12PM and 6PM UTC.
Urgent opportunities (edge > 5% or volume > $1M) are posted immediately.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import httpx

from utils.discord_alerts import DiscordAlerts

logger = logging.getLogger("polybot.scout")

# Discord channel IDs
SCOUT_INTEL_CHANNEL = "1483029658072121355"

# Polymarket Gamma API
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Buffer: accumulates opportunities between digest posts
BUFFER_PATH = os.path.join(DATA_DIR, "recon_opportunity_buffer.json")

# Tracks when each digest slot was last posted to avoid double posting
LAST_POSTS_PATH = os.path.join(DATA_DIR, "recon_last_posts.json")

# Digest post hours (UTC)
DIGEST_HOURS = {9, 12, 18}

# Thresholds for immediate urgent posting
URGENT_EDGE_PCT = 5.0   # edge > 5%
URGENT_VOLUME = 1_000_000.0  # volume > $1,000,000


# ── Buffer helpers ──────────────────────────────────────────────────────────

def _load_buffer() -> list[dict]:
    """Load the current opportunity buffer."""
    try:
        if os.path.exists(BUFFER_PATH):
            with open(BUFFER_PATH) as f:
                data = json.load(f)
                return data.get("opportunities", [])
    except Exception as exc:
        logger.warning(f"Failed to load opportunity buffer: {exc}")
    return []


def _save_buffer(opportunities: list[dict]) -> None:
    """Persist the opportunity buffer."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(BUFFER_PATH, "w") as f:
            json.dump(
                {
                    "opportunities": opportunities,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                f,
            )
    except Exception as exc:
        logger.warning(f"Failed to save opportunity buffer: {exc}")


def _clear_buffer() -> None:
    """Clear the opportunity buffer after a digest post."""
    _save_buffer([])


# ── Last-posts helpers ──────────────────────────────────────────────────────

def _load_last_posts() -> dict:
    """Load last digest post timestamps keyed by slot name."""
    try:
        if os.path.exists(LAST_POSTS_PATH):
            with open(LAST_POSTS_PATH) as f:
                return json.load(f)
    except Exception as exc:
        logger.warning(f"Failed to load last posts: {exc}")
    return {}


def _save_last_posts(data: dict) -> None:
    """Persist last digest post timestamps."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LAST_POSTS_PATH, "w") as f:
            json.dump(data, f)
    except Exception as exc:
        logger.warning(f"Failed to save last posts: {exc}")


def _digest_slot_key(hour: int) -> str:
    """Return a unique key for a digest slot, e.g. '2026-03-16_9am'."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{today}_{hour}h"


def _already_posted_slot(last_posts: dict, hour: int) -> bool:
    key = _digest_slot_key(hour)
    return last_posts.get(key) is not None


def _mark_slot_posted(last_posts: dict, hour: int) -> None:
    key = _digest_slot_key(hour)
    last_posts[key] = datetime.now(timezone.utc).isoformat()


# ── Polymarket fetch ────────────────────────────────────────────────────────

async def fetch_polymarket_opportunities() -> list[dict]:
    """Scan Polymarket Gamma API for mispriced / high-value open markets.

    Edge metric: measures how far the mid-price is from 50¢ relative to the
    spread. A market at 20¢ bid / 22¢ ask has a real mid of 21¢ — if the
    true probability is 25¢, that's a 4¢ edge. We proxy this by combining:
      1. Price momentum: |lastTradePrice - bestMid| as signal of recent move
      2. Spread efficiency: wide spread on a liquid market = inefficiency
      3. Volume signal: 24h volume confirms genuine interest
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{POLYMARKET_GAMMA_URL}/markets",
                params={
                    "closed": "false",
                    "limit": 100,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            if resp.status_code == 200:
                markets = resp.json()
                opportunities = []
                for m in markets:
                    best_ask = float(m.get("bestAsk") or 0)
                    best_bid = float(m.get("bestBid") or 0)
                    last_price = float(m.get("lastTradePrice") or m.get("price") or 0)
                    vol_24h = float(m.get("volume24hr") or 0)
                    vol_total = float(m.get("volume") or 0)

                    if best_ask <= 0 or best_bid <= 0 or vol_24h < 500:
                        continue  # skip illiquid / broken markets

                    mid = (best_ask + best_bid) / 2
                    spread = best_ask - best_bid

                    # Edge signal 1: spread relative to mid (wider = more inefficient)
                    spread_inefficiency = (spread / mid * 100) if mid > 0 else 0

                    # Edge signal 2: recent price momentum vs current mid
                    # If last trade moved away from mid significantly → momentum opportunity
                    momentum_gap = abs(last_price - mid) * 100 if last_price > 0 else 0

                    # Edge signal 3: market in sweet spot (20¢-80¢) = real uncertainty
                    in_uncertainty_zone = 0.20 <= mid <= 0.80

                    # Combined edge score — weighted
                    edge_score = round(
                        (spread_inefficiency * 0.5) + (momentum_gap * 0.3) + (5.0 if in_uncertainty_zone else 0),
                        2
                    )

                    opportunities.append({
                        "question": m.get("question", "")[:120],
                        "best_bid": round(best_bid, 4),
                        "best_ask": round(best_ask, 4),
                        "mid_price": round(mid, 4),
                        "spread": round(spread, 4),
                        "last_price": round(last_price, 4),
                        "edge_pct": edge_score,
                        "volume_24h": vol_24h,
                        "volume_total": vol_total,
                        "in_uncertainty_zone": in_uncertainty_zone,
                    })

                # Sort by edge score descending, return top 10
                opportunities.sort(key=lambda x: x["edge_pct"], reverse=True)
                return opportunities[:10]
    except Exception as exc:
        logger.warning(f"Polymarket Gamma fetch failed: {exc}")
    return []


# ── Discord posting helpers ─────────────────────────────────────────────────

def _build_opp_line(opp: dict) -> str:
    q = opp.get("question", "")[:80]
    bid = opp.get("best_bid", 0)
    ask = opp.get("best_ask", 0)
    mid = opp.get("mid_price", (bid + ask) / 2 if bid and ask else 0)
    vol = opp.get("volume_24h", 0)
    edge = opp.get("edge_pct", 0)
    zone = "🎯" if opp.get("in_uncertainty_zone") else "⚡"
    return f"{zone} {q}\n  Mid: {mid:.3f} (Bid {bid:.3f} / Ask {ask:.3f}) | Score: {edge:.1f} | 24h: ${vol:,.0f}"


async def _post_urgent(discord: DiscordAlerts, opp: dict) -> None:
    """Post a single urgent opportunity immediately."""
    line = _build_opp_line(opp)
    embed = {
        "title": "⚠️ URGENT — Recon Opportunity",
        "description": "High-priority opportunity detected (edge > 5% or volume > $1M)",
        "color": 0xFF4500,
        "fields": [
            {
                "name": "Opportunity",
                "value": line,
                "inline": False,
            }
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "PolyBot Recon Agent — URGENT"},
    }
    await discord._post_channel_message(SCOUT_INTEL_CHANNEL, embed)
    logger.info(f"Recon URGENT post sent: {opp.get('question', '')[:60]}")


async def _post_digest(discord: DiscordAlerts, buffered: list[dict], hour: int) -> None:
    """Post a scheduled digest of buffered opportunities."""
    if not buffered:
        logger.info(f"Recon: digest slot {hour}h — buffer empty, nothing to post.")
        return

    opp_lines = [_build_opp_line(o) for o in buffered[:5]]  # Cap at 5 items per digest
    slot_label = {9: "9AM", 12: "12PM", 18: "6PM"}.get(hour, f"{hour}h")

    embed = {
        "title": f"Recon Intel Digest — {slot_label} UTC",
        "description": f"{len(buffered)} opportunit{'y' if len(buffered) == 1 else 'ies'} accumulated since last digest",
        "color": 0x007BFF,
        "fields": [
            {
                "name": "Buffered Opportunities",
                "value": "\n".join(opp_lines),
                "inline": False,
            }
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "PolyBot Recon Agent"},
    }
    await discord._post_channel_message(SCOUT_INTEL_CHANNEL, embed)
    logger.info(f"Recon digest posted at {slot_label} UTC ({len(buffered)} opportunities).")


# ── Main entry point ────────────────────────────────────────────────────────

async def run_scout() -> None:
    """
    Main Recon agent.

    Each run:
      1. Fetch current Polymarket opportunities.
      2. Add any new ones (by question text) to the buffer.
      3. If any new opportunity has edge > 5% or volume > $1M, post IMMEDIATELY as URGENT.
      4. If the current UTC hour is 9, 12, or 18 and this slot hasn't been posted today,
         post the full buffered digest and clear the buffer.
    """
    logger.info("Recon agent starting...")

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord = DiscordAlerts(bot_token=bot_token)

    now = datetime.now(timezone.utc)
    current_hour = now.hour

    opps = await fetch_polymarket_opportunities()
    if isinstance(opps, Exception):
        logger.error(f"Polymarket fetch failed: {opps}")
        opps = []

    # Load existing buffer and determine which opportunities are new
    buffer = _load_buffer()
    buffered_questions = {o.get("question", "") for o in buffer}
    new_opps = [o for o in opps if o.get("question", "") not in buffered_questions]

    # Add new opportunities to buffer
    buffer.extend(new_opps)
    _save_buffer(buffer)
    logger.info(f"Recon: {len(new_opps)} new opportunities added to buffer (buffer size: {len(buffer)}).")

    # ── Urgent immediate posts ──────────────────────────────────────────────
    for opp in new_opps:
        edge = opp.get("edge_pct", 0)
        vol = opp.get("volume_24h", 0)
        if edge > URGENT_EDGE_PCT or vol > URGENT_VOLUME:
            try:
                await _post_urgent(discord, opp)
            except Exception as exc:
                logger.error(f"Recon: failed to post urgent opportunity: {exc}")

    # ── Scheduled digest post ───────────────────────────────────────────────
    if current_hour in DIGEST_HOURS:
        last_posts = _load_last_posts()
        if not _already_posted_slot(last_posts, current_hour):
            try:
                await _post_digest(discord, buffer, current_hour)
                _mark_slot_posted(last_posts, current_hour)
                _save_last_posts(last_posts)
                _clear_buffer()
            except Exception as exc:
                logger.error(f"Recon: failed to post digest at {current_hour}h: {exc}")
        else:
            logger.info(f"Recon: digest slot {current_hour}h already posted today — skipping.")
    else:
        logger.info(f"Recon: current UTC hour {current_hour} is not a digest slot — buffer updated only.")
