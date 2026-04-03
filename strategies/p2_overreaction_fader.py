"""
P2: Overreaction Fader (Polymarket)

Identifies extreme price moves and bets on mean reversion:
1. Scans markets for YES-price moves > OVERREACTION_THRESHOLD in recent data
2. Checks if the move is fundamental (high volume confirmation) or reactionary
3. If reactionary → fade with the opposite side
4. Target: 50% mean reversion of the move

Classic trade: market spikes from 0.40 → 0.65 on rumors,
               P2 enters NO at 0.65, targets reversion to ~0.52.

Returns Signal objects for MetaAgent aggregation.
"""

import asyncio
import logging

import httpx

from core.meta_agent import Signal

logger = logging.getLogger("polybot.p2_overreaction_fader")

OVERREACTION_THRESHOLD = 0.18    # 18%+ move = candidate
MIN_VOLUME_USD         = 15_000  # Skip illiquid markets
MIN_EDGE               = 0.07    # 7% minimum edge
MEAN_REVERSION_FACTOR  = 0.50    # Expect 50% of move to revert
DEFAULT_SIZE_USD       = 10.0
MAX_SIGNALS            = 3

# Volume ratio: if 24h volume > this fraction of total volume, move is likely real
FUNDAMENTAL_VOLUME_RATIO = 0.25


class P2OverreactionFader:
    """
    Fades extreme Polymarket price moves. Returns Signal objects.
    """

    def __init__(self, settings, portfolio, risk_manager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager

    async def scan(self, open_token_ids: set = None) -> list[Signal]:
        """Find overreacted markets, emit fade signals."""
        open_token_ids = open_token_ids or set()
        signals: list[Signal] = []

        markets = await self._fetch_markets()
        if not markets:
            return signals

        for market in markets:
            move = self._estimate_move(market)
            if abs(move) < OVERREACTION_THRESHOLD:
                continue

            vol_24h = float(market.get("volume24hr", 0) or 0)
            if vol_24h < MIN_VOLUME_USD:
                continue

            yes_price = self._get_yes_mid(market)
            if yes_price <= 0.06 or yes_price >= 0.94:
                continue  # Too extreme to fade safely

            # Check if move was volume-confirmed (= fundamental, don't fade)
            total_vol = float(market.get("volume", 0) or 1)
            vol_ratio = vol_24h / total_vol
            if vol_ratio > FUNDAMENTAL_VOLUME_RATIO:
                logger.debug(
                    f"P2: '{market.get('question','')[:40]}' vol_ratio={vol_ratio:.0%} "
                    f"— fundamental, skip"
                )
                continue

            # Fade direction
            if move > 0:
                # Price spiked up → expect partial reversion → sell YES (buy NO)
                side = "NO"
                token_id = market.get("no_token_id", "")
                revert_to = yes_price - abs(move) * MEAN_REVERSION_FACTOR
                agent_prob_no = 1.0 - max(0.05, revert_to)
                market_price  = 1.0 - yes_price   # cost of NO token
            else:
                # Price dropped → expect bounce → buy YES
                side = "YES"
                token_id = market.get("yes_token_id", "")
                revert_to = yes_price + abs(move) * MEAN_REVERSION_FACTOR
                agent_prob_no = min(0.95, revert_to)
                market_price  = yes_price

            if not token_id or token_id in open_token_ids:
                continue

            agent_prob = agent_prob_no
            edge = abs(agent_prob - market_price) - 0.01
            if edge < MIN_EDGE:
                continue

            sig = Signal(
                agent_name="P2_OverreactionFader",
                market_id=market.get("conditionId", market.get("condition_id", "")),
                market_question=market.get("question", ""),
                token_id=token_id,
                side=side,
                agent_probability=round(agent_prob, 3),
                market_price=round(market_price, 3),
                confidence=min(0.85, abs(move) * 2.5),
                size_usd=DEFAULT_SIZE_USD,
                metadata={
                    "price_move": move,
                    "yes_price": yes_price,
                    "revert_target": revert_to,
                    "vol_ratio": vol_ratio,
                },
            )
            signals.append(sig)
            logger.info(
                f"P2: fade [{side}] '{market.get('question','')[:50]}' "
                f"move={move:+.1%} edge={edge:.1%}"
            )

            if len(signals) >= MAX_SIGNALS:
                break

        logger.info(f"P2 scan: {len(markets)} markets → {len(signals)} fade signals")
        return signals

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _estimate_move(self, market: dict) -> float:
        """Estimate 24h YES price change."""
        change = market.get("oneDayPriceChange", market.get("price_change_24h"))
        if change is not None:
            try:
                return float(change)
            except (TypeError, ValueError):
                pass
        # Fallback: compare lastTradePrice vs current mid
        last = float(market.get("lastTradePrice", 0) or 0)
        curr = self._get_yes_mid(market)
        if last > 0 and curr > 0:
            return curr - last
        return 0.0

    def _get_yes_mid(self, market: dict) -> float:
        try:
            bid = float(market.get("bestBid", 0.45) or 0.45)
            ask = float(market.get("bestAsk", 0.55) or 0.55)
            return max(0.01, min(0.99, (bid + ask) / 2.0))
        except (TypeError, ValueError):
            return 0.5

    async def _fetch_markets(self) -> list[dict]:
        """Fetch top markets sorted by 24h volume (most activity first)."""
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"closed": "false", "active": "true",
                            "limit": 150, "order": "volume24hr", "ascending": "false"},
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.warning(f"P2: markets fetch failed: {e}")
        return []

    async def cleanup(self):
        pass
