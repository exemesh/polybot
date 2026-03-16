"""
Apex Coordinator — Runs Scout, Analyst, and Guardian agents in parallel.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from utils.discord_alerts import DiscordAlerts

logger = logging.getLogger("polybot.apex")

# All channel IDs for the startup broadcast
CHANNELS = {
    "scout-intel": "1483029658072121355",
    "trader-bot": "1483029674396487762",
    "analyst-dashboard": "1483029691689341110",
    "guardian-alerts": "1483029707329896471",
}

COLOR_BLUE = 0x007BFF


async def _run_agent(name: str, coro) -> None:
    """Run a single agent coroutine and catch all exceptions."""
    try:
        logger.info(f"Apex: starting {name}...")
        await coro
        logger.info(f"Apex: {name} completed successfully.")
    except Exception as exc:
        logger.error(f"Apex: {name} raised an error: {exc}", exc_info=True)


async def _send_startup_message(discord: DiscordAlerts) -> None:
    """Broadcast a startup notice to all channels."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tasks = []
    for channel_name, channel_id in CHANNELS.items():
        embed = {
            "title": "Apex Coordinator Online",
            "description": (
                f"Agent swarm starting at {ts}.\n"
                f"Scout, Analyst, and Guardian are launching in parallel."
            ),
            "color": COLOR_BLUE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"PolyBot Apex — #{channel_name}"},
        }
        tasks.append(discord._post_channel_message(channel_id, embed))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for channel_name, result in zip(CHANNELS.keys(), results):
        if isinstance(result, Exception):
            logger.warning(f"Apex: startup message to #{channel_name} failed: {result}")


async def run_apex() -> None:
    """
    Main coordinator entry point.

    Sends a startup message to all four channels, then runs Scout, Analyst,
    and Guardian concurrently via asyncio.gather(). Each agent is wrapped so
    its failure cannot crash the other agents.
    """
    logger.info("Apex coordinator starting...")

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord = DiscordAlerts(bot_token=bot_token)

    # Lazy imports to avoid circular imports at module load time
    from agents.scout import run_scout
    from agents.analyst import run_analyst
    from agents.guardian import run_guardian

    # Send startup broadcast first (best-effort)
    try:
        await _send_startup_message(discord)
    except Exception as exc:
        logger.warning(f"Apex: startup broadcast failed: {exc}")

    # Run all three agents in parallel
    await asyncio.gather(
        _run_agent("Scout", run_scout()),
        _run_agent("Analyst", run_analyst()),
        _run_agent("Guardian", run_guardian()),
        return_exceptions=True,  # Never propagate — Apex itself should not crash
    )

    logger.info("Apex coordinator: all agents finished.")
