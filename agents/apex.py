"""
Apex Coordinator — Runs Recon, Blaze, Sage, and Sentinel agents in parallel.
Sends ONE startup message per calendar day (first run only).
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from utils.discord_alerts import DiscordAlerts

logger = logging.getLogger("polybot.apex")

# apex-command channel for startup message
APEX_COMMAND_CHANNEL = "1482503179504586904"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
LAST_STARTUP_PATH = os.path.join(DATA_DIR, "apex_last_startup.json")

COLOR_BLUE = 0x007BFF


# ── Startup deduplication helpers ───────────────────────────────────────────

def _load_last_startup_date() -> str | None:
    """Return the date string (YYYY-MM-DD) of the last startup message, or None."""
    try:
        if os.path.exists(LAST_STARTUP_PATH):
            with open(LAST_STARTUP_PATH) as f:
                return json.load(f).get("last_startup_date")
    except Exception as exc:
        logger.warning(f"Failed to load last startup date: {exc}")
    return None


def _save_last_startup_date(date_str: str) -> None:
    """Persist today's date as the last startup date."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LAST_STARTUP_PATH, "w") as f:
            json.dump(
                {
                    "last_startup_date": date_str,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                f,
            )
    except Exception as exc:
        logger.warning(f"Failed to save last startup date: {exc}")


# ── Agent runner ─────────────────────────────────────────────────────────────

async def _run_agent(name: str, coro) -> None:
    """Run a single agent coroutine and catch all exceptions."""
    try:
        logger.info(f"Apex: starting {name}...")
        await coro
        logger.info(f"Apex: {name} completed successfully.")
    except Exception as exc:
        logger.error(f"Apex: {name} raised an error: {exc}", exc_info=True)


# ── Startup message ──────────────────────────────────────────────────────────

async def _send_startup_message(discord: DiscordAlerts) -> None:
    """Send a single startup message to the apex-command channel."""
    content = "🔱 Apex online. Swarm active — Recon, Sage and Sentinel running."
    await discord._post_channel_message(APEX_COMMAND_CHANNEL, content)
    logger.info("Apex: startup message sent to #apex-command.")


# ── Main entry point ─────────────────────────────────────────────────────────

async def run_apex() -> None:
    """
    Main coordinator entry point.

    On first run of each calendar day: sends a startup message to #apex-command.
    On subsequent runs of the same day: skips the startup message silently.
    Then runs Recon, Sage, and Sentinel concurrently via asyncio.gather().
    Each agent is wrapped so its failure cannot crash the other agents.
    """
    logger.info("Apex coordinator starting...")

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord = DiscordAlerts(bot_token=bot_token)

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    # Only send startup message once per calendar day AND only during morning/evening windows
    # Morning: 07:00-09:00 UTC (07:00-09:00 WAT) | Evening: 16:00-18:00 UTC (16:00-18:00 WAT)
    current_hour = now.hour
    in_briefing_window = (7 <= current_hour < 9) or (16 <= current_hour < 18)
    last_startup_date = _load_last_startup_date()
    if last_startup_date == today_str:
        logger.info(f"Apex: startup message already sent today ({today_str}) — skipping.")
    elif not in_briefing_window:
        logger.info(f"Apex: startup message suppressed (hour={current_hour} UTC, not in briefing window).")
    else:
        try:
            await _send_startup_message(discord)
            _save_last_startup_date(today_str)
        except Exception as exc:
            logger.warning(f"Apex: startup message failed: {exc}")

    # Lazy imports to avoid circular imports at module load time
    from agents.scout import run_scout
    from agents.analyst import run_analyst
    from agents.guardian import run_guardian

    # Run all three agents in parallel
    await asyncio.gather(
        _run_agent("Recon", run_scout()),
        _run_agent("Sage", run_analyst()),
        _run_agent("Sentinel", run_guardian()),
        return_exceptions=True,  # Never propagate — Apex itself should not crash
    )

    logger.info("Apex coordinator: all agents finished.")
