"""
Market Maker Strategy
Provides liquidity in low-competition markets by placing bid/ask quotes on both sides.

NOTE: Disabled by default in GitHub Actions mode - requires continuous quoting.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Set

from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager

logger = logging.getLogger("polybot.market_maker")


class MarketQuote:
    def __init__(self, token_id, bid_price, ask_price, size_usd, bid_order_id=None, ask_order_id=None):
        self.token_id = token_id
        self.bid_price = bid_price
        self.ask_price = ask_price
        self.size_usd = size_usd
        self.bid_order_id = bid_order_id
        self.ask_order_id = ask_order_id
        self.created_at = time.time()

    @property
    def spread(self):
        return self.ask_price - self.bid_price


class MarketMakerStrategy:
    def __init__(self, settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)
        self.active_quotes: Dict[str, MarketQuote] = {}
        self.blacklisted_markets: Set[str] = set()

    async def run(self):
        logger.info("MarketMakerStrategy started")
        await self._discover_markets()
        while True:
            try:
                if self.active_quotes:
                    await self._manage_quotes()
                else:
                    await self._discover_markets()
                await asyncio.sleep(self.settings.MM_UPDATE_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Market maker error: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def run_once(self, open_token_ids=None):
        """Single cycle - discover and place initial quotes only."""
        logger.info("MarketMakerStrategy: running single cycle (limited in cron mode)")
        try:
            await self._discover_markets()
            logger.info(f"MM cycle complete: {len(self.active_quotes)} quotes placed")
        except Exception as e:
            logger.error(f"Market maker error: {e}", exc_info=True)

    async def _discover_markets(self):
        logger.info("Discovering MM markets...")
        target_tags = ["sports", "entertainment", "crypto", "science", "economics"]
        candidate_markets = []

        for tag in target_tags[:2]:
            markets = await self.poly_client.get_markets(tag=tag)
            candidate_markets.extend(markets)

        evaluated = 0
        for market in candidate_markets:
            if len(self.active_quotes) >= 5:
                break

            market_id = market.get("condition_id", "")
            if market_id in self.blacklisted_markets:
                continue

            score = await self._score_market(market)
            if score and score["tradeable"]:
                await self._enter_market(market, score)
                evaluated += 1
                await asyncio.sleep(0.5)

        logger.info(f"MM discovery: {evaluated} markets evaluated, {len(self.active_quotes)} active")

    async def _score_market(self, market: Dict) -> Optional[Dict]:
        volume_24h = float(market.get("volume24hr", 0))
        liquidity = float(market.get("liquidity", 0))

        if volume_24h < self.settings.MM_MIN_VOLUME_24H:
            return None

        end_date_str = market.get("end_date_iso", "")
        if end_date_str:
            try:
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", ""))
                days_remaining = (end_dt - datetime.utcnow()).days
                if days_remaining < 2 or days_remaining > 60:
                    return None
            except Exception:
                pass

        tokens = market.get("tokens", [])
        if not tokens:
            return None

        yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), tokens[0])
        token_id = yes_token.get("token_id")
        if not token_id:
            return None

        order_book = await self.poly_client.get_order_book(token_id)
        if not order_book:
            return None

        current_spread = order_book.spread
        mid = order_book.mid_price

        if not (0.05 <= mid <= 0.95):
            return None

        price_balance_score = 1 - abs(mid - 0.5) * 2
        spread_score = min(current_spread / 0.10, 1.0)

        if liquidity > volume_24h * 100:
            return None

        composite_score = (price_balance_score * 0.4) + (spread_score * 0.4) + (min(volume_24h / 5000, 1.0) * 0.2)

        return {
            "tradeable": composite_score > 0.3 and current_spread >= self.settings.MM_MIN_SPREAD,
            "token_id": token_id,
            "market_id": market.get("condition_id"),
            "question": market.get("question", ""),
            "mid_price": mid,
            "natural_spread": current_spread,
            "volume_24h": volume_24h,
            "score": composite_score,
        }

    async def _enter_market(self, market: Dict, score: Dict):
        token_id = score["token_id"]
        mid = score["mid_price"]

        target_spread = max(self.settings.MM_MIN_SPREAD, score["natural_spread"] * 0.7)
        bid_price = round(mid - target_spread / 2, 3)
        ask_price = round(mid + target_spread / 2, 3)

        bid_price = max(0.01, min(0.99, bid_price))
        ask_price = max(0.01, min(0.99, ask_price))

        if ask_price - bid_price < self.settings.MM_MIN_SPREAD:
            return

        size = min(self.settings.MM_ORDER_SIZE_USD, self.portfolio.get_portfolio_value() * 0.05)
        approved, reason = self.risk_manager.approve_trade(size * 2, "market_maker", score["market_id"])
        if not approved:
            return

        logger.info(
            f"MM Entering | {score['question'][:50]} | "
            f"Bid: {bid_price:.3f} | Ask: {ask_price:.3f} | "
            f"Spread: {target_spread:.3f} | Size: ${size:.2f}"
        )

        bid_result = await self.poly_client.place_limit_order(
            token_id=token_id, price=bid_price, size=size / bid_price,
            side="BUY", dry_run=self.settings.DRY_RUN
        )
        ask_result = await self.poly_client.place_limit_order(
            token_id=token_id, price=ask_price, size=size / ask_price,
            side="SELL", dry_run=self.settings.DRY_RUN
        )

        if bid_result.success and ask_result.success:
            quote = MarketQuote(
                token_id=token_id, bid_price=bid_price, ask_price=ask_price,
                size_usd=size, bid_order_id=bid_result.order_id, ask_order_id=ask_result.order_id
            )
            self.active_quotes[token_id] = quote

            trade = Trade(
                id=None, timestamp=datetime.utcnow().isoformat(),
                strategy="market_maker", market_id=score["market_id"],
                market_question=score["question"], side="QUOTE",
                token_id=token_id, price=mid, size_usd=size * 2,
                edge_pct=target_spread, dry_run=self.settings.DRY_RUN,
                order_id=f"{bid_result.order_id}|{ask_result.order_id}",
                status="open"
            )
            self.portfolio.log_trade(trade)

    async def _manage_quotes(self):
        tokens_to_remove = []
        for token_id, quote in self.active_quotes.items():
            order_book = await self.poly_client.get_order_book(token_id)
            if not order_book:
                tokens_to_remove.append(token_id)
                continue

            current_mid = order_book.mid_price
            quote_mid = (quote.bid_price + quote.ask_price) / 2
            price_drift = abs(current_mid - quote_mid)

            if price_drift > 0.03:
                if quote.bid_order_id:
                    await self.poly_client.cancel_order(quote.bid_order_id, self.settings.DRY_RUN)
                if quote.ask_order_id:
                    await self.poly_client.cancel_order(quote.ask_order_id, self.settings.DRY_RUN)

                target_spread = max(self.settings.MM_MIN_SPREAD, order_book.spread * 0.7)
                new_bid = round(current_mid - target_spread / 2, 3)
                new_ask = round(current_mid + target_spread / 2, 3)

                size = quote.size_usd
                bid_result = await self.poly_client.place_limit_order(
                    token_id=token_id, price=new_bid, size=size / new_bid,
                    side="BUY", dry_run=self.settings.DRY_RUN
                )
                ask_result = await self.poly_client.place_limit_order(
                    token_id=token_id, price=new_ask, size=size / new_ask,
                    side="SELL", dry_run=self.settings.DRY_RUN
                )

                if bid_result.success and ask_result.success:
                    quote.bid_price = new_bid
                    quote.ask_price = new_ask
                    quote.bid_order_id = bid_result.order_id
                    quote.ask_order_id = ask_result.order_id

        for token_id in tokens_to_remove:
            del self.active_quotes[token_id]

    async def cleanup(self):
        logger.info(f"Cancelling {len(self.active_quotes)} MM quotes...")
        for token_id, quote in self.active_quotes.items():
            if quote.bid_order_id:
                await self.poly_client.cancel_order(quote.bid_order_id, self.settings.DRY_RUN)
            if quote.ask_order_id:
                await self.poly_client.cancel_order(quote.ask_order_id, self.settings.DRY_RUN)
