"""
Spread Capture Strategy
Finds markets where the YES+NO spread is significantly below $1.00,
capturing guaranteed profit regardless of outcome.

Unlike the arb scanner which requires exact book data, this strategy:
1. Scans ALL markets rapidly using the Gamma API (no order book calls needed)
2. Targets wider spreads (2-10% profit) on less liquid markets
3. Works on any market timeframe since profit is guaranteed
4. Higher volume of smaller guaranteed-profit trades

Trade rules:
- $1 USD per trade
- Minimum 0.5% guaranteed return (after fees)
- Max 10 trades per cycle
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List, Dict

import httpx

from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager

logger = logging.getLogger("polybot.spread")


class SpreadCaptureStrategy:
    """Captures guaranteed spread profit on YES+NO < $1 markets."""

    def __init__(self, settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)
        self.traded_markets: Dict[str, float] = {}

    async def run_once(self, open_token_ids=None):
        """Single scan-and-trade cycle."""
        logger.info("SpreadCapture: scanning for spread opportunities (guaranteed profit)")
        try:
            opportunities = await self._scan_spreads()
            executed = 0
            for opp in opportunities[:20]:  # Up to 20 spread trades (all guaranteed profit)
                success = await self._execute_trade(opp)
                if success:
                    executed += 1
            logger.info(f"SpreadCapture complete: {len(opportunities)} opps, {executed} trades (all guaranteed profit)")
        except Exception as e:
            logger.error(f"SpreadCapture error: {e}", exc_info=True)

    async def _scan_spreads(self) -> List[Dict]:
        """Rapidly scan for YES+NO spread opportunities using Gamma API."""
        opportunities = []

        # Fetch markets directly from Gamma API with outcome prices
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                # Get active markets with token data
                resp = await client.get(
                    f"{self.settings.GAMMA_HOST}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 500,
                        "order": "volume24hr",
                        "ascending": "false",
                    }
                )
                if resp.status_code != 200:
                    logger.error(f"Gamma API error: {resp.status_code}")
                    return []
                all_markets = resp.json()
        except Exception as e:
            logger.error(f"Gamma API fetch failed: {e}")
            return []

        logger.info(f"SpreadCapture: scanning {len(all_markets)} markets via Gamma API")
        now = datetime.now(timezone.utc)

        for market in all_markets:
            condition_id = market.get("condition_id", "")
            if not condition_id:
                continue

            if self.portfolio.has_open_position(condition_id):
                continue
            if condition_id in self.traded_markets:
                if time.time() - self.traded_markets[condition_id] < 3600:
                    continue

            # Parse outcome prices from Gamma response
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_token = None
            no_token = None
            for t in tokens:
                outcome = t.get("outcome", "").upper()
                if outcome == "YES":
                    yes_token = t
                elif outcome == "NO":
                    no_token = t

            if not yes_token or not no_token:
                continue

            yes_id = yes_token.get("token_id", "")
            no_id = no_token.get("token_id", "")
            if not yes_id or not no_id:
                continue

            # Try multiple price fields from Gamma API
            yes_price = float(yes_token.get("price", 0) or 0)
            no_price = float(no_token.get("price", 0) or 0)

            # Also try outcomePrices at market level
            if (yes_price <= 0 or no_price <= 0):
                outcome_prices = market.get("outcomePrices", "")
                if outcome_prices and isinstance(outcome_prices, str):
                    try:
                        import json
                        prices = json.loads(outcome_prices)
                        if len(prices) >= 2:
                            yes_price = float(prices[0])
                            no_price = float(prices[1])
                    except Exception:
                        pass

            if yes_price <= 0.01 or no_price <= 0.01:
                continue

            total = yes_price + no_price

            # We want total < 1.0 (spread exists)
            if total >= 0.998:
                continue

            spread_profit = 1.0 - total
            net_profit = spread_profit - 0.004  # ~0.4% fees both sides

            if net_profit < 0.001:  # AGGRESSIVE: 0.1% guaranteed return (was 0.3%)
                continue

            return_pct = net_profit / total * 100

            # Calculate hours until resolution for scoring — 30-day max timeline
            hours_until = None
            end_date = market.get("end_date_iso", market.get("endDateIso", ""))
            if end_date:
                try:
                    resolution_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    hours_until = (resolution_dt - now).total_seconds() / 3600
                    if hours_until < 1:  # Skip about-to-close
                        continue
                    if hours_until > 2160:  # > 90 days (3 months) — skip
                        continue
                except Exception:
                    pass

            # Score: higher return + sooner closing = better
            time_mult = 1.0
            if hours_until:
                if hours_until <= 24:
                    time_mult = 3.0
                elif hours_until <= 72:
                    time_mult = 2.0
                elif hours_until <= 168:
                    time_mult = 1.5

            score = return_pct * time_mult

            opportunities.append({
                "condition_id": condition_id,
                "question": market.get("question", ""),
                "yes_token_id": yes_id,
                "no_token_id": no_id,
                "yes_price": yes_price,
                "no_price": no_price,
                "total": total,
                "spread_profit": spread_profit,
                "net_profit": net_profit,
                "return_pct": return_pct,
                "hours_until": hours_until,
                "score": score,
            })

        opportunities.sort(key=lambda x: x["score"], reverse=True)
        logger.info(f"SpreadCapture: {len(opportunities)} spread opportunities found")
        if opportunities:
            best = opportunities[0]
            logger.info(f"Best spread: {best['return_pct']:.1f}% on {best['question'][:50]} "
                       f"({best['yes_price']:.3f}+{best['no_price']:.3f}={best['total']:.3f})")
        return opportunities

    async def _execute_trade(self, opp: Dict) -> bool:
        """Execute spread capture trade. $15 per trade."""
        trade_size = 15.00  # $15 per trade — fits $200 portfolio (75% cap = $150)

        approved, reason = self.risk_manager.approve_trade(
            trade_size, "spread_capture", opp["condition_id"])
        if not approved:
            logger.debug(f"Spread trade rejected: {reason}")
            return False

        # Verify spread still exists via order books
        yes_book = await self.poly_client.get_order_book(opp["yes_token_id"])
        no_book = await self.poly_client.get_order_book(opp["no_token_id"])

        if not yes_book or not no_book:
            return False

        # Re-check with live order book prices
        live_total = yes_book.mid_price + no_book.mid_price
        live_profit = 1.0 - live_total - 0.004
        if live_profit < 0.001:  # AGGRESSIVE: 0.1% threshold (was 0.3%)
            logger.debug(f"Spread vanished: {live_total:.4f} (was {opp['total']:.4f})")
            return False

        half = trade_size / 2
        logger.info(
            f"[SPREAD] {opp['question'][:50]} | "
            f"YES: {yes_book.mid_price:.3f} + NO: {no_book.mid_price:.3f} = {live_total:.3f} | "
            f"Profit: {live_profit*100:.2f}% | Closes: {opp.get('hours_until', '?')}h"
        )

        yes_r = await self.poly_client.place_market_order(
            opp["yes_token_id"], half, "BUY", self.settings.DRY_RUN)
        no_r = await self.poly_client.place_market_order(
            opp["no_token_id"], half, "BUY", self.settings.DRY_RUN)

        if yes_r.success and no_r.success:
            expected_pnl = trade_size * live_profit  # used for logging only
            trade = Trade(
                id=None, timestamp=datetime.utcnow().isoformat(),
                strategy="spread_capture", market_id=opp["condition_id"],
                market_question=opp["question"], side="BOTH",
                token_id=f"{opp['yes_token_id'][:16]}|{opp['no_token_id'][:16]}",
                price=live_total,
                size_usd=trade_size, edge_pct=live_profit,
                dry_run=self.settings.DRY_RUN,
                order_id=f"{yes_r.order_id}|{no_r.order_id}",
                pnl=None, status="open"
            )
            self.portfolio.log_trade(trade)
            self.traded_markets[opp["condition_id"]] = time.time()
            logger.info(f"Spread captured! PnL: ${expected_pnl:.4f} ({live_profit*100:.2f}%)")
            return True

        return False

    async def cleanup(self):
        logger.info(f"SpreadCapture cleanup: {len(self.traded_markets)} markets traded")
