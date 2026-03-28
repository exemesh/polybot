"""
Cross-Platform Arbitrage Strategy
Detects price discrepancies between Polymarket and Kalshi for the same events.
Also detects single-platform arbitrage when YES + NO < $1.00.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import List, Optional, Dict

import httpx

from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager

logger = logging.getLogger("polybot.cross_arb")


class KalshiClient:
    def __init__(self, settings):
        self.settings = settings
        self.base_url = settings.KALSHI_BASE_URL

    async def _get_headers(self) -> Dict:
        if not self.settings.KALSHI_API_KEY:
            return {"Content-Type": "application/json"}
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.KALSHI_API_KEY}"
        }

    async def get_markets(self, status: str = "open", limit: int = 200) -> List[Dict]:
        try:
            headers = await self._get_headers()
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self.base_url}/markets",
                    params={"status": status, "limit": limit},
                    headers=headers
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("markets", [])
        except Exception as e:
            logger.error(f"Kalshi market fetch failed: {e}")
            return []

    async def get_market_price(self, ticker: str) -> Optional[Dict]:
        try:
            headers = await self._get_headers()
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.base_url}/markets/{ticker}",
                    headers=headers
                )
                resp.raise_for_status()
                data = resp.json().get("market", {})
                yes_price = data.get("yes_ask", data.get("yes_bid", 0)) / 100
                no_price = data.get("no_ask", data.get("no_bid", 0)) / 100
                return {
                    "ticker": ticker,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "title": data.get("title", ""),
                    "volume": data.get("volume", 0),
                }
        except Exception as e:
            logger.debug(f"Kalshi price fetch failed for {ticker}: {e}")
            return None


def similarity_score(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


class CrossPlatformArbStrategy:
    def __init__(self, settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)
        self.kalshi_client = KalshiClient(settings)
        self.executed_arbs: Dict[str, float] = {}

    async def run(self):
        logger.info("CrossPlatformArbStrategy started")
        scan_count = 0
        while True:
            try:
                scan_count += 1
                logger.debug(f"Arb scan #{scan_count}")

                type1_opps = await self._scan_single_platform_arb()
                for opp in type1_opps[:3]:
                    await self._execute_single_platform_arb(opp)

                if self.settings.KALSHI_API_KEY:
                    type2_opps = await self._scan_cross_platform_arb()
                    for opp in type2_opps[:2]:
                        await self._execute_cross_platform_arb(opp)

                await asyncio.sleep(self.settings.ARB_SCAN_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Arb strategy error: {e}", exc_info=True)
                await asyncio.sleep(30)

    async def run_once(self, open_token_ids=None):
        """Single scan-and-trade cycle."""
        logger.info("CrossPlatformArbStrategy: running single scan")
        try:
            type1_opps = await self._scan_single_platform_arb()
            for opp in type1_opps[:3]:
                await self._execute_single_platform_arb(opp)

            if self.settings.KALSHI_API_KEY:
                type2_opps = await self._scan_cross_platform_arb()
                for opp in type2_opps[:2]:
                    await self._execute_cross_platform_arb(opp)

            logger.info(f"Arb scan complete: {len(type1_opps)} single-platform opportunities")
        except Exception as e:
            logger.error(f"Arb strategy error: {e}", exc_info=True)

    async def _scan_single_platform_arb(self) -> List[Dict]:
        opportunities = []
        markets = await self.poly_client.get_markets()

        for market in markets[:100]:
            condition_id = market.get("condition_id", "")

            # Skip markets where we already have an open position (persisted in DB)
            if self.portfolio.has_open_position(condition_id):
                continue

            if condition_id in self.executed_arbs:
                if time.time() - self.executed_arbs[condition_id] < 3600:
                    continue

            # Filter: 30-day max timeline, min hours from settings
            end_date = market.get("end_date_iso", "")
            if end_date:
                try:
                    resolution_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    hours_until = (resolution_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_until < self.settings.ARB_MIN_HOURS_TO_RESOLUTION:
                        continue
                    if hours_until > 2160:  # > 90 days (3 months) — skip
                        continue
                except Exception:
                    pass

            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)
            if not yes_token or not no_token:
                continue

            yes_id = yes_token.get("token_id")
            no_id = no_token.get("token_id")

            yes_price = await self.poly_client.get_market_price(yes_id, "BUY")
            no_price = await self.poly_client.get_market_price(no_id, "BUY")

            if not yes_price or not no_price:
                continue

            total_cost = yes_price + no_price
            gross_profit = 1.0 - total_cost

            poly_fee = self.settings.ARB_POLY_FEE
            net_profit = gross_profit - (2 * poly_fee)
            net_edge_pct = net_profit / total_cost

            if net_edge_pct < self.settings.ARB_MIN_EDGE_PCT:
                continue

            yes_book = await self.poly_client.get_order_book(yes_id)
            no_book = await self.poly_client.get_order_book(no_id)

            if not yes_book or not no_book:
                continue

            min_liquidity = min(yes_book.liquidity_usd, no_book.liquidity_usd)
            if min_liquidity < 50:
                continue

            opportunities.append({
                "type": "single_platform",
                "condition_id": condition_id,
                "question": market.get("question", ""),
                "yes_token_id": yes_id,
                "no_token_id": no_id,
                "yes_price": yes_price,
                "no_price": no_price,
                "total_cost": total_cost,
                "gross_profit": gross_profit,
                "net_profit_per_dollar": net_profit,
                "edge_pct": net_edge_pct,
                "min_liquidity": min_liquidity,
            })

        opportunities.sort(key=lambda x: x["edge_pct"], reverse=True)
        if opportunities:
            logger.info(f"Single-platform arb: {len(opportunities)} opportunities, best edge: {opportunities[0]['edge_pct']:.2%}")
        return opportunities

    async def _execute_single_platform_arb(self, opp: Dict):
        # $10 USD per arb ($5 per side)
        max_size = 5.00  # $5.00 per side = $10.00 total

        approved, reason = self.risk_manager.approve_trade(max_size, "cross_platform_arb", opp["condition_id"])
        if not approved:
            logger.info(f"Arb rejected: {reason} | {opp['question'][:40]}")
            return

        logger.info(
            f"Single-Platform Arb | {opp['question'][:50]} | "
            f"YES: {opp['yes_price']:.3f} + NO: {opp['no_price']:.3f} = {opp['total_cost']:.3f} | "
            f"Edge: {opp['edge_pct']:.2%} | Size: ${max_size:.2f}"
        )

        yes_result, no_result = await asyncio.gather(
            self.poly_client.place_market_order(opp["yes_token_id"], max_size, "BUY", self.settings.DRY_RUN),
            self.poly_client.place_market_order(opp["no_token_id"], max_size, "BUY", self.settings.DRY_RUN)
        )

        if yes_result.success and no_result.success:
            # Fix: use total investment (max_size * 2) as denominator
            # For a $5+$5 arb at combined cost total_cost:
            # expected_profit = total_invested * (1 - total_cost)
            total_invested = max_size * 2
            expected_profit = total_invested * (1 - opp["total_cost"])

            trade = Trade(
                id=None, timestamp=datetime.utcnow().isoformat(),
                strategy="cross_platform_arb", market_id=opp["condition_id"],
                market_question=opp["question"], side="BOTH",
                token_id=f"{opp['yes_token_id']}|{opp['no_token_id']}",
                price=opp["total_cost"], size_usd=max_size * 2,
                edge_pct=opp["edge_pct"], dry_run=self.settings.DRY_RUN,
                order_id=f"{yes_result.order_id}|{no_result.order_id}",
                pnl=None, status="open"
            )
            self.portfolio.log_trade(trade)
            self.executed_arbs[opp["condition_id"]] = time.time()
            logger.info(f"Arb executed! Expected profit: ${expected_profit:.4f}")
        else:
            logger.warning(f"Arb failed: YES={yes_result.success}, NO={no_result.success}")

    async def _scan_cross_platform_arb(self) -> List[Dict]:
        opportunities = []

        kalshi_markets = await self.kalshi_client.get_markets()
        if not kalshi_markets:
            return []

        poly_markets = await self.poly_client.get_markets()
        poly_sample = poly_markets[:200]

        for kalshi_mkt in kalshi_markets[:100]:
            k_title = kalshi_mkt.get("title", "")
            k_yes = kalshi_mkt.get("yes_ask", 0) / 100
            k_ticker = kalshi_mkt.get("ticker_name", kalshi_mkt.get("ticker", ""))

            if not k_title or k_yes <= 0:
                continue

            best_match = None
            best_score = 0.65

            for poly_mkt in poly_sample:
                p_question = poly_mkt.get("question", "")
                score = similarity_score(k_title, p_question)
                if score > best_score:
                    best_score = score
                    best_match = poly_mkt

            if not best_match:
                continue

            tokens = best_match.get("tokens", [])
            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            if not yes_token:
                continue

            p_yes = await self.poly_client.get_market_price(yes_token["token_id"], "BUY")
            if not p_yes:
                continue

            spread_1 = p_yes - k_yes
            spread_2 = k_yes - p_yes
            gross_spread = max(spread_1, spread_2)
            net_spread = gross_spread - self.settings.ARB_POLY_FEE - self.settings.ARB_KALSHI_FEE

            if net_spread < self.settings.ARB_MIN_EDGE_PCT:
                continue

            opportunities.append({
                "type": "cross_platform",
                "kalshi_ticker": k_ticker,
                "kalshi_title": k_title,
                "poly_question": best_match.get("question"),
                "poly_condition_id": best_match.get("condition_id"),
                "poly_yes_token": yes_token["token_id"],
                "kalshi_yes_price": k_yes,
                "poly_yes_price": p_yes,
                "gross_spread": gross_spread,
                "net_spread": net_spread,
                "similarity_score": best_score,
                "direction": "buy_kalshi" if k_yes < p_yes else "buy_poly",
                "resolution_verified": False,
            })

        opportunities.sort(key=lambda x: x["net_spread"], reverse=True)
        return opportunities

    async def _execute_cross_platform_arb(self, opp: Dict):
        if not opp["resolution_verified"] and not self.settings.DRY_RUN:
            logger.warning(
                f"Cross-platform arb BLOCKED (resolution not verified): "
                f"Kalshi='{opp['kalshi_title']}' vs Poly='{opp['poly_question']}'"
            )
            return

        logger.info(
            f"Cross-Platform Arb | {opp['direction']} | "
            f"Kalshi: {opp['kalshi_yes_price']:.3f} | Poly: {opp['poly_yes_price']:.3f} | "
            f"Net Edge: {opp['net_spread']:.2%} | Match: {opp['similarity_score']:.0%}"
        )

        size = min(self.settings.ARB_MAX_POSITION_USD, self.portfolio.get_portfolio_value() * 0.03)
        approved, reason = self.risk_manager.approve_trade(size, "cross_platform_arb", opp["poly_condition_id"])
        if not approved:
            return

        trade = Trade(
            id=None, timestamp=datetime.utcnow().isoformat(),
            strategy="cross_platform_arb",
            market_id=opp["poly_condition_id"],
            market_question=f"[CROSS] {opp['kalshi_title']}",
            side=opp["direction"].upper(),
            token_id=opp["poly_yes_token"],
            price=opp["poly_yes_price"],
            size_usd=size, edge_pct=opp["net_spread"],
            dry_run=getattr(self.settings, 'DRY_RUN', True), pnl=None, status="open"
        )
        self.portfolio.log_trade(trade)

    async def cleanup(self):
        logger.info("CrossPlatformArbStrategy cleanup complete")
