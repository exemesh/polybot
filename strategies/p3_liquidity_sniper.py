"""
P3: Liquidity Sniper (Polymarket)

Exploits thin order books:
1. Scans markets for wide bid/ask spreads (> SPREAD_THRESHOLD)
2. Estimates true fair value via LLM
3. Enters when fair value diverges from best available price by MIN_EDGE

Key insight: wide spreads = price discovery incomplete.
             Our information edge has higher alpha in thin markets.

Position sizing is conservative — thin books mean higher slippage risk.
Returns Signal objects for MetaAgent aggregation.
"""

import asyncio
import logging

import httpx

from core.meta_agent import Signal

logger = logging.getLogger("polybot.p3_liquidity_sniper")

SPREAD_THRESHOLD   = 0.06     # 6%+ spread = thin book candidate
MIN_EDGE           = 0.07     # 7% from fair value to best price
MIN_TOTAL_VOLUME   = 3_000    # Avoid completely dead markets
MAX_TOTAL_VOLUME   = 300_000  # Avoid very large markets (thin snipe rarely works there)
DEFAULT_SIZE_USD   = 9.0      # Smaller size — thin books = more slippage
MAX_SIGNALS        = 3


class P3LiquiditySniper:
    """
    Finds wide-spread markets and enters at estimated fair value.
    Returns Signal objects for MetaAgent aggregation.
    """

    def __init__(self, settings, portfolio, risk_manager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager

    async def scan(self, open_token_ids: set = None) -> list[Signal]:
        """Scan for thin-book opportunities, emit snipe signals."""
        open_token_ids = open_token_ids or set()
        signals: list[Signal] = []

        markets = await self._fetch_markets()
        if not markets:
            return signals

        # Filter to thin-book candidates, sorted by spread descending
        candidates = []
        for m in markets:
            spread = self._compute_spread(m)
            if spread < SPREAD_THRESHOLD:
                continue
            vol = float(m.get("volume", 0) or 0)
            if vol < MIN_TOTAL_VOLUME or vol > MAX_TOTAL_VOLUME:
                continue
            candidates.append((spread, m))
        candidates.sort(reverse=True)

        # Evaluate top candidates (limit LLM calls)
        for spread, market in candidates[:8]:
            fair_value = await self._estimate_fair_value(market)
            if fair_value is None:
                continue

            yes_bid = float(market.get("bestBid", 0.4) or 0.4)
            yes_ask = float(market.get("bestAsk", 0.6) or 0.6)

            if fair_value > yes_ask + MIN_EDGE:
                # Fair value above ask → BUY YES (ask is cheap relative to true value)
                side     = "YES"
                token_id = market.get("yes_token_id", "")
                price    = yes_ask          # we pay the ask
                edge     = fair_value - yes_ask - 0.01
                agent_p  = fair_value
            elif fair_value < yes_bid - MIN_EDGE:
                # Fair value below bid → BUY NO (YES bid is expensive)
                side     = "NO"
                token_id = market.get("no_token_id", "")
                price    = 1.0 - yes_bid   # cost of NO
                edge     = (1.0 - fair_value) - price - 0.01
                agent_p  = 1.0 - fair_value
            else:
                continue

            if not token_id or token_id in open_token_ids or edge < MIN_EDGE:
                continue

            sig = Signal(
                agent_name="P3_LiquiditySniper",
                market_id=market.get("conditionId", market.get("condition_id", "")),
                market_question=market.get("question", ""),
                token_id=token_id,
                side=side,
                agent_probability=round(agent_p, 3),
                market_price=round(price, 3),
                confidence=min(0.80, edge * 5),
                size_usd=DEFAULT_SIZE_USD,
                metadata={
                    "spread": spread,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "fair_value": fair_value,
                    "edge": edge,
                },
            )
            signals.append(sig)
            logger.info(
                f"P3: snipe [{side}] '{market.get('question','')[:50]}' "
                f"spread={spread:.1%} fair={fair_value:.2f} edge={edge:.1%}"
            )

            if len(signals) >= MAX_SIGNALS:
                break

        logger.info(f"P3 scan: {len(candidates)} thin-book markets → {len(signals)} snipe signals")
        return signals

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _compute_spread(self, market: dict) -> float:
        try:
            bid = float(market.get("bestBid", 0) or 0)
            ask = float(market.get("bestAsk", 1) or 1)
            if bid <= 0 or ask <= 0 or bid >= ask:
                return 0.0
            mid = (bid + ask) / 2.0
            return (ask - bid) / mid if mid > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    async def _estimate_fair_value(self, market: dict) -> float | None:
        """Estimate YES probability via LLM. Falls back to mid-price if no key."""
        bid = float(market.get("bestBid", 0.5) or 0.5)
        ask = float(market.get("bestAsk", 0.5) or 0.5)

        if not self.settings.OPENAI_API_KEY:
            return (bid + ask) / 2.0  # No real snipe edge without LLM

        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=self.settings.OPENAI_API_KEY)
            response = await client.chat.completions.create(
                model=self.settings.AI_MODEL,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Estimate the YES probability (0.00-1.00) for this prediction market:\n"
                        f"'{market.get('question', '')}'\n"
                        f"End date: {market.get('endDate', market.get('end_date', 'unknown'))}\n\n"
                        "Use base rates and current knowledge. "
                        "Reply with ONLY a decimal number (e.g., 0.63)."
                    ),
                }],
                max_tokens=10,
                temperature=0.2,
            )
            raw = response.choices[0].message.content.strip()
            import re
            m = re.search(r'0?\.\d+', raw)
            if m:
                return max(0.02, min(0.98, float(m.group())))
        except Exception as e:
            logger.debug(f"P3: fair value LLM failed: {e}")

        return None

    async def _fetch_markets(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"closed": "false", "active": "true",
                            "limit": 200, "order": "volume", "ascending": "false"},
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.warning(f"P3: markets fetch failed: {e}")
        return []

    async def cleanup(self):
        pass
