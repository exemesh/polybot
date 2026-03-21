"""
Win Rate Monitor
================
Tracks win rate over a sliding window. Triggers recalibration when degraded.
"""
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

WINDOW_SIZE = 20
MIN_SAMPLE_SIZE = 10
WARN_THRESHOLD = 0.55
RECAL_THRESHOLD = 0.40
RECAL_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "recalibration.json")


@dataclass
class WinRateResult:
    status: str
    win_rate: float
    sample_size: int
    window_size: int
    worst_strategy: Optional[str]
    message: str
    per_strategy: dict
    recalibration_applied: bool = False


def check(db) -> WinRateResult:
    """Check win rate over last WINDOW_SIZE closed trades."""
    # Use _get_conn() from Portfolio (returns a context-manager sqlite3 connection)
    try:
        with db._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT pnl, strategy FROM trades
                WHERE status IN ('won', 'lost', 'resolved') AND pnl IS NOT NULL
                ORDER BY closed_at DESC LIMIT ?
            """, (WINDOW_SIZE,))
            recent = cursor.fetchall()
    except Exception:
        # Fallback: open DB directly
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "polybot.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT pnl, strategy FROM trades
            WHERE status IN ('won', 'lost', 'resolved') AND pnl IS NOT NULL
            ORDER BY closed_at DESC LIMIT ?
        """, (WINDOW_SIZE,))
        recent = cursor.fetchall()
        conn.close()

    sample_size = len(recent)

    if sample_size < MIN_SAMPLE_SIZE:
        return WinRateResult(
            status="ok", win_rate=0.0, sample_size=sample_size,
            window_size=WINDOW_SIZE, worst_strategy=None,
            message=f"Insufficient data ({sample_size}/{MIN_SAMPLE_SIZE} trades needed)",
            per_strategy={}
        )

    wins = sum(1 for r in recent if r[0] > 0)
    win_rate = wins / sample_size

    strategy_stats = {}
    for pnl, strategy in recent:
        s = strategy or "unknown"
        strategy_stats.setdefault(s, {"wins": 0, "losses": 0})
        strategy_stats[s]["wins" if pnl > 0 else "losses"] += 1

    per_strategy = {
        s: {**v, "total": v["wins"] + v["losses"],
            "win_rate": v["wins"] / (v["wins"] + v["losses"]) if (v["wins"] + v["losses"]) > 0 else 0.0}
        for s, v in strategy_stats.items()
    }
    worst_strategy = min(per_strategy, key=lambda s: per_strategy[s]["win_rate"]) if per_strategy else None

    if win_rate >= WARN_THRESHOLD:
        status, message = "ok", f"Win rate healthy: {win_rate:.1%} over last {sample_size} trades"
    elif win_rate >= RECAL_THRESHOLD:
        status, message = "warn", f"Win rate declining: {win_rate:.1%} (threshold: {WARN_THRESHOLD:.0%})"
    else:
        status, message = "recalibrate", f"Win rate critical: {win_rate:.1%} — recalibration triggered"

    result = WinRateResult(
        status=status, win_rate=win_rate, sample_size=sample_size,
        window_size=WINDOW_SIZE, worst_strategy=worst_strategy,
        message=message, per_strategy=per_strategy
    )
    if status == "recalibrate":
        _write_recalibration(result)
        result.recalibration_applied = True
    return result


def _write_recalibration(result: WinRateResult):
    os.makedirs(os.path.dirname(RECAL_FILE), exist_ok=True)
    recal = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trigger_win_rate": result.win_rate,
        "trigger_reason": result.message,
        "worst_strategy": result.worst_strategy,
        "adjustments": {
            "min_edge_increase": 0.02,
            "position_size_factor": 0.75,
            "disabled_strategies": [result.worst_strategy] if result.worst_strategy else []
        },
        "per_strategy": result.per_strategy,
        "expires_after_trades": 20
    }
    with open(RECAL_FILE, "w") as f:
        json.dump(recal, f, indent=2)


def load_recalibration() -> Optional[dict]:
    """Load active recalibration config. Returns None if not present."""
    if not os.path.exists(RECAL_FILE):
        return None
    try:
        with open(RECAL_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def clear_recalibration():
    if os.path.exists(RECAL_FILE):
        os.remove(RECAL_FILE)


def format_discord_alert(result: WinRateResult) -> str:
    emoji = {"ok": "✅", "warn": "⚠️", "recalibrate": "🔴"}[result.status]
    lines = [
        f"{emoji} **WIN RATE MONITOR — {result.status.upper()}**",
        f"Win rate: **{result.win_rate:.1%}** over last {result.sample_size} trades",
        result.message,
    ]
    if result.worst_strategy:
        wr = result.per_strategy.get(result.worst_strategy, {}).get("win_rate", 0)
        lines.append(f"Worst strategy: `{result.worst_strategy}` ({wr:.1%} WR)")
    if result.recalibration_applied:
        lines.append("**Recalibration active** — edge +2%, positions 75%, worst strategy paused")
    return "\n".join(lines)
