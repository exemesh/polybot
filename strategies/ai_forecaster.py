"""
AI Superforecaster Strategy — LONGSHOT HUNTER MODE
Adapted from Polymarket/agents official framework.

Uses OpenAI LLM as a "superforecaster" to find HIGH-PAYOUT longshot bets.
Targets: $10 bets that can pay $50+ (5x+ returns).
Timelines up to 3 months are fine — profitability over speed.

Flow:
1. Fetch top markets from Gamma API (up to 90-day window)
2. For each candidate, ask the LLM to estimate P(YES)
3. Compare LLM probability to current market price
4. Trade when AI finds underpriced longshots with 5x+ payout potential
5. Max 10 LLM calls per cycle, max 5 trades

Trade rules:
- $10 USD per AI-driven longshot bet
- Minimum 8% edge (LLM vs market price)
- 90-day max timeline (3 months)
- Focus on tokens priced $0.03-$0.25 for max payout multiplier
- Requires OPENAI_API_KEY — gracefully skips if not set
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional

from core.ai_client import AIClient
from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager

logger = logging.getLogger("polybot.ai_forecaster")

# ─── Strategy Constants — LONGSHOT HUNTER ────────────────────────────
MAX_LLM_CALLS_PER_CYCLE = 10      # More LLM calls to find the best longshots
MAX_TRADES_PER_CYCLE = 5          # Up to 5 trades per run
MIN_EDGE_PCT = 0.05               # 5% edge — low bar for longshots (5x+ payout compensates)
TRADE_SIZE_USD = 10.00            # $10 per bet — targeting $50+ payouts
MIN_LIQUIDITY_USD = 25.0          # Lower liq threshold for longshot markets
MIN_HOURS_TO_RESOLUTION = 4       # At least 4 hours out
MAX_HOURS_TO_RESOLUTION = 2160    # 90-day (3 month) max timeline
PRICE_RANGE = (0.02, 0.98)        # Ultra-wide — catch $0.02 longshots (50x payout)
# Longshot sweet spot: tokens at $0.03-$0.25 give 4x-33x payout on $10


class AIForecasterStrategy:
    """Uses LLM superforecaster to identify mispriced prediction markets."""

    def __init__(self, settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)
        self.ai_client = AIClient(
            api_key=getattr(settings, 'OPENAI_API_KEY', ''),
            model=getattr(settings, 'AI_MODEL', 'gpt-4o-mini'),
        )
        self.traded_markets: Dict[str, float] = {}  # condition_id -> last_trade_time

    async def run_once(self, open_token_ids=None):
        """Single AI forecasting cycle."""
        if not self.ai_client.enabled:
            logger.info("AIForecaster: SKIPPED (no OPENAI_API_KEY)")
            return

        logger.info("AIForecaster: scanning markets with AI superforecaster")

        try:
            # Step 1: Get candidate markets
            candidates = await self._get_candidate_markets()
            logger.info(f"AIForecaster: {len(candidates)} candidate markets for AI analysis")

            if not candidates:
                logger.info("AIForecaster: no suitable candidates this cycle")
                return

            # Step 2: Run AI analysis on top candidates
            trades_executed = 0
            llm_calls = 0
            markets_checked = 0
            skipped_no_book = 0
            skipped_low_liq = 0
            skipped_price_range = 0

            for market in candidates:
                if llm_calls >= MAX_LLM_CALLS_PER_CYCLE:
                    logger.info(f"AIForecaster: hit LLM call limit ({MAX_LLM_CALLS_PER_CYCLE})")
                    break
                if trades_executed >= MAX_TRADES_PER_CYCLE:
                    logger.info(f"AIForecaster: hit trade limit ({MAX_TRADES_PER_CYCLE})")
                    break

                markets_checked += 1
                result, reason = await self._analyze_and_trade(market)

                if reason == "llm_called":
                    llm_calls += 1
                elif reason == "no_book":
                    skipped_no_book += 1
                elif reason == "low_liq":
                    skipped_low_liq += 1
                elif reason == "price_range":
                    skipped_price_range += 1

                if result:
                    trades_executed += 1

                # Rate limit between LLM calls
                await asyncio.sleep(0.5)

            logger.info(
                f"AIForecaster complete: {markets_checked} checked, {llm_calls} LLM calls, "
                f"{trades_executed} trades | skipped: no_book={skipped_no_book}, "
                f"low_liq={skipped_low_liq}, price_range={skipped_price_range}"
            )

        except Exception as e:
            logger.error(f"AIForecaster error: {e}", exc_info=True)

    async def _get_candidate_markets(self) -> List[Dict]:
        """
        Fetch and filter markets for AI analysis.
        AGGRESSIVE: Accept markets with or without end dates.
        Real price/liquidity checks happen later via order books.
        """
        markets = await self.poly_client.get_markets(active_only=True)
        now = datetime.now(timezone.utc)
        candidates = []
        skipped_no_tokens = 0
        skipped_too_close = 0
        skipped_too_far = 0
        skipped_no_question = 0
        skipped_position = 0

        for market in markets[:500]:  # Scan all 500
            condition_id = market.get("condition_id", "")

            # Skip markets we already have positions in
            if self.portfolio.has_open_position(condition_id):
                skipped_position += 1
                continue

            # Skip recently traded (4-hour cooldown for AI trades)
            if condition_id in self.traded_markets:
                if time.time() - self.traded_markets[condition_id] < 14400:
                    continue

            # Must have YES/NO tokens
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                skipped_no_tokens += 1
                continue

            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)
            if not yes_token or not no_token:
                skipped_no_tokens += 1
                continue

            # Check resolution timeline if available — 30-day max
            # But ALLOW markets without end dates (many Polymarket markets lack this)
            end_date = market.get("end_date_iso", "")
            hours_until = None
            if end_date:
                try:
                    resolution_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    hours_until = (resolution_dt - now).total_seconds() / 3600
                    if hours_until < MIN_HOURS_TO_RESOLUTION:
                        skipped_too_close += 1
                        continue
                    if hours_until > MAX_HOURS_TO_RESOLUTION:
                        skipped_too_far += 1
                        continue
                except Exception:
                    pass  # Treat as unknown timeline

            # Must have a real question (not empty)
            question = market.get("question", "")
            if not question or len(question) < 10:
                skipped_no_question += 1
                continue

            # Get prices from token metadata (may be 0 — real prices come from order book later)
            yes_price = float(yes_token.get("price", 0) or 0)
            no_price = float(no_token.get("price", 0) or 0)

            candidates.append({
                "condition_id": condition_id,
                "question": question,
                "description": market.get("description", ""),
                "yes_token_id": yes_token.get("token_id"),
                "no_token_id": no_token.get("token_id"),
                "yes_price": yes_price,
                "no_price": no_price,
                "hours_until": hours_until,
                "volume": float(market.get("volume", 0) or 0),
                "liquidity": float(market.get("liquidity", 0) or 0),
            })

        logger.info(
            f"AIForecaster filter stats: no_tokens={skipped_no_tokens}, "
            f"too_close={skipped_too_close}, too_far={skipped_too_far}, "
            f"no_question={skipped_no_question}, has_position={skipped_position}"
        )

        # Sort to prioritize LONGSHOT opportunities:
        # 1. Markets with mid-range prices (0.05-0.30) — highest payout multipliers
        # 2. Higher volume = more interesting/liquid
        # 3. Known resolution date gets a small boost
        def sort_key(m):
            vol = max(m["volume"], 1)
            yes_p = m["yes_price"]
            # Longshot bonus: tokens at $0.05-$0.25 get priority (5x-20x payout)
            if 0.05 <= yes_p <= 0.25:
                longshot_bonus = 10.0  # Strong priority for high-payout range
            elif 0.25 < yes_p <= 0.50:
                longshot_bonus = 3.0   # Medium priority
            elif yes_p < 0.05 and yes_p > 0:
                longshot_bonus = 5.0   # Ultra-longshot (but might be too unlikely)
            else:
                longshot_bonus = 1.0   # Regular markets
            time_boost = 1.5 if m["hours_until"] else 1.0
            return vol * longshot_bonus * time_boost

        candidates.sort(key=sort_key, reverse=True)

        return candidates[:30]  # Top 30 candidates for AI analysis

    async def _analyze_and_trade(self, market: Dict):
        """
        Run AI superforecaster on a single market and trade if edge found.
        Returns (True/False, reason_string).
        """
        question = market["question"]
        condition_id = market["condition_id"]

        # Step 1: Get order book for real liquidity check
        yes_book = await self.poly_client.get_order_book(market["yes_token_id"])
        no_book = await self.poly_client.get_order_book(market["no_token_id"])

        if not yes_book or not no_book:
            logger.debug(f"No order book: {question[:50]}")
            return False, "no_book"

        min_liq = min(yes_book.liquidity_usd, no_book.liquidity_usd)
        if min_liq < MIN_LIQUIDITY_USD:
            logger.debug(f"Low liquidity (${min_liq:.0f}): {question[:50]}")
            return False, "low_liq"

        yes_mid = yes_book.mid_price
        no_mid = no_book.mid_price

        # Price range check — at least ONE side must be in tradeable range
        # If YES=$0.97 (favorite), NO=$0.03 (longshot) — NO side is the opportunity!
        yes_in_range = PRICE_RANGE[0] <= yes_mid <= PRICE_RANGE[1]
        no_in_range = PRICE_RANGE[0] <= no_mid <= PRICE_RANGE[1]
        if not yes_in_range and not no_in_range:
            logger.debug(f"Both prices out of range (YES={yes_mid:.3f}, NO={no_mid:.3f}): {question[:50]}")
            return False, "price_range"

        # Step 2: Ask AI for probability estimate
        ai_prob = await self.ai_client.get_probability(
            question=question,
            description=market.get("description", ""),
            current_yes_price=yes_mid,
            current_no_price=no_mid,
            hours_until_resolution=market["hours_until"],
        )

        if ai_prob is None:
            logger.warning(f"AI returned no probability for: {question[:60]}")
            return False, "llm_called"

        # Step 3: Calculate edge
        yes_edge = ai_prob - yes_mid        # Positive = AI thinks YES is underpriced
        no_edge = (1 - ai_prob) - no_mid    # Positive = AI thinks NO is underpriced

        best_edge = max(abs(yes_edge), abs(no_edge))
        trade_side = "BUY_YES" if yes_edge > no_edge else "BUY_NO"
        edge = yes_edge if trade_side == "BUY_YES" else no_edge

        logger.info(
            f"AI analysis: '{question[:55]}' | "
            f"AI P(YES)={ai_prob:.2f} vs Market={yes_mid:.3f} | "
            f"Edge: {edge*100:.1f}% ({trade_side})"
        )

        # Step 4: Trade if edge is significant
        if edge < MIN_EDGE_PCT:
            logger.info(f"AI: edge too small ({edge*100:.1f}%) for '{question[:50]}' — skip")
            return False, "llm_called"

        # Step 5: Execute trade
        traded = await self._execute_trade(market, trade_side, edge, ai_prob, yes_mid, no_mid)
        return traded, "llm_called"

    async def _execute_trade(
        self,
        market: Dict,
        side: str,
        edge: float,
        ai_prob: float,
        yes_price: float,
        no_price: float,
    ) -> bool:
        """Execute an AI-driven value trade. Adaptive sizing: $10 ideal, min $3."""
        trade_size = TRADE_SIZE_USD  # $10 ideal
        condition_id = market["condition_id"]

        # Adaptive sizing: try $10, fall back to $5, then $3
        approved, reason = self.risk_manager.approve_trade(
            trade_size, "ai_forecaster", condition_id
        )
        if not approved and trade_size > 5.0:
            trade_size = 5.00
            approved, reason = self.risk_manager.approve_trade(
                trade_size, "ai_forecaster", condition_id
            )
        if not approved and trade_size > 3.0:
            trade_size = 3.00
            approved, reason = self.risk_manager.approve_trade(
                trade_size, "ai_forecaster", condition_id
            )
        if not approved:
            logger.info(f"AI trade rejected (even at ${trade_size:.0f}): {reason}")
            return False

        token_id = market["yes_token_id"] if side == "BUY_YES" else market["no_token_id"]
        price = yes_price if side == "BUY_YES" else no_price

        hrs = market.get('hours_until')
        hrs_str = f"{hrs:.0f}h" if hrs else "unknown"
        payout = trade_size / price if price > 0 else 0
        logger.info(
            f"[AI FORECASTER] {side} | {market['question'][:55]} | "
            f"@ ${price:.3f} | AI P(YES)={ai_prob:.2f} | "
            f"Edge: {edge*100:.1f}% | ${trade_size:.0f} → ${payout:.0f} potential | "
            f"Closes: {hrs_str}"
        )

        result = await self.poly_client.place_market_order(
            token_id, trade_size, "BUY", self.settings.DRY_RUN
        )

        if result.success:
            trade = Trade(
                id=None,
                timestamp=datetime.utcnow().isoformat(),
                strategy="ai_forecaster",
                market_id=condition_id,
                market_question=market["question"],
                side=side,
                token_id=token_id,
                price=price,
                size_usd=trade_size,
                edge_pct=edge,
                dry_run=self.settings.DRY_RUN,
                order_id=result.order_id,
                pnl=None,
                status="open",
            )
            self.portfolio.log_trade(trade)
            self.traded_markets[condition_id] = time.time()

            logger.info(
                f"AI trade executed! {side} @ ${price:.3f} | "
                f"Edge: {edge*100:.1f}% | ${trade_size:.0f} → ${payout:.0f} payout | "
                f"Resolves: {hrs_str}"
            )
            return True

        logger.warning(f"AI trade failed for: {market['question'][:50]}")
        return False

    async def cleanup(self):
        logger.info(f"AIForecaster cleanup: {len(self.traded_markets)} markets analyzed")
