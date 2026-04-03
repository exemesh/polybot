"""
Amara — Trade Execution Alert Agent → #amara

Posts to Discord ONLY when a trade has actually been EXECUTED (filled)
by polybot or kalbot, and only when edge ≥ 15%.

No more opportunity spam — if polybot/kalbot didn't pull the trigger,
Amara stays silent.

Tracks last-seen trade IDs in data/amara_seen_trades.json to avoid
duplicate alerts across cycles.
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

from utils.discord_alerts import DiscordAlerts

logger = logging.getLogger("polybot.amara")

# ── Paths ─────────────────────────────────────────────────────────────────────
_ROOT        = Path(__file__).parent.parent
DATA_DIR     = _ROOT / "data"
DB_PATH      = DATA_DIR / "polybot.db"
SEEN_PATH    = DATA_DIR / "amara_seen_trades.json"

# Kalbot status lives one directory up
KALBOT_STATUS = _ROOT.parent / "kalbot" / "data" / "status.json"

# Discord channel
AMARA_CHANNEL = "1483029658072121355"   # #amara

# Only alert on trades with edge at or above this threshold
MIN_EDGE_PCT = 15.0

# Strategy display names
STRATEGY_LABELS = {
    "swarm_forecaster":      "SwarmForecaster",
    "news_arb":              "NewsArb",
    "P1_SentimentSpike":     "P1 · SentimentSpike",
    "P2_OverreactionFader":  "P2 · OverreactionFader",
    "P3_LiquiditySniper":    "P3 · LiquiditySniper",
    "K1_FairValue":          "K1 · FairValue",
    "K2_Resolution":         "K2 · Resolution",
    "K3_SpreadCapture":      "K3 · SpreadCapture",
}


# ── Seen-trades tracker ───────────────────────────────────────────────────────

def _load_seen() -> dict:
    try:
        if SEEN_PATH.exists():
            return json.loads(SEEN_PATH.read_text())
    except Exception:
        pass
    return {"polybot_ids": [], "kalbot_tickers": []}


def _save_seen(seen: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=2))


# ── Polybot trade reader ──────────────────────────────────────────────────────

def fetch_new_polybot_trades(seen_ids: list) -> list[dict]:
    """Read newly executed polybot trades from SQLite. Only live fills with edge ≥ 15%."""
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Only live trades that aren't in seen list
        placeholders = ",".join("?" * len(seen_ids)) if seen_ids else "NULL"
        query = f"""
            SELECT id, strategy, market_question, side, price, size_usd,
                   edge_pct, order_id, status, timestamp
            FROM trades
            WHERE dry_run = 0
              AND status IN ('open', 'filled', 'won', 'lost', 'closed')
              AND edge_pct >= ?
              {"AND id NOT IN (" + placeholders + ")" if seen_ids else ""}
            ORDER BY id DESC LIMIT 20
        """
        params = [MIN_EDGE_PCT] + (seen_ids if seen_ids else [])
        rows = cur.fetchall() if False else cur.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"Amara: polybot DB read failed: {e}")
        return []


# ── Kalbot position reader ────────────────────────────────────────────────────

def fetch_new_kalbot_positions(seen_tickers: list) -> list[dict]:
    """Read new filled positions from kalbot status.json. Only entries with edge ≥ 15%."""
    try:
        if not KALBOT_STATUS.exists():
            return []
        data = json.loads(KALBOT_STATUS.read_text())
        positions = data.get("positions", [])
        new_fills = []
        for pos in positions:
            ticker = pos.get("ticker", "")
            if ticker in seen_tickers:
                continue
            edge = pos.get("edge_pct", 0.0)
            # Kalbot K1 requires >12% edge; if edge_pct not stored, check entry price
            # as a proxy (entries below 30¢ on YES = asymmetric bet, likely high edge)
            if edge == 0.0:
                entry = pos.get("entry_price_cents", 50)
                # Rough proxy: if YES bought < 35¢, assume edge ≥ 15%
                edge = max(0.0, (35 - entry) * 2.0) if pos.get("side") == "yes" and entry < 35 else 0.0
            if edge >= MIN_EDGE_PCT:
                pos["_edge_pct"] = round(edge, 1)
                new_fills.append(pos)
        return new_fills
    except Exception as e:
        logger.warning(f"Amara: kalbot status read failed: {e}")
        return []


# ── Discord alert builders ────────────────────────────────────────────────────

async def _post_trade_alert(discord: DiscordAlerts, trade: dict, bot: str) -> None:
    """Post a single executed-trade alert embed."""
    webhook_url = os.getenv("DISCORD_WEBHOOK_AMARA", "")
    if not webhook_url:
        return

    if bot == "polybot":
        strat_raw  = trade.get("strategy", "unknown")
        strat      = STRATEGY_LABELS.get(strat_raw, strat_raw)
        question   = trade.get("market_question", "?")[:120]
        side       = trade.get("side", "?").replace("BUY_", "")
        price      = trade.get("price", 0)
        size       = trade.get("size_usd", 0)
        edge       = trade.get("edge_pct", 0)
        order_id   = trade.get("order_id", "")[:12]
        ts         = trade.get("timestamp", "")[:16]
        color      = 0x00C851   # green
        title      = f"✅ Trade Executed — Polymarket"
        desc_lines = [
            f"**{question}**",
            f"",
            f"Side: **{side}** | Price: **{price:.0%}** | Size: **${size:.2f}**",
            f"Edge: **{edge:.1f}%** | Strategy: `{strat}`",
            f"Order: `{order_id}` | Placed: {ts} UTC",
        ]
    else:
        ticker    = trade.get("ticker", "?")
        side      = trade.get("side", "?").upper()
        entry     = trade.get("entry_price_cents", 0)
        count     = trade.get("count", 0)
        size      = trade.get("size_usd", 0)
        edge      = trade.get("_edge_pct", 0)
        opened    = trade.get("opened_at", "")[:16]
        strat     = STRATEGY_LABELS.get(trade.get("strategy", "K1_FairValue"), "K1 · FairValue")
        color     = 0x007BFF   # blue for Kalshi
        title     = f"✅ Trade Executed — Kalshi"
        desc_lines = [
            f"**`{ticker}`**",
            f"",
            f"Side: **{side}** | Entry: **{entry}¢** | Contracts: **×{count}** | Size: **${size:.2f}**",
            f"Edge: **~{edge:.0f}%** | Strategy: `{strat}`",
            f"Opened: {opened} UTC",
        ]

    embed = {
        "title": title,
        "description": "\n".join(desc_lines),
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Amara · {bot} LIVE"},
    }

    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            webhook_url,
            json={"username": "Amara", "embeds": [embed]},
        )
    logger.info(f"Amara: trade alert posted ({bot}) — {trade.get('market_question', trade.get('ticker', '?'))[:60]}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_amara() -> None:
    """
    Amara Intelligence agent.

    Each run:
      1. Read new FILLED trades from polybot DB (edge ≥ 15%)
      2. Read new FILLED positions from kalbot status.json (edge ≥ 15%)
      3. Post an alert for each new trade — then mark as seen
      4. Never post about opportunities that weren't executed
    """
    logger.info("Amara agent starting...")

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord   = DiscordAlerts(bot_token=bot_token)

    seen         = _load_seen()
    seen_poly    = seen.get("polybot_ids", [])
    seen_kal     = seen.get("kalbot_tickers", [])

    alerted = 0

    # ── Polybot executed trades ─────────────────────────────────────────────
    poly_trades = await asyncio.to_thread(fetch_new_polybot_trades, seen_poly)
    for trade in poly_trades:
        try:
            await _post_trade_alert(discord, trade, "polybot")
            seen_poly.append(trade["id"])
            alerted += 1
        except Exception as exc:
            logger.error(f"Amara: polybot alert failed: {exc}")

    # ── Kalbot executed trades ──────────────────────────────────────────────
    kal_positions = await asyncio.to_thread(fetch_new_kalbot_positions, seen_kal)
    for pos in kal_positions:
        try:
            await _post_trade_alert(discord, pos, "kalbot")
            seen_kal.append(pos["ticker"])
            alerted += 1
        except Exception as exc:
            logger.error(f"Amara: kalbot alert failed: {exc}")

    # Keep seen lists bounded (last 500 each)
    seen["polybot_ids"]      = seen_poly[-500:]
    seen["kalbot_tickers"]   = seen_kal[-500:]
    _save_seen(seen)

    if alerted == 0:
        logger.info("Amara: no new executed trades above 15% edge this cycle.")
    else:
        logger.info(f"Amara: {alerted} trade alert(s) posted.")
