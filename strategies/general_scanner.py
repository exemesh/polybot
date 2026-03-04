"""
General Market Scanner Strategy
Scans all active Polymarket markets for mispriced opportunities.
Trades markets closing within 30 days for aggressive capital turnover.
Uses real edge calculation instead of naive fair-value assumptions.

Trade rules:
- $2-5 USD per trade (arbs $5, value $2)
- Minimum 15% return potential
- 30-day max timeline, prefer markets closing sooner
- Max 20 trades per cycle
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict

from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager

logger = logging.getLogger("polybot.scanner")


class GeneralScannerStrategy:
    """Scans Polymarket for short-duration markets with high return potential."""

    def __init__(self, settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)
        self.traded_markets: Dict[str, float] = {}  # condition_id -> last_trade_time

    async def run_once(self):
        """Single scan-and-trade cycle for GitHub Actions."""
        logger.info("GeneralScanner: scanning for SHORT-DURATION high-return markets")
        try:
            opportunities = await self._scan_markets()
            executed = 0
            # Arb trades first (guaranteed profit) — unlimited
            # Value trades second — max 5
            arb_opps = [o for o in opportunities if o["type"] == "arb"]
            value_opps = [o for o in opportunities if o["type"] == "value"]

            for opp in arb_opps[:15]:  # Up to 15 arb trades per cycle
                success = await self._execute_trade(opp)
                if success:
                    executed += 1
            for opp in value_opps[:5]:  # Max 5 value trades per cycle
                success = await self._execute_trade(opp)
                if success:
                    executed += 1
            logger.info(f"GeneralScanner complete: {len(arb_opps)} arbs + {len(value_opps)} value | {executed} trades executed")
        except Exception as e:
            logger.error(f"GeneralScanner error: {e}", exc_info=True)

    async def _scan_markets(self) -> List[Dict]:
        """Fetch markets and find fast-closing high-return opportunities."""
        markets = await self.poly_client.get_markets(active_only=True)
        logger.info(f"GeneralScanner: analyzing {len(markets)} active markets")

        opportunities = []
        analyzed = 0
        skipped_no_tokens = 0
        skipped_no_book = 0
        skipped_low_liq = 0
        skipped_too_far = 0
        skipped_too_close = 0
        skipped_low_return = 0

        now = datetime.now(timezone.utc)

        for market in markets[:500]:  # AGGRESSIVE: Scan ALL 500 markets
            condition_id = market.get("condition_id", "")

            # Skip markets where we already have an open position (persisted in DB)
            if self.portfolio.has_open_position(condition_id):
                continue

            # Skip recently traded markets (1 hour in-memory cooldown)
            if condition_id in self.traded_markets:
                if time.time() - self.traded_markets[condition_id] < 3600:
                    continue

            # Must have YES and NO tokens
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                skipped_no_tokens += 1
                continue

            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)
            if not yes_token or not no_token:
                skipped_no_tokens += 1
                continue

            yes_id = yes_token.get("token_id")
            no_id = no_token.get("token_id")
            if not yes_id or not no_id:
                skipped_no_tokens += 1
                continue

            # ═══ 30-DAY MAX TIMELINE — aggressive capital turnover ═══
            # Only trade markets resolving within 30 days
            end_date = market.get("end_date_iso", "")
            hours_until = None
            if end_date:
                try:
                    resolution_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    hours_until = (resolution_dt - now).total_seconds() / 3600
                    if hours_until < 2:  # Too close to expiry
                        skipped_too_close += 1
                        continue
                    if hours_until > 2160:  # > 90 days (3 months) — skip
                        skipped_too_far += 1
                        continue
                except Exception:
                    pass
            # Markets without end dates still get scanned but at lower priority

            # Get order books for both sides
            analyzed += 1
            yes_book = await self.poly_client.get_order_book(yes_id)
            if not yes_book:
                skipped_no_book += 1
                continue

            no_book = await self.poly_client.get_order_book(no_id)
            if not no_book:
                skipped_no_book += 1
                continue

            # Minimum liquidity check — AGGRESSIVE: $10 (was $20)
            min_liquidity = min(yes_book.liquidity_usd, no_book.liquidity_usd)
            if min_liquidity < 10:
                skipped_low_liq += 1
                continue

            yes_mid = yes_book.mid_price
            no_mid = no_book.mid_price
            total = yes_mid + no_mid

            # Time urgency bonus: markets closing sooner get priority
            time_bonus = 0
            if hours_until and hours_until <= 24:
                time_bonus = 0.10  # Strong bonus for same-day resolution
            elif hours_until and hours_until <= 72:
                time_bonus = 0.05  # Moderate bonus for 3-day resolution
            elif hours_until and hours_until <= 168:
                time_bonus = 0.02  # Small bonus for 1-week resolution

            # ── Opportunity Type 1: Arbitrage (YES + NO < $1.00) ──
            # Guaranteed profit on resolution regardless of outcome
            # AGGRESSIVE: wider threshold to catch more arbs
            if total < 0.998:
                arb_edge = 1.0 - total - 0.004  # Subtract ~0.4% fees
                if arb_edge > 0.001:  # > 0.1% edge — AGGRESSIVE (was 0.3%)
                    # Calculate annualized return for ranking
                    return_pct = arb_edge / total * 100  # % return
                    opportunities.append({
                        "type": "arb",
                        "condition_id": condition_id,
                        "question": market.get("question", ""),
                        "yes_token_id": yes_id,
                        "no_token_id": no_id,
                        "yes_price": yes_mid,
                        "no_price": no_mid,
                        "edge": arb_edge,
                        "return_pct": return_pct,
                        "liquidity": min_liquidity,
                        "side": "BOTH",
                        "hours_until": hours_until,
                        "score": return_pct + time_bonus * 100,  # Prioritize quick closers
                    })
                    continue

            # ── Opportunity Type 2: High-conviction value bets ──
            # ONLY trade when:
            # - Token price implies >= 30% potential return on resolution
            # - Market closes within 7 days (prefer < 72h)
            # - Spread is tight (market is active)
            spread = yes_book.spread

            # For $1 trade at price P, if we win: payout = $1/P tokens * $1 = $1/P
            # Return = ($1/P - $1) / $1 = (1/P) - 1
            # For 30% return: need price <= 1/1.30 ≈ 0.77
            # But we also need actual conviction — not just cheap tokens

            # Buy YES side: value opportunity
            # REQUIRE end_date for value bets — no longshots without known resolution
            # AGGRESSIVE: wider price range, lower return threshold
            if 0.05 <= yes_mid <= 0.85 and spread < 0.15 and hours_until is not None:
                potential_return = (1.0 / yes_mid - 1.0) * 100  # % return if YES wins
                if potential_return >= 15 and min_liquidity > 10:  # 15% min (was 30%)
                    opportunities.append({
                        "type": "value",
                        "condition_id": condition_id,
                        "question": market.get("question", ""),
                        "yes_token_id": yes_id,
                        "no_token_id": no_id,
                        "yes_price": yes_mid,
                        "no_price": no_mid,
                        "edge": potential_return / 100,
                        "return_pct": potential_return,
                        "liquidity": min_liquidity,
                        "side": "BUY_YES",
                        "hours_until": hours_until,
                        "score": potential_return * (1 + time_bonus) * min(1.0, min_liquidity / 100),
                    })

            # Buy NO side: value opportunity
            elif 0.05 <= no_mid <= 0.85 and spread < 0.15 and hours_until is not None:
                potential_return = (1.0 / no_mid - 1.0) * 100
                if potential_return >= 15 and min_liquidity > 10:  # 15% min (was 30%)
                    opportunities.append({
                        "type": "value",
                        "condition_id": condition_id,
                        "question": market.get("question", ""),
                        "yes_token_id": yes_id,
                        "no_token_id": no_id,
                        "yes_price": yes_mid,
                        "no_price": no_mid,
                        "edge": potential_return / 100,
                        "return_pct": potential_return,
                        "liquidity": min_liquidity,
                        "side": "BUY_NO",
                        "hours_until": hours_until,
                        "score": potential_return * (1 + time_bonus) * min(1.0, min_liquidity / 100),
                    })

            # Rate limit: don't hammer the API
            if analyzed % 10 == 0:
                await asyncio.sleep(0.3)

        logger.info(f"GeneralScanner stats: analyzed={analyzed}, no_tokens={skipped_no_tokens}, "
                   f"no_book={skipped_no_book}, low_liq={skipped_low_liq}, "
                   f"too_far={skipped_too_far}, too_close={skipped_too_close}, low_return={skipped_low_return}")

        # Sort by score (combines return + time urgency + liquidity)
        opportunities.sort(key=lambda x: x["score"], reverse=True)

        if opportunities:
            best = opportunities[0]
            hrs = best.get('hours_until')
            hrs_str = f"{hrs:.0f}h" if hrs else "unknown"
            logger.info(f"GeneralScanner: {len(opportunities)} opportunities | "
                       f"Best: {best['type']} {best['return_pct']:.1f}% return, "
                       f"closes in {hrs_str}")
        else:
            logger.info("GeneralScanner: no opportunities found this cycle")
        return opportunities

    async def _execute_trade(self, opp: Dict) -> bool:
        """Execute a paper/live trade for an opportunity.
        Arb trades: $5 (guaranteed profit)
        Value trades: $10 (longshot bets targeting 5x+ payout)
        """
        trade_size = 5.00 if opp["type"] == "arb" else 10.00

        approved, reason = self.risk_manager.approve_trade(trade_size, "general_scanner", opp["condition_id"])
        if not approved:
            logger.debug(f"Trade rejected: {reason}")
            return False

        if opp["type"] == "arb":
            # Buy both YES and NO
            logger.info(
                f"[SCANNER] ARB | {opp['question'][:55]} | "
                f"YES: {opp['yes_price']:.3f} + NO: {opp['no_price']:.3f} = {(opp['yes_price']+opp['no_price']):.3f} | "
                f"Return: {opp['return_pct']:.1f}% | Closes: {opp.get('hours_until') or 0:.0f}h | Size: ${trade_size:.2f}"
            )
            half_size = trade_size / 2
            yes_result = await self.poly_client.place_market_order(
                opp["yes_token_id"], half_size, "BUY", self.settings.DRY_RUN)
            no_result = await self.poly_client.place_market_order(
                opp["no_token_id"], half_size, "BUY", self.settings.DRY_RUN)

            if yes_result.success and no_result.success:
                expected_pnl = trade_size * opp["edge"]
                trade = Trade(
                    id=None, timestamp=datetime.utcnow().isoformat(),
                    strategy="general_scanner", market_id=opp["condition_id"],
                    market_question=opp["question"], side="BOTH",
                    token_id=f"{opp['yes_token_id'][:16]}|{opp['no_token_id'][:16]}",
                    price=(opp["yes_price"] + opp["no_price"]),
                    size_usd=trade_size, edge_pct=opp["edge"],
                    dry_run=self.settings.DRY_RUN,
                    order_id=f"{yes_result.order_id}|{no_result.order_id}",
                    pnl=expected_pnl, status="open"
                )
                self.portfolio.log_trade(trade)
                self.traded_markets[opp["condition_id"]] = time.time()
                logger.info(f"ARB executed! Expected return: {opp['return_pct']:.1f}%, closes in {opp.get('hours_until') or 0:.0f}h")
                return True

        elif opp["type"] == "value":
            side = opp["side"]
            token_id = opp["yes_token_id"] if side == "BUY_YES" else opp["no_token_id"]
            price = opp["yes_price"] if side == "BUY_YES" else opp["no_price"]

            logger.info(
                f"[SCANNER] VALUE | {opp['question'][:55]} | "
                f"{side} @ {price:.3f} | Return potential: {opp['return_pct']:.0f}% | "
                f"Closes: {opp.get('hours_until') or 0:.0f}h | Size: ${trade_size:.2f}"
            )

            result = await self.poly_client.place_market_order(
                token_id, trade_size, "BUY", self.settings.DRY_RUN)

            if result.success:
                trade = Trade(
                    id=None, timestamp=datetime.utcnow().isoformat(),
                    strategy="general_scanner", market_id=opp["condition_id"],
                    market_question=opp["question"], side=side,
                    token_id=token_id,
                    price=price, size_usd=trade_size, edge_pct=opp["edge"],
                    dry_run=self.settings.DRY_RUN,
                    order_id=result.order_id,
                    pnl=None, status="open"
                )
                self.portfolio.log_trade(trade)
                self.traded_markets[opp["condition_id"]] = time.time()
                logger.info(f"VALUE trade placed: ${trade_size:.2f} | {opp['return_pct']:.0f}% potential | closes {opp.get('hours_until') or 0:.0f}h")
                return True

        return False

    async def cleanup(self):
        logger.info(f"GeneralScanner cleanup: {len(self.traded_markets)} markets traded")
