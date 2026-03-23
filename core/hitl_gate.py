"""
core/hitl_gate.py — Human-in-the-Loop Approval Gate (v2 — Discord Buttons)

Sends a Discord message with ✅ Approve / ❌ Reject buttons.
Polls SQLite hitl_responses table for the decision (written by discord_interactions.py).

Requires:
  - discord_interactions.py running on Mac mini (port 8765)
  - Cloudflare Tunnel exposing it publicly
  - Discord App Interaction Endpoint set to the tunnel URL + /interactions

Fallback (no interaction server): webhook-only notification, auto-approves.

Based on:
  - OpenClaw incident: human approval gate prevents autonomous overreach
  - GitHub agentic security: "human-in-the-loop for consequential actions"
"""

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("polybot.hitl")

# ─── Thresholds ────────────────────────────────────────────────────────────────
HITL_SIZE_THRESHOLD: float       = 10.00  # Trades >= this need approval
HITL_CONFIDENCE_THRESHOLD: float = 0.90   # Edge >= this needs approval
HITL_TIMEOUT_SECONDS: int        = 300    # 5 min — auto-reject on timeout


def needs_approval(size_usd: float, edge_pct: float) -> bool:
    return size_usd >= HITL_SIZE_THRESHOLD or edge_pct >= HITL_CONFIDENCE_THRESHOLD


def _build_button_message(
    strategy: str,
    market_question: str,
    side: str,
    price: float,
    size_usd: float,
    edge_pct: float,
    message_id_placeholder: str,
) -> dict:
    """Build a Discord message payload with Approve/Reject buttons."""
    direction_emoji = "🟢" if side.upper() == "BUY" else "🔴"
    edge_bar = "█" * min(int(edge_pct * 100 / 5), 20)

    content = (
        f"⚠️ **TRADE APPROVAL REQUIRED**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"**Strategy:** {strategy}\n"
        f"**Market:** {market_question[:120]}\n"
        f"{direction_emoji} **{side}** @ `{price:.3f}` | **${size_usd:.2f}** | Edge: {edge_pct:.1%}\n"
        f"Edge: `{edge_bar}` {edge_pct:.1%}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ Auto-rejects in **{HITL_TIMEOUT_SECONDS // 60} minutes** if no response."
    )

    return {
        "content": content,
        "username": "PolyBot Sage — HITL Gate",
        "components": [
            {
                "type": 1,  # ACTION_ROW
                "components": [
                    {
                        "type": 2,  # BUTTON
                        "style": 3,  # SUCCESS (green)
                        "label": "✅  Approve Trade",
                        "custom_id": f"hitl_approve_{message_id_placeholder}",
                    },
                    {
                        "type": 2,  # BUTTON
                        "style": 4,  # DANGER (red)
                        "label": "❌  Reject Trade",
                        "custom_id": f"hitl_reject_{message_id_placeholder}",
                    }
                ]
            }
        ]
    }


async def _poll_for_decision(
    db_path: str,
    message_id: str,
    timeout: int,
    poll_interval: int = 8,
) -> tuple[bool, str]:
    """Poll hitl_responses table for a decision on this message_id."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT decision, decided_by FROM hitl_responses WHERE message_id = ?",
                (message_id,)
            ).fetchone()
            conn.close()
            if row:
                decision, decided_by = row
                approved = decision == "approved"
                logger.info(f"[HITL] Decision: {decision.upper()} by @{decided_by}")
                return approved, f"{decision}_by:{decided_by}"
        except Exception as exc:
            logger.warning(f"[HITL] Poll error: {exc}")

    return False, f"timeout_auto_rejected:{timeout}s"


async def request_approval(
    webhook_url: str,
    strategy: str,
    market_question: str,
    side: str,
    price: float,
    size_usd: float,
    edge_pct: float,
    token_id: str,
    db_path: str = "data/polybot.db",
    bot_token: Optional[str] = None,
    channel_id: Optional[str] = None,
    timeout: int = HITL_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """
    Send a Discord approval request with Approve/Reject buttons and
    wait for human response via the interaction server.

    Returns:
        (approved: bool, reason: str)
    """
    if not webhook_url:
        logger.warning("[HITL] No webhook — auto-approving")
        return True, "auto_approved:no_webhook"

    # ── Send message with buttons ────────────────────────────────────────────
    # Use a temporary placeholder for custom_id (will be updated with real message_id)
    temp_id = f"pending_{int(time.time())}"
    payload = _build_button_message(strategy, market_question, side, price, size_usd, edge_pct, temp_id)

    message_id = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{webhook_url}?wait=true", json=payload)
            if resp.status_code == 200:
                message_id = resp.json().get("id")
                logger.info(f"[HITL] Approval buttons sent — message_id={message_id}")
            else:
                logger.warning(f"[HITL] Webhook failed: {resp.status_code}")
                return True, "auto_approved:webhook_failed"
    except Exception as exc:
        logger.warning(f"[HITL] Send error: {exc}")
        return True, f"auto_approved:send_error"

    if not message_id:
        return True, "auto_approved:no_message_id"

    # ── Update buttons with real message_id in custom_ids ───────────────────
    # Patch the message to use the real message_id in custom_ids
    # (Discord sent message_id after posting — now update component custom_ids)
    real_payload = _build_button_message(strategy, market_question, side, price, size_usd, edge_pct, message_id)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Edit message via webhook to update custom_ids
            parts = webhook_url.rstrip("/").split("/")
            if len(parts) >= 2:
                wh_id, wh_token = parts[-2], parts[-1]
                edit_url = f"https://discord.com/api/v10/webhooks/{wh_id}/{wh_token}/messages/{message_id}"
                await client.patch(edit_url, json={
                    "content": real_payload["content"],
                    "components": real_payload["components"]
                })
    except Exception:
        pass  # non-critical — buttons still work with temp_id in fallback

    # ── Check if interaction server is running ───────────────────────────────
    interactions_available = False
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get("http://localhost:8765/health")
            interactions_available = r.status_code == 200
    except Exception:
        pass

    if not interactions_available:
        # Interaction server not running — log and auto-approve
        logger.warning(
            "[HITL] Interaction server not running on :8765 — auto-approving. "
            "Run: python core/discord_interactions.py to enable button responses."
        )
        return True, "auto_approved:interaction_server_offline"

    # ── Poll for decision ────────────────────────────────────────────────────
    approved, reason = await _poll_for_decision(db_path, message_id, timeout)

    if not approved and "timeout" in reason:
        # Send timeout notification
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(webhook_url, json={
                    "content": (
                        f"⏱️ **TIMEOUT** — trade auto-rejected after {timeout // 60}min\n"
                        f"**{strategy}** | {side} ${size_usd:.2f} | {market_question[:80]}"
                    ),
                    "username": "PolyBot Sage — HITL Gate"
                })
        except Exception:
            pass

    return approved, reason
