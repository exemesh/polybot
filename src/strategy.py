"""Late Entry V3 — signal logic.

The only strategy in this bot. All 5 conditions must pass for entry.
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("polybot.strategy")


@dataclass
class Signal:
    fire: bool
    coin: str
    side: str                 # "UP" or "DOWN"
    token_id: str
    opposite_token_id: str
    entry_price: float        # the ask we'd take
    favorite_price: float
    underdog_price: float
    seconds_left: float
    seconds_elapsed: float
    binance_momentum_pct: Optional[float]
    vwap_deviation_pct: Optional[float]
    reason: str               # if fire=False, why not


def _compute_vwap(samples, lookback_sec: int) -> Optional[float]:
    """VWAP over the last lookback_sec seconds. None if insufficient data."""
    if not samples:
        return None
    cutoff = time.time() - lookback_sec
    filtered = [s for s in samples if s.ts >= cutoff]
    if len(filtered) < 5:
        return None
    num = sum(s.close * s.volume for s in filtered)
    denom = sum(s.volume for s in filtered)
    if denom <= 0:
        # fall back to unweighted mean
        return statistics.fmean(s.close for s in filtered)
    return num / denom


class LateEntryV3:
    def __init__(
        self,
        min_elapsed_sec: int,
        max_time_left_sec: int,
        min_entry_price: float,
        max_entry_price: float,
        min_favorite_gap_pct: float,
        min_vwap_deviation_pct: float,
        require_positive_momentum: bool,
        momentum_lookback_sec: int,
        max_spread_cents: int,
        max_book_staleness_sec: float,
        min_book_depth_usd: float,
    ) -> None:
        self.min_elapsed_sec = min_elapsed_sec
        self.max_time_left_sec = max_time_left_sec
        self.min_entry_price = min_entry_price
        self.max_entry_price = max_entry_price
        self.min_favorite_gap_pct = min_favorite_gap_pct
        self.min_vwap_deviation_pct = min_vwap_deviation_pct
        self.require_positive_momentum = require_positive_momentum
        self.momentum_lookback_sec = momentum_lookback_sec
        self.max_spread_cents = max_spread_cents
        self.max_book_staleness_sec = max_book_staleness_sec
        self.min_book_depth_usd = min_book_depth_usd

    def evaluate(
        self,
        coin: str,
        market,                 # ActiveMarket
        book_up,                # OrderBook
        book_down,              # OrderBook
        binance_state,          # CoinState
    ) -> Signal:
        # --- sentinel signal used on every reject ---
        def reject(reason: str) -> Signal:
            return Signal(
                fire=False, coin=coin, side="", token_id="", opposite_token_id="",
                entry_price=0.0, favorite_price=0.0, underdog_price=0.0,
                seconds_left=market.seconds_left, seconds_elapsed=market.seconds_elapsed,
                binance_momentum_pct=None, vwap_deviation_pct=None, reason=reason,
            )

        # --- time window ---
        if market.seconds_elapsed < self.min_elapsed_sec:
            return reject(f"elapsed={market.seconds_elapsed:.0f}s < {self.min_elapsed_sec}s")
        if market.seconds_left > self.max_time_left_sec:
            return reject(f"left={market.seconds_left:.0f}s > {self.max_time_left_sec}s")
        if market.seconds_left <= 0:
            return reject("window closed")

        # --- book freshness + both sides priced ---
        if not book_up or not book_down:
            return reject("missing book")
        if not book_up.is_fresh(self.max_book_staleness_sec):
            return reject(f"UP book stale ({time.time() - book_up.last_update:.1f}s)")
        if not book_down.is_fresh(self.max_book_staleness_sec):
            return reject(f"DOWN book stale ({time.time() - book_down.last_update:.1f}s)")

        ask_up = book_up.best_ask
        ask_down = book_down.best_ask
        if ask_up is None or ask_down is None:
            return reject("no ask on one side")

        # --- identify favorite ---
        if ask_up >= ask_down:
            fav_side = "UP"
            fav_ask = ask_up
            underdog_ask = ask_down
            fav_book = book_up
            token_id = market.token_id_up
            opp_token_id = market.token_id_down
        else:
            fav_side = "DOWN"
            fav_ask = ask_down
            underdog_ask = ask_up
            fav_book = book_down
            token_id = market.token_id_down
            opp_token_id = market.token_id_up

        # --- price in edge zone ---
        if fav_ask < self.min_entry_price:
            return reject(f"favorite ask {fav_ask:.3f} < {self.min_entry_price:.3f}")
        if fav_ask > self.max_entry_price:
            return reject(f"favorite ask {fav_ask:.3f} > {self.max_entry_price:.3f} (break-even wall)")

        # --- favorite gap ---
        gap_pct = (fav_ask - underdog_ask) / max(underdog_ask, 0.001) * 100.0
        if gap_pct < self.min_favorite_gap_pct:
            return reject(f"fav gap {gap_pct:.1f}% < {self.min_favorite_gap_pct:.1f}%")

        # --- spread ---
        spread = fav_book.spread_cents
        if spread is None or spread > self.max_spread_cents:
            return reject(f"spread {spread} > {self.max_spread_cents}c")

        # --- book depth ---
        if fav_book.depth_usd_at_ask(n_levels=3) < self.min_book_depth_usd:
            return reject(f"fav book depth < ${self.min_book_depth_usd:.0f}")

        # --- Binance momentum ---
        momentum = binance_state.momentum_pct(self.momentum_lookback_sec)
        if self.require_positive_momentum:
            if momentum is None:
                return reject("momentum unavailable")
            if (fav_side == "UP" and momentum <= 0) or (fav_side == "DOWN" and momentum >= 0):
                return reject(f"momentum {momentum:+.3f}% contradicts favorite {fav_side}")

        # --- VWAP deviation of the window ---
        vwap = _compute_vwap(binance_state.samples, lookback_sec=900)
        last_price = binance_state.last_price
        if vwap is None or last_price is None:
            return reject("insufficient Binance history for VWAP")
        deviation = (last_price - vwap) / vwap * 100.0
        # deviation sign must match favorite direction
        dev_abs = abs(deviation)
        if dev_abs < self.min_vwap_deviation_pct:
            return reject(f"|VWAP dev| {dev_abs:.2f}% < {self.min_vwap_deviation_pct:.2f}%")
        if (fav_side == "UP" and deviation <= 0) or (fav_side == "DOWN" and deviation >= 0):
            return reject(f"VWAP dev {deviation:+.2f}% contradicts favorite {fav_side}")

        # --- all gates passed ---
        return Signal(
            fire=True, coin=coin, side=fav_side, token_id=token_id,
            opposite_token_id=opp_token_id, entry_price=fav_ask,
            favorite_price=fav_ask, underdog_price=underdog_ask,
            seconds_left=market.seconds_left, seconds_elapsed=market.seconds_elapsed,
            binance_momentum_pct=momentum, vwap_deviation_pct=deviation,
            reason="all_gates_passed",
        )
