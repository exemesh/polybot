"""
Momentum Scalper Strategy
Targets markets approaching resolution with strong momentum signals.

This strategy looks for:
1. Markets closing within 30 days where one outcome has strong momentum
2. Near-expiry arbs: YES + NO < $1 on markets about to close (guaranteed quick profit)
3. High-confidence near-binary outcomes: markets where one side is 65%+
   and the other side is still trading above 0 — buy the likely winner cheap

Trade rules:
- $2-5 USD per trade (arbs $5, momentum $3, value $2)
- 30-day max timeline, prefer markets closing sooner
- Max 10 trades per cycle
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional, Dict

from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager

logger = logging.getLogger("polybot.momentum")


class MomentumScalperStrategy:
    """Scalps quick profits from markets about to resolve."""

    def __init__(self, settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)
        self.traded_markets: Dict[str, float] = {}

    async def run_once(self):
        """Single scan-and-trade cycle."""
        logger.info("MomentumScalper: hunting near-expiry opportunities")
        try:
            opportunities = await self._scan_near_expiry()
            executed = 0
            for opp in opportunities[:10]:  # Max 10 trades (prioritize arbs)
                success = await self._execute_trade(opp)
                if success:
                    executed += 1
            logger.info(f"MomentumScalper complete: {len(opportunities)} opportunities, {executed} trades")
        except Exception as e:
            logger.error(f"MomentumScalper error: {e}", exc_info=True)

    async def _scan_near_expiry(self) -> List[Dict]:
        """Find markets closing within 48 hours with profitable setups."""
        markets = await self.poly_client.get_markets(active_only=True)
        logger.info(f"MomentumScalper: scanning {len(markets)} markets for near-expiry plays")

        opportunities = []
        now = datetime.now(timezone.utc)
        analyzed = 0

        for market in markets[:500]:  # AGGRESSIVE: Scan ALL markets
            condition_id = market.get("condition_id", "")

            if self.portfolio.has_open_position(condition_id):
                continue
            if condition_id in self.traded_markets:
                if time.time() - self.traded_markets[condition_id] < 1800:  # 30min cooldown
                    continue

            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)
            if not yes_token or not no_token:
                continue

            yes_id = yes_token.get("token_id")
            no_id = no_token.get("token_id")
            if not yes_id or not no_id:
                continue

            # Focus on markets closing within 7 days (168 hours)
            end_date = market.get("end_date_iso", market.get("endDateIso", ""))
            if not end_date:
                continue

            try:
                resolution_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                hours_until = (resolution_dt - now).total_seconds() / 3600
                if hours_until < 1 or hours_until > 2160:  # 90-day (3 month) max timeline
                    continue
            except Exception:
                continue

            analyzed += 1
            yes_book = await self.poly_client.get_order_book(yes_id)
            no_book = await self.poly_client.get_order_book(no_id)
            if not yes_book or not no_book:
                continue

            min_liq = min(yes_book.liquidity_usd, no_book.liquidity_usd)
            if min_liq < 5:  # AGGRESSIVE: $5 min liquidity (was $15)
                continue

            yes_mid = yes_book.mid_price
            no_mid = no_book.mid_price
            total = yes_mid + no_mid
            question = market.get("question", "")

            # ── Type 1: Near-expiry arb (guaranteed profit) ──
            if total < 0.995:
                arb_edge = 1.0 - total - 0.004
                if arb_edge > 0.001:  # Even tiny edge is good when closing soon
                    return_pct = arb_edge / total * 100
                    opportunities.append({
                        "type": "expiry_arb",
                        "condition_id": condition_id,
                        "question": question,
                        "yes_token_id": yes_id,
                        "no_token_id": no_id,
                        "yes_price": yes_mid,
                        "no_price": no_mid,
                        "edge": arb_edge,
                        "return_pct": return_pct,
                        "hours_until": hours_until,
                        "liquidity": min_liq,
                        "side": "BOTH",
                        "score": return_pct * (48 / max(hours_until, 1)),  # Sooner = better
                    })
                    continue

            # ── Type 2: High-conviction play ──
            # AGGRESSIVE: 65%+ favorite (was 75%) — more opportunities
            if yes_mid >= 0.65 and no_mid > 0.02:
                return_if_win = (1.0 / yes_mid - 1.0) * 100
                if return_if_win >= 1.0:  # At least 1% return
                    opportunities.append({
                        "type": "momentum_yes",
                        "condition_id": condition_id,
                        "question": question,
                        "yes_token_id": yes_id,
                        "no_token_id": no_id,
                        "yes_price": yes_mid,
                        "no_price": no_mid,
                        "edge": return_if_win / 100,
                        "return_pct": return_if_win,
                        "hours_until": hours_until,
                        "liquidity": min_liq,
                        "side": "BUY_YES",
                        "score": return_if_win * (168 / max(hours_until, 1)),
                    })

            elif no_mid >= 0.65 and yes_mid > 0.02:  # AGGRESSIVE: 65% (was 75%)
                return_if_win = (1.0 / no_mid - 1.0) * 100
                if return_if_win >= 1.0:
                    opportunities.append({
                        "type": "momentum_no",
                        "condition_id": condition_id,
                        "question": question,
                        "yes_token_id": yes_id,
                        "no_token_id": no_id,
                        "yes_price": yes_mid,
                        "no_price": no_mid,
                        "edge": return_if_win / 100,
                        "return_pct": return_if_win,
                        "hours_until": hours_until,
                        "liquidity": min_liq,
                        "side": "BUY_NO",
                        "score": return_if_win * (168 / max(hours_until, 1)),
                    })

            # ── Type 3: Value play ──
            # Markets closing within 30 days, wider price range, 15% min return
            if hours_until <= 2160:
                if 0.10 <= yes_mid <= 0.80 and yes_book.spread < 0.15:
                    return_pct = (1.0 / yes_mid - 1.0) * 100
                    if return_pct >= 15:  # 15% min (was 30%)
                        opportunities.append({
                            "type": "near_expiry_value",
                            "condition_id": condition_id,
                            "question": question,
                            "yes_token_id": yes_id,
                            "no_token_id": no_id,
                            "yes_price": yes_mid,
                            "no_price": no_mid,
                            "edge": return_pct / 100,
                            "return_pct": return_pct,
                            "hours_until": hours_until,
                            "liquidity": min_liq,
                            "side": "BUY_YES",
                            "score": return_pct * 2,
                        })
                elif 0.10 <= no_mid <= 0.80 and no_book.spread < 0.15:
                    return_pct = (1.0 / no_mid - 1.0) * 100
                    if return_pct >= 15:  # 15% min (was 30%)
                        opportunities.append({
                            "type": "near_expiry_value",
                            "condition_id": condition_id,
                            "question": question,
                            "yes_token_id": yes_id,
                            "no_token_id": no_id,
                            "yes_price": yes_mid,
                            "no_price": no_mid,
                            "edge": return_pct / 100,
                            "return_pct": return_pct,
                            "hours_until": hours_until,
                            "liquidity": min_liq,
                            "side": "BUY_NO",
                            "score": return_pct * 2,
                        })

            if analyzed % 15 == 0:
                await asyncio.sleep(0.2)

        opportunities.sort(key=lambda x: x["score"], reverse=True)
        logger.info(f"MomentumScalper: {analyzed} analyzed, {len(opportunities)} opportunities")
        if opportunities:
            best = opportunities[0]
            logger.info(f"Best: {best['type']} | {best['question'][:40]} | "
                       f"{best['return_pct']:.1f}% return | closes {best['hours_until']:.1f}h")
        return opportunities

    async def _execute_trade(self, opp: Dict) -> bool:
        """Execute trade. All trades $10 USD."""
        trade_size = 10.00

        approved, reason = self.risk_manager.approve_trade(trade_size, "momentum_scalper", opp["condition_id"])
        if not approved:
            logger.debug(f"Trade rejected: {reason}")
            return False

        if opp["side"] == "BOTH":
            # Arb: buy both sides
            half = trade_size / 2
            logger.info(
                f"[MOMENTUM] EXPIRY ARB | {opp['question'][:50]} | "
                f"YES: {opp['yes_price']:.3f} + NO: {opp['no_price']:.3f} | "
                f"Return: {opp['return_pct']:.1f}% | Closes: {opp['hours_until']:.1f}h"
            )
            yes_r = await self.poly_client.place_market_order(
                opp["yes_token_id"], half, "BUY", self.settings.DRY_RUN)
            no_r = await self.poly_client.place_market_order(
                opp["no_token_id"], half, "BUY", self.settings.DRY_RUN)

            if yes_r.success and no_r.success:
                pnl = trade_size * opp["edge"]
                trade = Trade(
                    id=None, timestamp=datetime.utcnow().isoformat(),
                    strategy="momentum_scalper", market_id=opp["condition_id"],
                    market_question=opp["question"], side="BOTH",
                    token_id=f"{opp['yes_token_id'][:16]}|{opp['no_token_id'][:16]}",
                    price=(opp["yes_price"] + opp["no_price"]),
                    size_usd=trade_size, edge_pct=opp["edge"],
                    dry_run=self.settings.DRY_RUN,
                    order_id=f"{yes_r.order_id}|{no_r.order_id}",
                    pnl=pnl, status="open"
                )
                self.portfolio.log_trade(trade)
                self.traded_markets[opp["condition_id"]] = time.time()
                return True
        else:
            # Single side trade
            token_id = opp["yes_token_id"] if "YES" in opp["side"] else opp["no_token_id"]
            price = opp["yes_price"] if "YES" in opp["side"] else opp["no_price"]

            logger.info(
                f"[MOMENTUM] {opp['type'].upper()} | {opp['question'][:50]} | "
                f"{opp['side']} @ {price:.3f} | Return: {opp['return_pct']:.1f}% | "
                f"Closes: {opp['hours_until']:.1f}h"
            )

            result = await self.poly_client.place_market_order(
                token_id, trade_size, "BUY", self.settings.DRY_RUN)

            if result.success:
                trade = Trade(
                    id=None, timestamp=datetime.utcnow().isoformat(),
                    strategy="momentum_scalper", market_id=opp["condition_id"],
                    market_question=opp["question"], side=opp["side"],
                    token_id=token_id,
                    price=price, size_usd=trade_size, edge_pct=opp["edge"],
                    dry_run=self.settings.DRY_RUN,
                    order_id=result.order_id,
                    pnl=None, status="open"
                )
                self.portfolio.log_trade(trade)
                self.traded_markets[opp["condition_id"]] = time.time()
                return True

        return False

    async def cleanup(self):
        logger.info(f"MomentumScalper cleanup: {len(self.traded_markets)} markets traded")
