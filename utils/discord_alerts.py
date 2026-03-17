"""
Discord alert module for PolyBot.

Two interfaces are provided:

1. Module-level async functions (webhook-based, legacy):
   - send_trade_alert / send_pnl_update / send_error_alert / send_bot_status
   - Require DISCORD_WEBHOOK_URL environment variable.

2. DiscordAlerts class (bot API-based, preferred for channel targeting):
   - Uses Discord Bot Token via POST https://discord.com/api/v10/channels/{id}/messages
   - Require DISCORD_BOT_TOKEN environment variable (or pass token directly).
   - Methods: send_trade_alert / send_pnl_update / send_risk_alert / send_info

Uses Discord embed format with colors:
  Green  (0x00C851) — profit / positive status
  Red    (0xFF4444) — loss / error / risk
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

# Discord Bot API base URL
DISCORD_API_BASE = "https://discord.com/api/v10"


class DiscordAlerts:
    """Send rich embed messages to specific Discord channels via the Bot API.

    Usage:
        alerts = DiscordAlerts()  # reads DISCORD_BOT_TOKEN from env
        await alerts.send_trade_alert(channel_id, market, side, amount, price)

    All methods are async and are silent no-ops when the bot token is not set.
    """

    def __init__(self, bot_token: str = ""):
        self.bot_token = bot_token or os.getenv("DISCORD_BOT_TOKEN", "")
        if self.bot_token:
            logger.info("DiscordAlerts (bot API) enabled")
        else:
            logger.info("DiscordAlerts: DISCORD_BOT_TOKEN not set — alerts disabled")

    # ── Internal helper ─────────────────────────────────────────────────────

    async def _post_channel_message(self, channel_id: str, embed: dict) -> None:
        """POST an embed to a Discord channel via the Bot API."""
        if not self.bot_token or not channel_id:
            return
        url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {self.bot_token}",
            "Content-Type": "application/json",
        }
        payload = {"embeds": [embed]}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code not in (200, 201):
                    logger.warning(
                        f"Discord bot API returned {resp.status_code} for channel "
                        f"{channel_id}: {resp.text[:200]}"
                    )
        except Exception as exc:
            logger.warning(f"Discord bot API error: {exc}")

    # ── Public methods ───────────────────────────────────────────────────────

    async def send_trade_alert(
        self,
        channel_id: str,
        market: str,
        side: str,
        amount: float,
        price: float,
    ) -> None:
        """Send a trade execution alert to the specified channel.

        Args:
            channel_id: Discord channel snowflake ID string.
            market:     Market question or description (truncated to 200 chars).
            side:       Trade direction, e.g. "BUY_YES", "BUY_NO", "SELL".
            amount:     Trade size in USD.
            price:      Entry/exit price (0.0 – 1.0).
        """
        is_buy = "BUY" in side.upper()
        color = COLOR_GREEN if is_buy else COLOR_BLUE
        implied_prob = f"{price * 100:.1f}%"
        embed = {
            "title": f"Trade Executed — {side}",
            "description": market[:200],
            "color": color,
            "fields": [
                {"name": "Side", "value": side, "inline": True},
                {"name": "Amount", "value": f"${amount:.2f}", "inline": True},
                {"name": "Price", "value": f"{price:.4f} ({implied_prob})", "inline": True},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "PolyBot"},
        }
        await self._post_channel_message(channel_id, embed)

    async def send_pnl_update(
        self,
        channel_id: str,
        daily_pnl: float,
        total_pnl: float,
        win_rate: float,
    ) -> None:
        """Send a portfolio P&L summary to the specified channel.

        Args:
            channel_id: Discord channel snowflake ID string.
            daily_pnl:  Today's realized P&L in USD.
            total_pnl:  All-time realized P&L in USD.
            win_rate:   Win rate as a percentage (0–100).
        """
        # If win_rate is a dict, extract the float value
        if isinstance(win_rate, dict):
            win_rate_pct = win_rate.get('win_rate', 0.0)
        else:
            win_rate_pct = float(win_rate)
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
                    "value": f"{win_rate_pct:.1f}%",
                    "inline": True,
                },
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "PolyBot"},
        }
        await self._post_channel_message(channel_id, embed)

    async def send_risk_alert(self, channel_id: str, message: str) -> None:
        """Send a red risk/warning alert to the specified channel.

        Args:
            channel_id: Discord channel snowflake ID string.
            message:    Risk alert message (truncated to 1000 chars).
        """
        embed = {
            "title": "Risk Alert",
            "description": message[:1000],
            "color": COLOR_RED,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "PolyBot — Risk Manager"},
        }
        await self._post_channel_message(channel_id, embed)

    async def send_info(self, channel_id: str, message: str) -> None:
        """Send a blue informational message to the specified channel.

        Args:
            channel_id: Discord channel snowflake ID string.
            message:    Informational message (truncated to 1000 chars).
        """
        embed = {
            "title": "PolyBot Info",
            "description": message[:1000],
            "color": COLOR_BLUE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "PolyBot"},
        }
        await self._post_channel_message(channel_id, embed)

    async def send_webhook(
        self,
        webhook_url: str,
        content: str = None,
        embed: dict = None,
        username: str = None,
        avatar_url: str = None,
    ) -> bool:
        """POST a message directly to a Discord webhook URL.

        Supports custom username override, avatar, and embeds — allowing each
        agent to appear in Discord with its own identity.

        Args:
            webhook_url: Full Discord webhook URL
                         (https://discord.com/api/webhooks/{id}/{token}).
            content:     Plain-text message content.
            embed:       Single embed dict (will be wrapped in a list).
            username:    Override display name for this message.
            avatar_url:  Override avatar image URL for this message.

        Returns:
            True if Discord accepted the payload (200 or 204), False otherwise.
        """
        if not webhook_url:
            return False

        payload: dict = {}
        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]
        if username:
            payload["username"] = username
        if avatar_url:
            payload["avatar_url"] = avatar_url

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(webhook_url, json=payload)
                if resp.status_code not in (200, 204):
                    logger.warning(
                        f"Discord webhook returned {resp.status_code}: {resp.text[:200]}"
                    )
                return resp.status_code in (200, 204)
        except Exception as exc:
            logger.warning(f"Discord webhook error: {exc}")
            return False


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
    # If win_rate is a dict, extract the float value
    if isinstance(win_rate, dict):
        win_rate_pct = win_rate.get('win_rate', 0.0)
    else:
        win_rate_pct = float(win_rate)
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
                "value": f"{win_rate_pct:.1f}%",
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
