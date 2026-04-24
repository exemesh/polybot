"""Risk gates: sizing, daily/weekly loss caps, consecutive-loss breaker,
concurrent-position limits.

All hard caps live in safety_guard.py; risk_manager only enforces config-soft
caps that are at or below those hard caps.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("polybot.risk")

STATE_PATH = "data/risk_state.json"


@dataclass
class RiskState:
    day_ymd: str = ""
    day_pnl_usd: float = 0.0
    week_year_week: str = ""
    week_pnl_usd: float = 0.0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    last_loss_at: float = 0.0
    auto_paused_until: float = 0.0
    pause_reason: str = ""

    @classmethod
    def load(cls) -> "RiskState":
        if not Path(STATE_PATH).exists():
            return cls()
        try:
            with open(STATE_PATH) as f:
                return cls(**json.load(f))
        except Exception:
            return cls()

    def save(self) -> None:
        Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.__dict__, f, indent=2)
        os.replace(tmp, STATE_PATH)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _this_iso_week() -> str:
    d = datetime.now(timezone.utc)
    yr, wk, _ = d.isocalendar()
    return f"{yr}-W{wk:02d}"


@dataclass
class SizingDecision:
    approved: bool
    size_contracts: float = 0.0
    size_usd: float = 0.0
    reason: str = ""


class RiskManager:
    def __init__(
        self,
        daily_loss_cap_usd: float,
        weekly_loss_cap_usd: float,
        consecutive_loss_limit: int,
        max_concurrent_positions: int,
        min_bankroll_usd: float,
        hard_max_bet_usd: float,
    ) -> None:
        self.daily_loss_cap_usd = daily_loss_cap_usd
        self.weekly_loss_cap_usd = weekly_loss_cap_usd
        self.consecutive_loss_limit = consecutive_loss_limit
        self.max_concurrent_positions = max_concurrent_positions
        self.min_bankroll_usd = min_bankroll_usd
        self.hard_max_bet_usd = hard_max_bet_usd
        self.state = RiskState.load()
        self._roll_if_needed()

    # ---- rollover ----------------------------------------------------
    def _roll_if_needed(self) -> None:
        today = _today_utc()
        if self.state.day_ymd != today:
            if self.state.day_ymd:
                log.info(
                    "day rollover %s → %s (prior day pnl: $%.2f)",
                    self.state.day_ymd, today, self.state.day_pnl_usd,
                )
            self.state.day_ymd = today
            self.state.day_pnl_usd = 0.0
        week = _this_iso_week()
        if self.state.week_year_week != week:
            if self.state.week_year_week:
                log.info(
                    "week rollover %s → %s (prior week pnl: $%.2f)",
                    self.state.week_year_week, week, self.state.week_pnl_usd,
                )
            self.state.week_year_week = week
            self.state.week_pnl_usd = 0.0
        self.state.save()

    # ---- public API --------------------------------------------------
    def check_pre_trade(
        self,
        bankroll_usd: float,
        open_positions: int,
        coin: str,
        already_open_on_coin: bool,
        seconds_left_in_window: float,
    ) -> SizingDecision:
        """Return approved=False with reason if this trade should be skipped."""
        self._roll_if_needed()

        # auto-pause
        if time.time() < self.state.auto_paused_until:
            return SizingDecision(False, reason=f"auto-paused ({self.state.pause_reason}) until {self.state.auto_paused_until:.0f}")

        # min bankroll
        if bankroll_usd < self.min_bankroll_usd:
            return SizingDecision(False, reason=f"bankroll ${bankroll_usd:.2f} below min ${self.min_bankroll_usd:.2f}")

        # daily cap
        if self.state.day_pnl_usd <= -self.daily_loss_cap_usd:
            self._auto_pause(seconds=max(60, int(_seconds_to_next_utc_midnight())), reason="daily_loss_cap")
            return SizingDecision(False, reason=f"daily loss cap hit ({self.state.day_pnl_usd:.2f})")

        # weekly cap
        if self.state.week_pnl_usd <= -self.weekly_loss_cap_usd:
            self._auto_pause(seconds=7 * 86400, reason="weekly_loss_cap")
            return SizingDecision(False, reason=f"weekly loss cap hit ({self.state.week_pnl_usd:.2f})")

        # consecutive losses
        if self.state.consecutive_losses >= self.consecutive_loss_limit:
            self._auto_pause(seconds=24 * 3600, reason="consecutive_losses")
            return SizingDecision(False, reason=f"consecutive loss breaker ({self.state.consecutive_losses})")

        # concurrent positions
        if open_positions >= self.max_concurrent_positions:
            return SizingDecision(False, reason=f"{open_positions} positions open (max {self.max_concurrent_positions})")

        # already-on-coin
        if already_open_on_coin:
            return SizingDecision(False, reason=f"already open on {coin}")

        # approved — sizing done separately via pick_bet_size
        return SizingDecision(True)

    def pick_bet_size(
        self,
        seconds_left_in_window: float,
        bet_usd_above_180: float,
        bet_usd_120_to_180: float,
        bet_usd_below_120: float,
    ) -> float:
        """Returns USD size to bet (will be converted to contracts by caller)."""
        if seconds_left_in_window > 180:
            size = bet_usd_above_180
        elif seconds_left_in_window > 120:
            size = bet_usd_120_to_180
        else:
            size = bet_usd_below_120
        # enforce hardcoded cap
        return min(size, self.hard_max_bet_usd)

    def on_position_closed(self, realized_usd: float) -> None:
        self._roll_if_needed()
        self.state.day_pnl_usd += realized_usd
        self.state.week_pnl_usd += realized_usd
        if realized_usd < 0:
            self.state.consecutive_losses += 1
            self.state.consecutive_wins = 0
            self.state.last_loss_at = time.time()
        else:
            self.state.consecutive_wins += 1
            self.state.consecutive_losses = 0
        self.state.save()

    def _auto_pause(self, seconds: int, reason: str) -> None:
        self.state.auto_paused_until = time.time() + seconds
        self.state.pause_reason = reason
        self.state.save()
        log.warning("auto-paused for %ds — reason: %s", seconds, reason)


def _seconds_to_next_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if tomorrow <= now:
        tomorrow = tomorrow.replace(day=tomorrow.day + 1)
    return int((tomorrow - now).total_seconds())
