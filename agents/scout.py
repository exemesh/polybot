"""
Recon Agent — Polymarket opportunities → #scout-intel
Only posts when new opportunities are found vs last run.
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

# Path to store last known opportunities for deduplication
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
LAST_OPPS_PATH = os.path.join(DATA_DIR, "scout_last_opportunities.json")


def _load_last_opportunities() -> list[str]:
    """Load question texts from the last posted opportunities."""
    try:
        if os.path.exists(LAST_OPPS_PATH):
            with open(LAST_OPPS_PATH) as f:
                data = json.load(f)
                return data.get("questions", [])
    except Exception as exc:
        logger.warning(f"Failed to load last opportunities: {exc}")
    return []


def _save_last_opportunities(questions: list[str]) -> None:
    """Persist the question texts of the current opportunities."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LAST_OPPS_PATH, "w") as f:
            json.dump({"questions": questions, "updated_at": datetime.now(timezone.utc).isoformat()}, f)
    except Exception as exc:
        logger.warning(f"Failed to save last opportunities: {exc}")


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


async def run_scout() -> None:
    """Main Recon agent: scan Polymarket and post only when new opportunities found."""
    logger.info("Recon agent starting...")

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord = DiscordAlerts(bot_token=bot_token)

    opps = await fetch_polymarket_opportunities()

    if isinstance(opps, Exception):
        logger.error(f"Polymarket fetch failed: {opps}")
        opps = []

    current_questions = [o.get("question", "") for o in opps]
    last_questions = _load_last_opportunities()

    # Determine which opportunities are new (not seen in last run)
    new_opps = [o for o in opps if o.get("question", "") not in last_questions]

    if not new_opps:
        logger.info("Recon: no new Polymarket opportunities vs last run — skipping Discord post.")
        return

    # Build embed fields for new opportunities
    opp_lines = []
    for opp in new_opps:
        q = opp.get("question", "")[:80]
        bid = opp.get("best_bid", 0)
        ask = opp.get("best_ask", 0)
        vol = opp.get("volume_24h", 0)
        opp_lines.append(f"• {q}\n  Bid: {bid:.3f} | Ask: {ask:.3f} | 24h Vol: ${vol:,.0f}")

    embed = {
        "title": "Recon Intel Report",
        "description": f"New Polymarket opportunities detected ({len(new_opps)} new vs last scan)",
        "color": 0x007BFF,
        "fields": [
            {
                "name": "New Polymarket Opportunities",
                "value": "\n".join(opp_lines),
                "inline": False,
            }
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "PolyBot Recon Agent"},
    }

    await discord._post_channel_message(SCOUT_INTEL_CHANNEL, embed)
    logger.info(f"Recon report posted to #scout-intel ({len(new_opps)} new opportunities).")

    # Persist current opportunity questions for next run comparison
    _save_last_opportunities(current_questions)
