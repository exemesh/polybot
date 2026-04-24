"""Hardcoded safety rails that CANNOT be raised via config.

The whole point of this file is to protect you from your future self editing
config.json at 2am. If you want to change these, you must edit the source.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Tuple

log = logging.getLogger("polybot.safety")

# ============================================================================
# HARD CAPS — do not raise without reading a week of trade logs.
# ============================================================================
HARD_MAX_BET_USD: float = 5.0
HARD_DAILY_LOSS_CAP_USD: float = 30.0
HARD_WEEKLY_LOSS_CAP_USD: float = 60.0
HARD_MIN_BANKROLL_USD: float = 50.0
HARD_MAX_CONCURRENT_POSITIONS: int = 2

# Emergency stop file — `touch EMERGENCY_STOP` at repo root to flatten & exit.
EMERGENCY_STOP_FILENAME: str = "EMERGENCY_STOP"

# Go-live guard: confirmation token the user must type
GOLIVE_CONFIRM_TOKEN: str = "I-UNDERSTAND-I-WILL-LIKELY-LOSE-MONEY"


def validate_config_against_hard_caps(cfg) -> None:
    """Crash on startup if config tries to exceed any hard cap."""
    hc = HARD_MAX_BET_USD
    for k in ("bet_usd_above_180s_left", "bet_usd_120_to_180s_left", "bet_usd_below_120s_left"):
        v = getattr(cfg.sizing, k)
        if v > hc:
            raise RuntimeError(
                f"config sizing.{k}=${v} exceeds hard cap ${hc}. Edit config/config.json."
            )
    if cfg.risk.daily_loss_cap_usd > HARD_DAILY_LOSS_CAP_USD:
        raise RuntimeError(
            f"config risk.daily_loss_cap_usd=${cfg.risk.daily_loss_cap_usd} "
            f"exceeds hard cap ${HARD_DAILY_LOSS_CAP_USD}"
        )
    if cfg.risk.weekly_loss_cap_usd > HARD_WEEKLY_LOSS_CAP_USD:
        raise RuntimeError(
            f"config risk.weekly_loss_cap_usd=${cfg.risk.weekly_loss_cap_usd} "
            f"exceeds hard cap ${HARD_WEEKLY_LOSS_CAP_USD}"
        )
    if cfg.risk.min_bankroll_usd < HARD_MIN_BANKROLL_USD:
        raise RuntimeError(
            f"config risk.min_bankroll_usd=${cfg.risk.min_bankroll_usd} "
            f"below hard floor ${HARD_MIN_BANKROLL_USD}"
        )
    if cfg.risk.max_concurrent_positions > HARD_MAX_CONCURRENT_POSITIONS:
        raise RuntimeError(
            f"config risk.max_concurrent_positions={cfg.risk.max_concurrent_positions} "
            f"exceeds hard cap {HARD_MAX_CONCURRENT_POSITIONS}"
        )


def emergency_stop_file_present(repo_root: Path) -> bool:
    return (repo_root / EMERGENCY_STOP_FILENAME).exists()


def require_live_confirmation() -> None:
    """Interactive confirmation before trading with real money."""
    print()
    print("=" * 72)
    print("  LIVE TRADING MODE")
    print("=" * 72)
    print(
        "  You are about to place REAL orders on Polymarket with REAL money.\n"
        "  Expected outcome: you may lose all of it.\n"
        "  Hard caps in effect:\n"
        f"    • Max bet size: ${HARD_MAX_BET_USD}\n"
        f"    • Daily loss cap: ${HARD_DAILY_LOSS_CAP_USD}\n"
        f"    • Weekly loss cap: ${HARD_WEEKLY_LOSS_CAP_USD}\n"
        f"    • Min bankroll: ${HARD_MIN_BANKROLL_USD}\n"
        "  To proceed, type exactly:\n"
        f"    {GOLIVE_CONFIRM_TOKEN}\n"
    )
    try:
        got = input("  Confirmation: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("Live confirmation aborted.")
    if got != GOLIVE_CONFIRM_TOKEN:
        raise SystemExit("Live confirmation token did not match. Exiting.")
    print("  Confirmed. Starting live mode.")
    print("=" * 72)


def enforce_order_envelope(bet_usd: float, fav_price: float) -> Tuple[float, float]:
    """Final server-side-style check right before order submission.

    Returns (bet_usd_capped, size_contracts). Raises if anything looks wrong.
    """
    if bet_usd <= 0:
        raise RuntimeError(f"bet_usd must be > 0, got {bet_usd}")
    if bet_usd > HARD_MAX_BET_USD:
        log.warning("enforcing bet cap: %.2f → %.2f", bet_usd, HARD_MAX_BET_USD)
        bet_usd = HARD_MAX_BET_USD
    if not (0.01 <= fav_price <= 0.99):
        raise RuntimeError(f"fav_price must be in [0.01, 0.99], got {fav_price}")
    # 1 contract = $1 payout, costs `fav_price` USD
    size_contracts = round(bet_usd / fav_price, 4)
    # Polymarket min order: 5 contracts typically
    if size_contracts < 5:
        raise RuntimeError(
            f"size {size_contracts} contracts < 5 min. Raise bet_usd or lower fav_price."
        )
    return bet_usd, size_contracts


class ConsecutiveRejectTracker:
    """Tracks consecutive rejected/failed orders to pause on 3 in a row."""

    def __init__(self, limit: int = 3, pause_sec: int = 300) -> None:
        self.limit = limit
        self.pause_sec = pause_sec
        self.count = 0
        self.paused_until = 0.0

    def on_success(self) -> None:
        self.count = 0

    def on_failure(self) -> None:
        self.count += 1
        if self.count >= self.limit:
            self.paused_until = time.time() + self.pause_sec
            log.warning("pausing %ds after %d consecutive order failures", self.pause_sec, self.count)

    def is_paused(self) -> bool:
        return time.time() < self.paused_until
