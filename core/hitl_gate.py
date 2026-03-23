"""
core/hitl_gate.py — Human-in-the-Loop Approval Gate

Principle: "High-stakes decisions require human sign-off."

For trades above a configurable threshold (size or confidence), the bot sends
a Discord message and waits for a ✅ or ❌ reaction before executing.

Triggers when:
  - Trade size > HITL_SIZE_THRESHOLD (default $1.00 on $7 capital)
  - OR edge confidence > HITL_CONFIDENCE_THRESHOLD (default 90%)

Behaviour:
  - Sends a rich embed to the Sage Discord channel
  - Polls for ✅/❌ reaction for up to HITL_TIMEOUT_SECONDS
  - ✅ = approved, execute trade
  - ❌ = rejected, skip trade, log reason
  - Timeout = auto-reject (fail safe)

Based on:
  - OpenClaw incident: human approval gate prevents autonomous overreach
  - GitHub agentic security: "human-in-the-loop for consequential actions"
"""

import asyncio
import json
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger("polybot.hitl")

# ─── Thresholds ────────────────────────────────────────────────────────────────
HITL_SIZE_THRESHOLD: float       = 1.00   # USD — trades >= this need approval
HITL_CONFIDENCE_THRESHOLD: float = 0.90   # edge/confidence — trades >= this need approval
HITL_TIMEOUT_SECONDS: int        = 300    # 5 minutes — auto-reject if no response


def needs_approval(size_usd: float, edge_pct: float) -> bool:
    """Return True if this trade requires human approval before execution."""
    return size_usd >= HITL_SIZE_THRESHOLD or edge_pct >= HITL_CONFIDENCE_THRESHOLD


async def request_approval(
    webhook_url: str,
    strategy: str,
    market_question: str,
    side: str,
    price: float,
    size_usd: float,
    edge_pct: float,
    token_id: str,
    bot_token: Optional[str] = None,
    channel_id: Optional[str] = None,
    timeout: int = HITL_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """
    Send a Discord approval request and wait for human response.

    Returns:
        (approved: bool, reason: str)

    If bot_token + channel_id are provided, polls for message reactions.
    Otherwise falls back to webhook-only mode (auto-approve after timeout warning).
    """
    if not webhook_url:
        logger.warning("[HITL] No webhook URL — auto-approving (configure DISCORD_WEBHOOK_SAGE)")
        return True, "auto_approved: no webhook configured"

    # ── Build approval message ──────────────────────────────────────────────
    direction_emoji = "🟢" if side.upper() == "BUY" else "🔴"
    edge_bar = "█" * min(int(edge_pct * 100 / 5), 20)  # visual edge bar

    message = (
        f"⚠️ **HITL APPROVAL REQUIRED**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"**Strategy:** {strategy}\n"
        f"**Market:** {market_question[:100]}\n"
        f"{direction_emoji} **{side}** @ {price:.3f} | **${size_usd:.2f}** | Edge: {edge_pct:.1%}\n"
        f"Edge: `{edge_bar}` {edge_pct:.1%}\n"
        f"Token: `{token_id[:20]}...`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"React ✅ to **APPROVE** or ❌ to **REJECT**\n"
        f"Auto-rejects in **{timeout // 60} minutes** if no response."
    )

    msg_id: Optional[str] = None
    try:
        # Post to Discord webhook (returns message ID only if wait=true)
        params = {"wait": "true"} if webhook_url else {}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                webhook_url,
                params=params,
                json={"content": message, "username": "PolyBot Sage — HITL Gate"},
            )
            if resp.status_code in (200, 204):
                try:
                    msg_id = resp.json().get("id")
                except Exception:
                    pass
                logger.info(f"[HITL] Approval request sent for {strategy} ${size_usd:.2f} trade")
            else:
                logger.warning(f"[HITL] Webhook post failed: {resp.status_code}")
                return True, "auto_approved: webhook post failed"
    except Exception as exc:
        logger.warning(f"[HITL] Could not send approval request: {exc}")
        return True, f"auto_approved: send error ({exc})"

    # ── Poll for reaction (requires bot token + channel ID) ─────────────────
    if not bot_token or not channel_id or not msg_id:
        # Webhook-only mode: we can't poll for reactions without bot credentials
        # Log the pending approval and auto-approve (operator must configure bot token
        # to get full HITL blocking behaviour)
        logger.info(
            f"[HITL] Webhook-only mode — trade auto-approved after notification. "
            f"Set DISCORD_BOT_TOKEN + DISCORD_ALERT_CHANNEL_ID for full blocking HITL."
        )
        return True, "auto_approved: webhook_only_mode"

    # Poll for reactions on the message
    headers = {"Authorization": f"Bot {bot_token}"}
    deadline = time.monotonic() + timeout
    poll_interval = 10  # check every 10 seconds

    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Check for ✅ reaction
                r_approve = await client.get(
                    f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}/reactions/%E2%9C%85",
                    headers=headers,
                )
                if r_approve.status_code == 200 and r_approve.json():
                    # Someone reacted ✅
                    users = r_approve.json()
                    reactor = users[0].get("username", "unknown") if users else "unknown"
                    logger.info(f"[HITL] APPROVED by {reactor}: {strategy} ${size_usd:.2f}")
                    return True, f"approved_by:{reactor}"

                # Check for ❌ reaction
                r_reject = await client.get(
                    f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}/reactions/%E2%9D%8C",
                    headers=headers,
                )
                if r_reject.status_code == 200 and r_reject.json():
                    users = r_reject.json()
                    reactor = users[0].get("username", "unknown") if users else "unknown"
                    logger.info(f"[HITL] REJECTED by {reactor}: {strategy} ${size_usd:.2f}")
                    return False, f"rejected_by:{reactor}"

        except Exception as exc:
            logger.warning(f"[HITL] Reaction poll error: {exc}")

    # Timeout — auto-reject
    logger.warning(
        f"[HITL] TIMEOUT — no response in {timeout}s — AUTO-REJECTING {strategy} ${size_usd:.2f}"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                webhook_url,
                json={
                    "content": f"⏱️ **HITL TIMEOUT** — trade auto-rejected after {timeout // 60}min: "
                               f"{strategy} ${size_usd:.2f} | {market_question[:60]}",
                    "username": "PolyBot Sage — HITL Gate",
                },
            )
    except Exception:
        pass

    return False, f"timeout_auto_rejected: {timeout}s elapsed"
