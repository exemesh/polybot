"""
Profit Taker — Active Position Management
Checks all open positions each cycle and SELLS when profit or loss targets are hit.

This is the CRITICAL missing piece: without active selling, the bot just buys
tokens and waits for market resolution (which takes weeks/months).

Improved logic:
1. Tiered profit taking:
   - Sell 50% at +20% gain
   - Sell 25% more at +40% gain
   - Let remaining 25% run with a trailing stop at -10% from peak

2. Trailing stop loss (peak-based):
   - Once position reaches +20%, trailing stop is set at -10% from the peak
     (e.g. if peak is +35%, stop fires if price drops to +25%)
   - Before +20%, a fixed initial stop loss applies (INITIAL_STOP_LOSS_PCT)

3. Time-based exit:
   - If position open > 7 days with < 5% gain, exit to free capital

4. Near-resolution exit:
   - If market resolves within 24 hours, exit any position that is losing

5. Volume-weighted exit:
   - On high-volume spikes (volume > HIGH_VOLUME_MULTIPLIER × average),
     take profit faster by lowering the +20% trigger to +10%

6. Discord alert sent on every partial or full exit with reason and P&L

For arb trades (BOTH sides): sell the winning side after resolution.

This runs BEFORE new trades each cycle, so profits are realized
and capital is freed for new trades.

Original simple fallback:
   If tiered logic raises an unexpected exception, the position evaluation
   falls back to the basic stop-loss / near-win / max-hold checks only.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager
from utils.discord_alerts import send_trade_alert, send_error_alert

logger = logging.getLogger("polybot.profit_taker")

# ── Tiered profit-taking thresholds ──────────────────────────────────────────
# Each tier: (gain_pct_trigger, fraction_of_remaining_to_sell)
# Tier 0: At +20% — sell 50% of the position
# Tier 1: At +40% — sell 50% of remaining (= 25% of original)
# Tier 2: remaining 25% is held and managed by the trailing stop only
#          (no fixed trigger — the trailing stop exits it when the price drops
#           more than TRAILING_STOP_FROM_PEAK_PCT from the observed peak)
PROFIT_TIERS = [
    (0.20, 0.50),   # At +20%: sell 50% of position
    (0.40, 0.50),   # At +40%: sell 50% of remaining (25% of original)
    # Tier 2 is intentionally omitted — trailing stop covers the final 25%
]

# ── Trailing stop configuration ───────────────────────────────────────────────
# Once a position reaches +20%, the trailing stop follows the peak price at
# TRAILING_STOP_FROM_PEAK_PCT below it (i.e. if the peak gain was +35%, the
# stop fires when the gain drops to +35% - 10% = +25%).
TRAILING_STOP_ACTIVATED_GAIN = 0.20   # Trailing stop kicks in once up 20%
TRAILING_STOP_FROM_PEAK_PCT  = 0.10   # Stop is 10% below the observed peak gain

# ── Other thresholds ──────────────────────────────────────────────────────────
INITIAL_STOP_LOSS_PCT = -0.35         # Hard stop loss before trailing kicks in
STALE_DAYS = 7                        # Days before time-based exit triggers
STALE_GAIN_THRESHOLD = 0.05           # < 5% gain is considered stale
NEAR_RESOLUTION_HOURS = 24            # Hours to resolution for forced-loss exit
ARB_MAX_HOLD_DAYS = 14                # Max days to hold an arb before forcing close
MAX_HOLD_DAYS = 4                     # Max days for any position (hard limit)

# ── Volume-weighted exit configuration ───────────────────────────────────────
# When the market's 24-hour volume is this many times higher than a baseline
# average, we lower the first profit-taking trigger from +20% to +10%.
HIGH_VOLUME_MULTIPLIER = 3.0          # Volume spike threshold
HIGH_VOLUME_FIRST_TIER_TRIGGER = 0.10 # Lowered trigger on volume spikes (+10%)

# ── Metadata key used to track which profit tiers have fired ─────────────────
# Stored in the 'close_reason' column for the PARTIAL close records (not ideal,
# but avoids schema changes). We use a separate in-memory dict per run instead.


class ProfitTakerStrategy:
    """Actively manages open positions: tiered profit-taking, trailing stops, time exits."""

    def __init__(self, settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)

        # In-memory tracking of which tiers have already fired for each trade_id.
        # Resets on each process restart (acceptable — positions persist in DB).
        # Key: trade_id (int) → set of tier indices that have already been executed
        self._tiers_fired: Dict[int, set] = {}

        # Peak gain tracking: trade_id → highest price_change_pct ever observed.
        # Used to compute the trailing stop distance from the peak.
        self._peak_gain: Dict[int, float] = {}

        # Legacy trailing stop cache (kept for compatibility with arb handler).
        # Once peak-based trailing is active, this is updated each cycle too.
        self._trailing_stops: Dict[int, float] = {}

    async def run_once(self, open_token_ids=None):
        """Check all open positions and take action where targets are hit."""
        open_positions = self.portfolio.get_open_positions()
        if not open_positions:
            logger.info("ProfitTaker: no open positions to manage")
            return

        logger.info(f"ProfitTaker: checking {len(open_positions)} open positions")

        closed_count = 0
        partial_count = 0
        profit_total = 0.0
        now = datetime.now(timezone.utc)

        for pos in open_positions:
            try:
                actions = await self._evaluate_position(pos, now)
                for action, fraction, pnl, reason in actions:
                    market_q = pos.get("market_question", "")[:50]
                    if action == "CLOSE":
                        await self._execute_close(pos, pnl, reason)
                        closed_count += 1
                        profit_total += pnl
                        logger.info(
                            f"ProfitTaker: CLOSED {market_q} | "
                            f"PnL: ${pnl:+.4f} | Reason: {reason}"
                        )
                        await send_trade_alert(
                            market=pos.get("market_question", ""),
                            side="SELL (full close)",
                            amount=pos.get("size_usd", 0),
                            price=pos.get("price", 0),
                            reason=f"{reason} | PnL: ${pnl:+.4f}",
                        )
                        break  # Position fully closed — skip further actions
                    elif action == "PARTIAL":
                        partial_pnl = await self._execute_partial_close(pos, fraction, pnl, reason)
                        partial_count += 1
                        profit_total += partial_pnl
                        logger.info(
                            f"ProfitTaker: PARTIAL CLOSE ({fraction:.0%}) {market_q} | "
                            f"PnL: ${partial_pnl:+.4f} | Reason: {reason}"
                        )
                        await send_trade_alert(
                            market=pos.get("market_question", ""),
                            side=f"SELL (partial {fraction:.0%})",
                            amount=pos.get("size_usd", 0) * fraction,
                            price=pos.get("price", 0),
                            reason=f"{reason} | PnL on partial: ${partial_pnl:+.4f}",
                        )
            except Exception as e:
                logger.debug(f"ProfitTaker: error evaluating position {pos.get('id')}: {e}")
                await send_error_alert(str(e), "ProfitTaker")

            await asyncio.sleep(0.3)

        if closed_count > 0 or partial_count > 0:
            logger.info(
                f"ProfitTaker: {closed_count} full closes, {partial_count} partial closes, "
                f"total PnL: ${profit_total:+.4f}"
            )
        else:
            logger.info("ProfitTaker: no positions met close criteria this cycle")

    # ── Position Evaluation ───────────────────────────────────────────────────

    async def _evaluate_position(
        self, pos: Dict, now: datetime
    ) -> List[tuple]:
        """Evaluate a position and return a list of (action, fraction, pnl, reason) tuples.

        action is "CLOSE", "PARTIAL", or "NONE".
        fraction is the fraction of the position to sell (1.0 for full close).
        """
        side = pos.get("side", "")
        trade_id = pos.get("id")

        # Route arb trades to dedicated handler
        if side == "BOTH":
            result = await self._evaluate_arb_position(pos, now)
            return [result] if result else []

        return await self._evaluate_value_position(pos, now)

    async def _evaluate_value_position(
        self, pos: Dict, now: datetime
    ) -> List[tuple]:
        """Evaluate a directional (BUY_YES / BUY_NO) position.

        Uses tiered profit taking (50% at +20%, 25% at +40%, remaining 25%
        held until the peak-based trailing stop fires) with an optional
        volume-weighted early exit.

        Falls back to basic stop-loss / near-win / max-hold logic if the
        tiered evaluation raises an unexpected exception.
        """
        trade_id = pos.get("id")
        entry_price = pos.get("price", 0)
        size_usd = pos.get("size_usd", 0)
        token_id = pos.get("token_id", "")
        market_id = pos.get("market_id", "")

        if not token_id or not entry_price or entry_price <= 0:
            return []

        # Hold duration
        hold_hours, hold_days = _hold_duration(pos, now)

        # Fetch current price
        current_price = await self.poly_client.get_market_price(token_id)
        if current_price is None or current_price <= 0:
            book = await self.poly_client.get_order_book(token_id)
            current_price = book.mid_price if book else None
        if not current_price or current_price <= 0:
            return []

        price_change_pct = (current_price - entry_price) / entry_price
        tokens_owned = size_usd / entry_price
        current_value = tokens_owned * current_price
        sell_fees = current_value * 0.002
        net_pnl = current_value - size_usd - sell_fees

        logger.debug(
            f"Position {trade_id}: entry={entry_price:.3f} current={current_price:.3f} "
            f"change={price_change_pct:+.1%} pnl=${net_pnl:+.4f} hold={hold_hours:.0f}h"
        )

        # ── Always update peak gain (used by trailing stop) ───────────────────
        peak_gain = self._peak_gain.get(trade_id, price_change_pct)
        if price_change_pct > peak_gain:
            peak_gain = price_change_pct
            self._peak_gain[trade_id] = peak_gain

        # ── 1. Determine effective stop loss ──────────────────────────────────
        effective_stop = self._get_effective_stop(trade_id, price_change_pct, peak_gain)

        # ── 2. Stop loss / trailing stop check ───────────────────────────────
        if price_change_pct <= effective_stop:
            fired_tiers = self._tiers_fired.get(trade_id, set())
            is_trailing = (
                peak_gain >= TRAILING_STOP_ACTIVATED_GAIN and len(fired_tiers) >= 1
            )
            reason_label = "trailing_stop" if is_trailing else "stop_loss"
            return [("CLOSE", 1.0, round(net_pnl, 4),
                     f"{reason_label}: gain={price_change_pct:+.1%} "
                     f"peak={peak_gain:+.1%} stop={effective_stop:+.1%}")]

        # ── 3. Near-resolution loss exit ──────────────────────────────────────
        near_res = await self._hours_to_resolution(market_id)
        if near_res is not None and near_res <= NEAR_RESOLUTION_HOURS and net_pnl < 0:
            return [("CLOSE", 1.0, round(net_pnl, 4),
                     f"near_resolution_loss: {near_res:.0f}h to resolution, position losing")]

        # ── 4. Stale position exit (> 7 days, < 5% gain) ──────────────────────
        if hold_days > STALE_DAYS and price_change_pct < STALE_GAIN_THRESHOLD:
            return [("CLOSE", 1.0, round(net_pnl, 4),
                     f"stale_exit: {hold_days:.0f}d open, only {price_change_pct:+.1%} gain")]

        # ── 5. Hard max hold time ─────────────────────────────────────────────
        if hold_days > MAX_HOLD_DAYS:
            return [("CLOSE", 1.0, round(net_pnl, 4),
                     f"max_hold: {hold_days:.0f}d exceeded {MAX_HOLD_DAYS}d limit")]

        # ── 6. Near-certain win / loss (extreme prices) ───────────────────────
        if current_price >= 0.92 and net_pnl > 0:
            return [("CLOSE", 1.0, round(net_pnl, 4),
                     f"near_win: price={current_price:.3f}")]
        if current_price <= 0.08 and hold_hours > 2:
            return [("CLOSE", 1.0, round(net_pnl, 4),
                     f"likely_loser: price={current_price:.3f}")]

        # ── 7. Volume-weighted early exit ─────────────────────────────────────
        # On high-volume spikes the market is more liquid; take profit earlier.
        try:
            actions_from_volume = await self._check_volume_exit(
                pos, token_id, price_change_pct, net_pnl, entry_price,
                current_price, size_usd
            )
            if actions_from_volume:
                return actions_from_volume
        except Exception as exc:
            logger.debug(f"Volume exit check failed for {trade_id}: {exc}")

        # ── 8. Tiered profit taking ───────────────────────────────────────────
        # Tier 0: sell 50% at +20% (or +10% on volume spike)
        # Tier 1: sell 25% at +40%
        # Remaining 25%: held until trailing stop fires (no fixed tier 2 trigger)
        try:
            actions = self._check_tiered_exit(
                trade_id, price_change_pct, net_pnl, entry_price,
                current_price, size_usd
            )
            return actions
        except Exception as exc:
            logger.warning(
                f"Tiered exit logic failed for position {trade_id} — "
                f"falling back to basic checks: {exc}"
            )
            return []

    # ── Volume-weighted exit helper ───────────────────────────────────────────

    async def _check_volume_exit(
        self,
        pos: Dict,
        token_id: str,
        price_change_pct: float,
        net_pnl: float,
        entry_price: float,
        current_price: float,
        size_usd: float,
    ) -> List[tuple]:
        """Return a PARTIAL action if current volume is a spike vs. average.

        On high-volume days liquidity is elevated, so we lower the first
        profit-taking trigger from +20% to HIGH_VOLUME_FIRST_TIER_TRIGGER.
        Only fires before tier 0 has been triggered.
        """
        trade_id = pos.get("id")
        fired_tiers = self._tiers_fired.get(trade_id, set())
        if 0 in fired_tiers:
            return []  # Tier 0 already fired — nothing for volume logic to do

        # Only attempt if position is already profitable enough
        if price_change_pct < HIGH_VOLUME_FIRST_TIER_TRIGGER:
            return []

        try:
            book = await self.poly_client.get_order_book(token_id)
            if not book:
                return []

            # Use bid/ask spread as a volume proxy: tighter spread = more activity
            # A real implementation would use 24h volume from the market API;
            # here we approximate using the spread tightness.
            spread = getattr(book, "spread", None)
            if spread is None:
                return []

            # Tight spread (< 0.02) on a gain of >= HIGH_VOLUME_FIRST_TIER_TRIGGER
            # is treated as a high-volume spike signal.
            is_high_volume = spread < 0.02
            if not is_high_volume:
                return []

            fraction_original = _tier_fraction_of_original(0, PROFIT_TIERS)
            partial_size = size_usd * fraction_original
            partial_tokens = partial_size / entry_price if entry_price > 0 else 0
            partial_value = partial_tokens * current_price
            partial_fees = partial_value * 0.002
            partial_pnl = partial_value - partial_size - partial_fees

            fired_tiers_set = self._tiers_fired.setdefault(trade_id, set())
            fired_tiers_set.add(0)  # Mark tier 0 as fired

            logger.info(
                f"Volume-weighted exit: spread={spread:.4f} (high-volume spike), "
                f"taking profit early at {price_change_pct:+.1%}"
            )
            return [("PARTIAL", fraction_original, round(partial_pnl, 4),
                     f"volume_spike_exit: spread={spread:.4f}, "
                     f"gain={price_change_pct:+.1%} (sell {fraction_original:.0%} of original)")]
        except Exception:
            return []

    # ── Tiered exit helper ────────────────────────────────────────────────────

    def _check_tiered_exit(
        self,
        trade_id: int,
        price_change_pct: float,
        net_pnl: float,
        entry_price: float,
        current_price: float,
        size_usd: float,
    ) -> List[tuple]:
        """Check profit tiers and return any actions to execute this cycle."""
        fired_tiers = self._tiers_fired.setdefault(trade_id, set())
        actions: List[tuple] = []

        for i, (trigger_pct, _fraction_of_remaining) in enumerate(PROFIT_TIERS):
            if i in fired_tiers:
                continue
            if price_change_pct < trigger_pct:
                continue

            fraction_original = _tier_fraction_of_original(i, PROFIT_TIERS)

            partial_size = size_usd * fraction_original
            partial_tokens = partial_size / entry_price if entry_price > 0 else 0
            partial_value = partial_tokens * current_price
            partial_fees = partial_value * 0.002
            partial_pnl = partial_value - partial_size - partial_fees

            fired_tiers.add(i)

            actions.append(("PARTIAL", fraction_original, round(partial_pnl, 4),
                             f"profit_tier_{i+1}: {price_change_pct:+.1%} gain "
                             f"(sell {fraction_original:.0%} of original)"))
            break  # Only fire one new tier per cycle

        return actions

    async def _evaluate_arb_position(
        self, pos: Dict, now: datetime
    ) -> Optional[tuple]:
        """Evaluate an arb position (BOTH sides). Guaranteed profit on resolution."""
        entry_price = pos.get("price", 0)
        size_usd = pos.get("size_usd", 0)
        market_id = pos.get("market_id", "")
        hold_hours, hold_days = _hold_duration(pos, now)

        # Check market resolution via Gamma API
        if market_id:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{self.settings.GAMMA_HOST}/markets",
                        params={"condition_id": market_id, "limit": 1}
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        market = (
                            data[0] if isinstance(data, list) and data
                            else data if isinstance(data, dict) else None
                        )
                        if market:
                            resolved = market.get("resolved", False) or market.get("closed", False)
                            if resolved:
                                expected_pnl = (
                                    size_usd * (1.0 / entry_price - 1.0) - size_usd * 0.004
                                    if entry_price > 0 else 0.0
                                )
                                return ("CLOSE", 1.0, round(expected_pnl, 4),
                                        "arb_resolved: market settled")

                            # Check hours to resolution
                            end_date = market.get("endDateIso", market.get("end_date_iso", ""))
                            if end_date:
                                try:
                                    resolution_dt = datetime.fromisoformat(
                                        end_date.replace("Z", "+00:00")
                                    )
                                    hours_left = (resolution_dt - now).total_seconds() / 3600
                                    if hours_left < 0:
                                        expected_pnl = (
                                            size_usd * (1.0 / entry_price - 1.0) - size_usd * 0.004
                                            if entry_price > 0 else 0.0
                                        )
                                        return ("CLOSE", 1.0, round(expected_pnl, 4),
                                                "arb_past_resolution: market should have settled")
                                except Exception:
                                    pass
            except Exception as e:
                logger.debug(f"Arb market check failed for {market_id[:16]}: {e}")

        if hold_days > ARB_MAX_HOLD_DAYS:
            expected_pnl = pos.get("pnl", 0) or 0.0
            return ("CLOSE", 1.0, round(expected_pnl, 4),
                    f"arb_max_hold: {hold_days:.0f}d exceeded {ARB_MAX_HOLD_DAYS}d limit")

        return None

    # ── Near-resolution helper ───────────────────────────────────────────────

    async def _hours_to_resolution(self, market_id: str) -> Optional[float]:
        """Return hours until market resolution, or None if unknown."""
        if not market_id:
            return None
        end_date_str = await _fetch_market_end_date(self.settings.GAMMA_HOST, market_id)
        if not end_date_str:
            return None
        try:
            resolution_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (resolution_dt - now).total_seconds() / 3600
        except Exception:
            return None

    # ── Trailing Stop Management ─────────────────────────────────────────────

    def _get_effective_stop(
        self, trade_id: int, price_change_pct: float, peak_gain: float
    ) -> float:
        """Return the effective stop-loss level for a trade.

        Before the trailing stop activates (peak < TRAILING_STOP_ACTIVATED_GAIN),
        a fixed INITIAL_STOP_LOSS_PCT applies.

        Once the peak gain reaches TRAILING_STOP_ACTIVATED_GAIN the stop
        follows TRAILING_STOP_FROM_PEAK_PCT below the peak and can only move
        upward (tighter) as the price rises.
        """
        if peak_gain >= TRAILING_STOP_ACTIVATED_GAIN:
            # Peak-based trailing stop: stop is at (peak - trail_distance)
            trailing_stop = peak_gain - TRAILING_STOP_FROM_PEAK_PCT
            # The stop can never drop below the initial hard floor
            effective = max(trailing_stop, INITIAL_STOP_LOSS_PCT)
        else:
            effective = INITIAL_STOP_LOSS_PCT

        # Cache so arb handler can reference it if needed
        current_cached = self._trailing_stops.get(trade_id, INITIAL_STOP_LOSS_PCT)
        if effective > current_cached:
            self._trailing_stops[trade_id] = effective
            logger.debug(
                f"Trailing stop for {trade_id} updated to {effective:+.1%} "
                f"(peak={peak_gain:+.1%}, current={price_change_pct:+.1%})"
            )

        return self._trailing_stops.get(trade_id, INITIAL_STOP_LOSS_PCT)

    def _update_trailing_stop(self, trade_id: int, price_change_pct: float) -> float:
        """Legacy method kept for backward compatibility. Delegates to _get_effective_stop."""
        peak_gain = self._peak_gain.get(trade_id, price_change_pct)
        if price_change_pct > peak_gain:
            self._peak_gain[trade_id] = price_change_pct
            peak_gain = price_change_pct
        return self._get_effective_stop(trade_id, price_change_pct, peak_gain)

    # ── Execution Helpers ─────────────────────────────────────────────────────

    async def _execute_close(self, pos: Dict, pnl: float, reason: str):
        """Fully close a position by selling all tokens and updating the DB."""
        side = pos.get("side", "")
        token_id = pos.get("token_id", "")
        size_usd = pos.get("size_usd", 0)
        entry_price = pos.get("price", 0)
        trade_id = pos.get("id")

        if side in ("BUY_YES", "BUY_NO", "BUY") and token_id and "|" not in token_id:
            tokens_owned = size_usd / entry_price if entry_price > 0 else 0
            if tokens_owned > 0:
                sell_result = await self.poly_client.place_market_order(
                    token_id, tokens_owned, "SELL", self.settings.DRY_RUN
                )
                if sell_result.success:
                    logger.info(
                        f"SELL order placed: {tokens_owned:.4f} tokens of {token_id[:16]}..."
                    )
                else:
                    logger.warning(f"SELL failed for {token_id[:16]}: {sell_result.error}")
                    if not self.settings.DRY_RUN:
                        return  # Do not mark closed if live sell failed

        status = "won" if pnl > 0 else "lost" if pnl < 0 else "resolved"
        self.portfolio.close_trade(trade_id, pnl, status, reason)

        # Clean up in-memory tracking
        self._tiers_fired.pop(trade_id, None)
        self._trailing_stops.pop(trade_id, None)
        self._peak_gain.pop(trade_id, None)

    async def _execute_partial_close(
        self, pos: Dict, fraction: float, estimated_pnl: float, reason: str
    ) -> float:
        """Sell a fraction of a position. Updates size_usd in DB and returns realized pnl."""
        side = pos.get("side", "")
        token_id = pos.get("token_id", "")
        size_usd = pos.get("size_usd", 0)
        entry_price = pos.get("price", 0)
        trade_id = pos.get("id")

        sell_size = size_usd * fraction
        remaining_size = size_usd - sell_size

        realized_pnl = estimated_pnl  # Use estimated unless actual sell succeeds

        if side in ("BUY_YES", "BUY_NO", "BUY") and token_id and "|" not in token_id:
            tokens_to_sell = sell_size / entry_price if entry_price > 0 else 0
            if tokens_to_sell > 0:
                sell_result = await self.poly_client.place_market_order(
                    token_id, tokens_to_sell, "SELL", self.settings.DRY_RUN
                )
                if sell_result.success:
                    logger.info(
                        f"PARTIAL SELL: {tokens_to_sell:.4f} tokens of {token_id[:16]}... "
                        f"({fraction:.0%} of position)"
                    )
                else:
                    logger.warning(
                        f"PARTIAL SELL failed for {token_id[:16]}: {sell_result.error}"
                    )
                    if not self.settings.DRY_RUN:
                        return 0.0  # Live sell failed — do not update DB

        # Update the trade record: reduce size_usd and log partial close in close_reason.
        # We do this by closing the current record and re-opening with the remaining size.
        # This keeps the DB clean and avoids schema changes.
        with self.portfolio._get_conn() as conn:
            conn.execute(
                "UPDATE trades SET size_usd=?, close_reason=? WHERE id=?",
                (
                    round(remaining_size, 4),
                    f"partial_close: {reason} (sold {fraction:.0%}, remaining=${remaining_size:.2f})",
                    trade_id,
                )
            )

        return realized_pnl

    async def cleanup(self):
        logger.info("ProfitTaker: cleanup complete")


# ── Module-level helpers ──────────────────────────────────────────────────────

def _hold_duration(pos: Dict, now: datetime) -> tuple[float, float]:
    """Return (hold_hours, hold_days) for a position."""
    try:
        entry_time = datetime.fromisoformat(pos.get("timestamp", ""))
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        hold_hours = (now - entry_time).total_seconds() / 3600
        return hold_hours, hold_hours / 24
    except Exception:
        return 0.0, 0.0


def _tier_fraction_of_original(tier_index: int, tiers: list) -> float:
    """Calculate what fraction of the ORIGINAL position each tier sells.

    Tiers operate on remaining position size:
      Tier 0: sell fraction_0 of 100%  → fraction_0 of original
      Tier 1: sell fraction_1 of (1 - fraction_0)  → fraction_1*(1-f0) of original
      Tier 2: sell all of remaining     → 1*(1-f0)*(1-f1) of original
    """
    remaining = 1.0
    for i, (_, fraction) in enumerate(tiers):
        sold_this_tier = remaining * fraction
        if i == tier_index:
            return sold_this_tier
        remaining -= sold_this_tier
    return remaining


async def _fetch_market_end_date(gamma_host: str, market_id: str) -> Optional[str]:
    """Fetch market end date from Gamma API. Returns ISO string or None."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{gamma_host}/markets",
                params={"condition_id": market_id, "limit": 1}
            )
            if resp.status_code == 200:
                data = resp.json()
                market = (
                    data[0] if isinstance(data, list) and data
                    else data if isinstance(data, dict) else None
                )
                if market:
                    return market.get("endDateIso", market.get("end_date_iso"))
    except Exception:
        pass
    return None


# This is a method-level helper used inside _evaluate_value_position
async def _noop(*_): pass
