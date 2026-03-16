"""
Discord alert module for PolyBot.
Sends trade notifications and portfolio reports via Discord webhooks.

Requires DISCORD_WEBHOOK_URL environment variable to be set.
If the variable is not set, all functions are silent no-ops.

Uses Discord embed format with colors:
  Green  (0x00C851) — profit / positive status
  Red    (0xFF4444) — loss / error
  Blue   (0x007BFF) — informational
  Yellow (0xFFBB33) — neutral / warning
"""

import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("polybot.discord")

# Discord embed color constants
COLOR_GREEN = 0x00C851
COLOR_RED = 0xFF4444
COLOR_BLUE = 0x007BFF
COLOR_YELLOW = 0xFFBB33


def _get_webhook_url() -> str:
    """Return the configured Discord webhook URL, or empty string if not set."""
    return os.getenv("DISCORD_WEBHOOK_URL", "")


def _timestamp_now() -> str:
    """ISO-8601 timestamp for Discord embed footer."""
    return datetime.now(timezone.utc).isoformat()


async def _post_embed(embed: dict) -> None:
    """POST a single embed to the Discord webhook. Silent no-op if URL not configured."""
    url = _get_webhook_url()
    if not url:
        return

    payload = {"embeds": [embed]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code not in (200, 204):
                logger.warning(
                    f"Discord webhook returned {resp.status_code}: {resp.text[:200]}"
                )
    except Exception as exc:
        logger.warning(f"Discord webhook error: {exc}")


async def send_trade_alert(
    market: str,
    side: str,
    amount: float,
    price: float,
    reason: str,
) -> None:
    """Send a rich embed with trade details.

    Args:
        market:  Market question or description (truncated to 200 chars).
        side:    Trade direction, e.g. "BUY_YES", "BUY_NO", "SELL".
        amount:  Trade size in USD.
        price:   Entry/exit price (0.0 – 1.0).
        reason:  Human-readable reason for the trade.
    """
    color = COLOR_GREEN if "BUY" in side.upper() else COLOR_BLUE
    implied_prob = f"{price * 100:.1f}%"
    embed = {
        "title": f"Trade Executed — {side}",
        "description": market[:200],
        "color": color,
        "fields": [
            {"name": "Side", "value": side, "inline": True},
            {"name": "Amount", "value": f"${amount:.2f}", "inline": True},
            {"name": "Price", "value": f"{price:.4f} ({implied_prob})", "inline": True},
            {"name": "Reason", "value": reason[:500], "inline": False},
        ],
        "timestamp": _timestamp_now(),
        "footer": {"text": "PolyBot"},
    }
    await _post_embed(embed)


async def send_pnl_update(
    total_pnl: float,
    daily_pnl: float,
    win_rate: float,
    open_positions: int,
) -> None:
    """Send a daily portfolio summary embed.

    Args:
        total_pnl:       All-time realized P&L in USD.
        daily_pnl:       Today's realized P&L in USD.
        win_rate:        Win rate as a percentage (0–100).
        open_positions:  Number of currently open positions.
    """
    color = COLOR_GREEN if total_pnl >= 0 else COLOR_RED
    total_sign = "+" if total_pnl >= 0 else ""
    daily_sign = "+" if daily_pnl >= 0 else ""
    embed = {
        "title": "Portfolio PnL Update",
        "color": color,
        "fields": [
            {
                "name": "Total PnL",
                "value": f"{total_sign}${total_pnl:.2f}",
                "inline": True,
            },
            {
                "name": "Today's PnL",
                "value": f"{daily_sign}${daily_pnl:.2f}",
                "inline": True,
            },
            {
                "name": "Win Rate",
                "value": f"{win_rate:.1f}%",
                "inline": True,
            },
            {
                "name": "Open Positions",
                "value": str(open_positions),
                "inline": True,
            },
        ],
        "timestamp": _timestamp_now(),
        "footer": {"text": "PolyBot"},
    }
    await _post_embed(embed)


async def send_error_alert(error_msg: str, strategy_name: str) -> None:
    """Send a red embed for errors.

    Args:
        error_msg:     The error message or exception string.
        strategy_name: Name of the strategy or component that raised the error.
    """
    embed = {
        "title": f"Error in {strategy_name}",
        "description": error_msg[:1000],
        "color": COLOR_RED,
        "timestamp": _timestamp_now(),
        "footer": {"text": "PolyBot — Error Alert"},
    }
    await _post_embed(embed)


async def send_bot_status(mode: str, balance: float, positions_count: int) -> None:
    """Send a green/yellow embed for bot heartbeat.

    Args:
        mode:            Trading mode string, e.g. "DRY RUN" or "LIVE".
        balance:         Current portfolio value in USD.
        positions_count: Number of currently open positions.
    """
    color = COLOR_GREEN if mode.upper() == "LIVE" else COLOR_YELLOW
    embed = {
        "title": "PolyBot Heartbeat",
        "color": color,
        "fields": [
            {"name": "Mode", "value": mode, "inline": True},
            {"name": "Portfolio", "value": f"${balance:.2f}", "inline": True},
            {"name": "Open Positions", "value": str(positions_count), "inline": True},
        ],
        "timestamp": _timestamp_now(),
        "footer": {"text": "PolyBot"},
    }
    await _post_embed(embed)
