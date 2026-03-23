"""
core/canary.py — Canary Deployment Support

Principle: "Roll out parameter changes to a small slice before full deployment."

When a strategy config or parameter set is updated, canary mode routes a
defined subset of markets through the new configuration first. If canary
performance is good after N trades, the new config is promoted to all markets.

Canary config lives in .polybot/canary.yaml (version-controlled):

    active: true
    strategy: ai_forecaster
    variant: v2
    market_slice: 0.1          # 10% of candidate markets go to canary
    min_trades_to_promote: 20  # promote after 20 trades
    min_win_rate: 0.55         # only promote if win rate >= 55%
    created_at: 2026-03-23
    notes: "Testing lower edge threshold (1.5% vs 2.0%)"

Based on:
  https://www.marktechpost.com/2026/03/21/safely-deploying-ml-models-to-production-
  four-controlled-strategies-a-b-canary-interleaved-shadow-testing/
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional, Any

logger = logging.getLogger("polybot.canary")

CANARY_CONFIG_PATH = Path(".polybot/canary.yaml")


def load_canary_config() -> Optional[dict]:
    """
    Load canary config from .polybot/canary.yaml.
    Returns None if no canary is active or file doesn't exist.
    """
    if not CANARY_CONFIG_PATH.exists():
        return None

    try:
        import yaml  # optional dependency
        with open(CANARY_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        if not cfg or not cfg.get("active", False):
            return None
        logger.info(
            f"[CANARY] Active: strategy={cfg.get('strategy')} "
            f"variant={cfg.get('variant')} slice={cfg.get('market_slice', 0.1):.0%}"
        )
        return cfg
    except ImportError:
        # Fallback: parse YAML manually for simple key:value pairs
        cfg = {}
        with open(CANARY_CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if ":" in line and not line.startswith("#"):
                    key, _, val = line.partition(":")
                    val = val.strip()
                    if val.lower() in ("true", "false"):
                        val = val.lower() == "true"
                    elif val.replace(".", "").isdigit():
                        val = float(val) if "." in val else int(val)
                    cfg[key.strip()] = val
        return cfg if cfg.get("active") else None
    except Exception as exc:
        logger.warning(f"[CANARY] Could not load config: {exc}")
        return None


def is_canary_market(market_id: str, canary_cfg: dict) -> bool:
    """
    Deterministically assign a market to canary or live slice.
    Uses market_id hash mod 100 to get a stable, repeatable assignment.
    """
    if not canary_cfg:
        return False
    slice_pct = float(canary_cfg.get("market_slice", 0.1))
    # Stable hash: same market always maps to same bucket
    bucket = abs(hash(market_id)) % 100
    return bucket < int(slice_pct * 100)


def get_canary_stats(db_path: str, strategy: str, variant: str) -> dict:
    """
    Return canary trade stats from the trades table.
    Canary trades are identified by strategy name + 'canary_variant' in extra metadata,
    or by checking the shadow_log table.
    """
    try:
        conn = sqlite3.connect(db_path)

        # Check shadow_log for canary variant performance
        row = conn.execute("""
            SELECT COUNT(*) as signals,
                   COUNT(simulated_pnl) as resolved,
                   COALESCE(SUM(simulated_pnl), 0) as total_pnl,
                   COALESCE(SUM(CASE WHEN simulated_pnl > 0 THEN 1 ELSE 0 END), 0) as wins
            FROM shadow_log
            WHERE strategy_name = ? AND variant = ?
        """, (strategy, variant)).fetchone()
        conn.close()

        signals = row[0] or 0
        resolved = row[1] or 0
        total_pnl = row[2] or 0.0
        wins = row[3] or 0
        win_rate = (wins / resolved) if resolved > 0 else 0.0

        return {
            "strategy": strategy,
            "variant": variant,
            "signals": signals,
            "resolved": resolved,
            "win_rate": win_rate,
            "total_pnl": round(total_pnl, 4),
        }
    except Exception as exc:
        return {"error": str(exc)}


def should_promote_canary(db_path: str, canary_cfg: dict) -> tuple[bool, str]:
    """
    Evaluate whether the canary variant should be promoted to full production.

    Returns:
        (should_promote: bool, reason: str)
    """
    if not canary_cfg:
        return False, "no active canary"

    strategy = canary_cfg.get("strategy", "")
    variant  = canary_cfg.get("variant", "candidate")
    min_trades   = int(canary_cfg.get("min_trades_to_promote", 20))
    min_win_rate = float(canary_cfg.get("min_win_rate", 0.55))

    stats = get_canary_stats(db_path, strategy, variant)
    if "error" in stats:
        return False, f"stats error: {stats['error']}"

    resolved = stats["resolved"]
    win_rate = stats["win_rate"]

    if resolved < min_trades:
        return False, f"only {resolved}/{min_trades} trades resolved — not enough data"

    if win_rate < min_win_rate:
        return False, f"win rate {win_rate:.1%} below threshold {min_win_rate:.1%}"

    return True, (
        f"canary ready to promote: {resolved} trades, "
        f"win_rate={win_rate:.1%}, pnl=${stats['total_pnl']:+.4f}"
    )


def create_canary_config(
    strategy: str,
    variant: str,
    market_slice: float = 0.1,
    min_trades: int = 20,
    min_win_rate: float = 0.55,
    notes: str = "",
) -> None:
    """Write a new canary config to .polybot/canary.yaml."""
    from datetime import datetime, timezone
    CANARY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = f"""# Canary Deployment Config
# Edit this file to control canary rollout. Set active: false to disable.
active: true
strategy: {strategy}
variant: {variant}
market_slice: {market_slice}
min_trades_to_promote: {min_trades}
min_win_rate: {min_win_rate}
created_at: {datetime.now(timezone.utc).date().isoformat()}
notes: "{notes}"
"""
    with open(CANARY_CONFIG_PATH, "w") as f:
        f.write(content)
    logger.info(f"[CANARY] Config written to {CANARY_CONFIG_PATH}")
