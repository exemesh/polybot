"""
General Market Scanner Strategy — ARB ONLY MODE
Scans Polymarket for genuine arbitrage opportunities (YES + NO < $1.00).

Strict rules (post-loss-audit):
- ARB ONLY: no directional/value trades — we have no research edge on outcomes
- Minimum 3% edge after fees (not 0.3% — slippage eats thin arbs)
- Minimum $50,000 CLOB liquidity (no illiquid lottery-ticket markets)
- Price filter 25¢-75¢ only (no <10¢ longshots on either side)
- Max 3 arb trades per cycle
- $10 USD per trade
- Cooldown: 4 hours per market after trade
"""

import asyncio
import json
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

    async def run_once(self, open_token_ids=None):
        """Single scan-and-trade cycle — arb only."""
        logger.info("GeneralScanner: scanning for HIGH-QUALITY arb opportunities (3%+ edge, $50k+ liquidity)")
        try:
            opportunities = await self._scan_markets()
            executed = 0
            # Arb trades only — max 3 per cycle to avoid overexposure
            for opp in opportunities[:3]:
                success = await self._execute_trade(opp)
                if success:
                    executed += 1
            logger.info(f"GeneralScanner complete: {len(opportunities)} arbs found | {executed} trades executed")
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

            # Skip recently traded markets (4 hour cooldown — arbs need time to resolve)
            if condition_id in self.traded_markets:
                if time.time() - self.traded_markets[condition_id] < 14400:
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
            # Gamma API returns endDate (full ISO datetime) — fall back to endDateIso (date only)
            end_date = market.get("endDate") or market.get("end_date_iso") or market.get("endDateIso") or ""
            hours_until = None
            if end_date:
                try:
                    resolution_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    if resolution_dt.tzinfo is None:
                        resolution_dt = resolution_dt.replace(tzinfo=timezone.utc)
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

            # ── Fast path: use Gamma API prices (already in market data, no extra calls) ──
            # CLOB order books show 0.01/0.99 placeholders for binary markets (sports, elections).
            # Gamma's outcomePrices / bestBid / bestAsk are the reliable source.
            analyzed += 1
            yes_mid = no_mid = spread = min_liquidity = total = None

            outcome_prices_raw = market.get("outcomePrices")
            gamma_bid = market.get("bestBid")
            gamma_ask = market.get("bestAsk")
            gamma_liq = float(market.get("liquidityClob") or market.get("liquidityNum") or 0)
            gamma_spread = float(market.get("spread") or 0)

            if outcome_prices_raw and gamma_bid and gamma_ask:
                try:
                    prices = json.loads(outcome_prices_raw) if isinstance(outcome_prices_raw, str) else outcome_prices_raw
                    y = float(prices[0])
                    n = float(prices[1])
                    sp = gamma_spread if gamma_spread else (float(gamma_ask) - float(gamma_bid))
                    liq = gamma_liq if gamma_liq > 0 else float(market.get("volume24hr") or 0) / 1000
                    if 0 < y < 1 and 0 < n < 1 and sp < 0.20:
                        yes_mid, no_mid, spread, min_liquidity, total = y, n, sp, liq, y + n
                except Exception:
                    pass  # fall through to CLOB

            if yes_mid is None:
                # Fall back to CLOB order book
                yes_book = await self.poly_client.get_order_book(yes_id)
                if not yes_book:
                    skipped_no_book += 1
                    continue
                no_book = await self.poly_client.get_order_book(no_id)
                if not no_book:
                    skipped_no_book += 1
                    continue
                min_liquidity = min(yes_book.liquidity_usd, no_book.liquidity_usd)
                yes_mid = yes_book.mid_price
                no_mid = no_book.mid_price
                total = yes_mid + no_mid
                spread = yes_book.spread

            # ── STRICT liquidity gate: $50k minimum ──
            # Illiquid markets have wide bid-ask, arb edges evaporate on execution
            if min_liquidity < 50_000:
                skipped_low_liq += 1
                continue

            # ── STRICT price filter: both sides must be 25¢-75¢ ──
            # Prevents buying lottery tickets (<10¢) and near-certain positions (>90¢)
            if not (0.25 <= yes_mid <= 0.75 and 0.25 <= no_mid <= 0.75):
                continue

            # Time urgency bonus for ranking
            time_bonus = 0
            if hours_until and hours_until <= 24:
                time_bonus = 0.10
            elif hours_until and hours_until <= 72:
                time_bonus = 0.05
            elif hours_until and hours_until <= 168:
                time_bonus = 0.02

            # ── ARB ONLY: YES + NO < $1.00, minimum 3% edge ──
            # 3% edge = absorbs CLOB fees (0.4%), slippage (1%), and still profitable
            # Anything below 3% is noise — our losses prove it
            if total < 0.970:  # YES + NO must sum to < 97¢
                arb_edge = 1.0 - total - 0.004  # Subtract ~0.4% fees
                if arb_edge >= 0.030:  # Must have 3%+ real edge
                    return_pct = arb_edge / total * 100
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
                        "score": return_pct + time_bonus * 100,
                    })
            # NOTE: No value/directional trades — we have no research edge on outcomes.
            # All directional trades go through news_arb (backed by breaking news signal).

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
        All trades: $10 USD. 4-hour cooldown per market.
        """
        trade_size = 10.00

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
                expected_pnl = trade_size * opp["edge"]  # used for logging only
                trade = Trade(
                    id=None, timestamp=datetime.utcnow().isoformat(),
                    strategy="general_scanner", market_id=opp["condition_id"],
                    market_question=opp["question"], side="BOTH",
                    token_id=f"{opp['yes_token_id'][:16]}|{opp['no_token_id'][:16]}",
                    price=(opp["yes_price"] + opp["no_price"]),
                    size_usd=trade_size, edge_pct=opp["edge"],
                    dry_run=self.settings.DRY_RUN,
                    order_id=f"{yes_result.order_id}|{no_result.order_id}",
                    pnl=None, status="open"
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
