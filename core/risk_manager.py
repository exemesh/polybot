"""
Risk Manager - enforces position limits, loss limits, and Kelly sizing.
All trade sizes must be approved through this module.
"""

import logging
from datetime import datetime, date, timezone

logger = logging.getLogger("polybot.risk")


class RiskManager:
    def __init__(self, settings, portfolio):
        self.settings = settings
        self.portfolio = portfolio
        self._trading_halted = False
        self._halt_reason = None
        self._last_reset_date: date = date.today()

    # ─── Trade Sizing ─────────────────────────────────────────────────────────

    def kelly_size(self, edge: float, odds: float, bankroll: float) -> float:
        """
        Calculate Kelly Criterion bet size.
        edge: probability edge (e.g., 0.20 = 20% edge)
        odds: net odds on a win (e.g., for a 0.30 price, win gives 0.70/0.30 = 2.33)
        """
        if odds <= 0 or edge <= 0:
            return 0.0
        kelly_pct = edge / odds
        fractional_kelly = kelly_pct * self.settings.KELLY_FRACTION  # Quarter-Kelly
        raw_size = bankroll * fractional_kelly

        # Apply absolute caps
        max_by_pct = bankroll * self.settings.MAX_POSITION_PCT
        final_size = min(raw_size, max_by_pct, self.settings.WEATHER_MAX_BET_USD * 2)
        return round(max(final_size, 0.50), 2)  # $0.50 minimum

    def approve_trade(self, size_usd: float, strategy: str, market_id: str = "") -> tuple[bool, str]:
        """
        Returns (approved: bool, reason: str).
        All trades must pass this check before execution.
        """
        # Hard kill switch
        if self._trading_halted:
            return False, f"Trading halted: {self._halt_reason}"

        portfolio_value = self.portfolio.get_portfolio_value()
        deployed = self.portfolio.get_deployed_capital()
        daily_pnl = self.portfolio.get_daily_pnl()

        # 1. Daily loss limit
        loss_limit = portfolio_value * self.settings.DAILY_LOSS_LIMIT_PCT
        if daily_pnl < -loss_limit:
            self._halt_trading(f"Daily loss limit hit: ${daily_pnl:.2f} (limit: -${loss_limit:.2f})")
            return False, self._halt_reason

        # 2. Global exposure limit
        max_deployed = portfolio_value * self.settings.MAX_GLOBAL_EXPOSURE_PCT
        if deployed + size_usd > max_deployed:
            return False, f"Exposure limit: ${deployed:.2f} + ${size_usd:.2f} > ${max_deployed:.2f} max"

        # 3. Per-trade size limit
        max_per_trade = portfolio_value * self.settings.MAX_POSITION_PCT
        if size_usd > max_per_trade:
            return False, f"Position too large: ${size_usd:.2f} > ${max_per_trade:.2f} max"

        # 4. Minimum portfolio to trade
        if portfolio_value < 10.0:
            return False, f"Portfolio too small to trade: ${portfolio_value:.2f}"

        # 5. Sanity check on size
        if size_usd < 0.50:
            return False, f"Trade too small: ${size_usd:.2f}"

        logger.debug(f"Trade approved: ${size_usd:.2f} {strategy} | Portfolio: ${portfolio_value:.2f}")
        return True, "approved"

    def _halt_trading(self, reason: str):
        if not self._trading_halted:
            self._trading_halted = True
            self._halt_reason = reason
            logger.critical(f"🛑 TRADING HALTED: {reason}")

    def resume_trading(self):
        """Manually resume trading (call at start of new day)."""
        self._trading_halted = False
        self._halt_reason = None
        logger.info("Trading resumed")

    def is_halted(self) -> bool:
        return self._trading_halted

    def check_daily_reset(self):
        """Called each cycle to check if a new calendar day should reset the daily-loss halt.

        Compares the date of the last reset against today's UTC date so that
        a reset happens on *any* run that occurs on a new calendar day,
        regardless of what hour the bot runs.
        """
        today = datetime.now(timezone.utc).date()
        if self._last_reset_date < today:
            self._last_reset_date = today
            if self._trading_halted and "Daily loss" in (self._halt_reason or ""):
                self.resume_trading()
                logger.info(f"Daily reset: trading resumed for new UTC day ({today})")
