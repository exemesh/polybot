"""
core/discord_interactions.py — Discord Interaction Endpoint Server

Receives button click interactions from Discord and stores approval/rejection
in SQLite so hitl_gate.py can poll for the response.

Runs as a persistent FastAPI app on port 8765.
Exposed publicly via Cloudflare Tunnel (cloudflared).

Setup (one-time):
  1. brew install cloudflare/cloudflare/cloudflared
  2. cloudflared tunnel login
  3. cloudflared tunnel create polybot-hitl
  4. Add tunnel URL as Discord App Interaction Endpoint URL in Discord Developer Portal
  5. launchctl load ~/Library/LaunchAgents/com.polybot-interactions.plist

Discord sends a POST to this server when a button is clicked.
We verify the signature, write approved/rejected to SQLite, return 200.
"""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import JSONResponse

# Load .env so DISCORD_PUBLIC_KEY is available when running via launchd
try:
    from dotenv import load_dotenv as _load_dotenv
    from pathlib import Path as _Path
    _load_dotenv(_Path(__file__).parent.parent / ".env")
except Exception:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH = Path(os.getenv("DB_PATH", "data/polybot.db"))
DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "")  # from Discord Developer Portal
PORT = int(os.getenv("INTERACTIONS_PORT", "8765"))

logger = logging.getLogger("polybot.interactions")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="PolyBot Discord Interactions", docs_url=None, redoc_url=None)

# ── DB init ───────────────────────────────────────────────────────────────────
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hitl_responses (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id   TEXT NOT NULL UNIQUE,
            decision     TEXT NOT NULL,   -- 'approved' or 'rejected'
            decided_by   TEXT,
            decided_at   TEXT NOT NULL,
            trade_info   TEXT            -- JSON snapshot of the trade
        )
    """)
    conn.commit()
    conn.close()

# ── Signature verification ────────────────────────────────────────────────────
def verify_discord_signature(public_key: str, timestamp: str, body: bytes, signature: str) -> bool:
    """Verify Discord's Ed25519 request signature."""
    if not public_key:
        return True  # Skip verification in dev mode (no public key set)
    try:
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError
        vk = VerifyKey(bytes.fromhex(public_key))
        vk.verify(f"{timestamp}{body.decode()}".encode(), bytes.fromhex(signature))
        return True
    except Exception:
        return False

# ── Interaction handler ───────────────────────────────────────────────────────
@app.post("/interactions")
async def handle_interaction(request: Request):
    """Main Discord interaction endpoint."""
    body = await request.body()
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp  = request.headers.get("X-Signature-Timestamp", "")

    if DISCORD_PUBLIC_KEY and not verify_discord_signature(DISCORD_PUBLIC_KEY, timestamp, body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    interaction_type = payload.get("type")

    # Type 1 = PING (Discord verifies endpoint during setup)
    if interaction_type == 1:
        return JSONResponse({"type": 1})

    # Type 3 = MESSAGE_COMPONENT (button click)
    if interaction_type == 3:
        data       = payload.get("data", {})
        custom_id  = data.get("custom_id", "")
        message_id = payload.get("message", {}).get("id", "")
        user       = payload.get("member", {}).get("user", {}) or payload.get("user", {})
        username   = user.get("username", "unknown")

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        # custom_id format: "hitl_approve_{message_id}" or "hitl_reject_{message_id}"
        if custom_id.startswith("hitl_approve_"):
            decision = "approved"
        elif custom_id.startswith("hitl_reject_"):
            decision = "rejected"
        else:
            return JSONResponse({"type": 4, "data": {"content": "Unknown action.", "flags": 64}})

        # Store decision in SQLite
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("""
                INSERT OR REPLACE INTO hitl_responses
                (message_id, decision, decided_by, decided_at, trade_info)
                VALUES (?, ?, ?, ?, ?)
            """, (message_id, decision, username, now, json.dumps(payload.get("message", {}).get("content", ""))))
            conn.commit()
            conn.close()
            logger.info(f"[HITL] {decision.upper()} by @{username} — message_id={message_id}")
        except Exception as exc:
            logger.error(f"[HITL] DB write failed: {exc}")

        # Respond to Discord — update the message to show the decision
        emoji   = "✅" if decision == "approved" else "❌"
        label   = "APPROVED" if decision == "approved" else "REJECTED"
        content = f"{emoji} Trade **{label}** by @{username}"

        return JSONResponse({
            "type": 7,  # UPDATE_MESSAGE
            "data": {
                "content": content,
                "components": []  # Remove buttons after decision
            }
        })

    # Unhandled type
    return JSONResponse({"type": 1})


@app.get("/health")
async def health():
    return {"status": "ok", "db": str(DB_PATH), "time": time.time()}


if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
