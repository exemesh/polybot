"""
Nyx — Supreme Coordinator.
Runs Amara (Intelligence & Trading) and Pia (Analytics & Risk) in parallel.
Sends ONE command briefing per calendar day (first run only, within briefing window).

Named after Nyx — Greek goddess of the night. Even the gods feared her.
She sees everything, commands everything, from the dark.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from utils.discord_alerts import DiscordAlerts

logger = logging.getLogger("polybot.nyx")

# nyx-command channel
NYX_COMMAND_CHANNEL = "1482503179504586904"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
LAST_STARTUP_PATH = os.path.join(DATA_DIR, "nyx_last_startup.json")

COLOR_DARK = 0x1A1A2E  # Deep night purple


# ── Startup deduplication helpers ───────────────────────────────────────────

def _load_last_startup_date() -> str | None:
    """Return the date string (YYYY-MM-DD) of the last startup message, or None."""
    try:
        if os.path.exists(LAST_STARTUP_PATH):
            with open(LAST_STARTUP_PATH) as f:
                return json.load(f).get("last_startup_date")
    except Exception as exc:
        logger.warning(f"Nyx: failed to load last startup date: {exc}")
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
        logger.warning(f"Nyx: failed to save last startup date: {exc}")


# ── Agent runner ─────────────────────────────────────────────────────────────

async def _run_agent(name: str, coro) -> None:
    """Run a single agent coroutine and catch all exceptions."""
    try:
        logger.info(f"Nyx: activating {name}...")
        await coro
        logger.info(f"Nyx: {name} completed.")
    except Exception as exc:
        logger.error(f"Nyx: {name} raised an error: {exc}", exc_info=True)


# ── Startup message ──────────────────────────────────────────────────────────

async def _send_startup_message(discord: DiscordAlerts) -> None:
    """Send a single startup briefing to the nyx-command channel."""
    now = datetime.now(timezone.utc)
    time_str = now.strftime("%H:%M UTC")
    date_str = now.strftime("%d %b %Y")

    content = (
        f"🌑 **Nyx** — {date_str} · {time_str}\n"
        f"▸ **Amara** active — scanning markets, routing signals\n"
        f"▸ **Pia** active — monitoring risk, reporting P&L\n"
        f"Systems nominal. The night begins."
    )
    await discord._post_channel_message(NYX_COMMAND_CHANNEL, content)
    logger.info("Nyx: startup briefing sent.")


# ── Main entry point ─────────────────────────────────────────────────────────

async def run_nyx() -> None:
    """
    Supreme coordinator entry point.

    On first run of each calendar day: sends a startup briefing to #nyx-command.
    On subsequent runs of the same day: skips silently.
    Runs Amara and Pia concurrently. Neither agent failure can bring down Nyx.
    """
    logger.info("Nyx: rising...")

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord = DiscordAlerts(bot_token=bot_token)

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    # Send startup once per day, only during morning (07–09 UTC) or evening (16–18 UTC) windows
    current_hour = now.hour
    in_briefing_window = (7 <= current_hour < 9) or (16 <= current_hour < 18)
    last_startup_date = _load_last_startup_date()

    if last_startup_date == today_str:
        logger.info(f"Nyx: briefing already sent today ({today_str}) — standing by.")
    elif not in_briefing_window:
        logger.info(f"Nyx: outside briefing window (hour={current_hour} UTC) — silent run.")
    else:
        try:
            await _send_startup_message(discord)
            _save_last_startup_date(today_str)
        except Exception as exc:
            logger.warning(f"Nyx: startup briefing failed: {exc}")

    # Lazy imports to avoid circular imports at module load time
    from agents.amara import run_amara
    from agents.pia import run_pia

    # Activate both agents in parallel — Nyx never crashes from agent failures
    await asyncio.gather(
        _run_agent("Amara", run_amara()),
        _run_agent("Pia", run_pia()),
        return_exceptions=True,
    )

    logger.info("Nyx: all agents complete. Returning to the dark.")
