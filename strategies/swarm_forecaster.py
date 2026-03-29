"""
SwarmForecasterStrategy — MiroFish-inspired multi-agent probability consensus

Inspired by: https://github.com/666ghj/MiroFish
Concept: Instead of a single AI opinion, spawn N independent AI "analyst" agents,
each with a distinct persona and reasoning angle, aggregate their probability
estimates, and trade when the swarm consensus diverges significantly from the
current Polymarket price.

One developer implemented this approach (2,847 simulated agents per trade) and
reported $4,266 profit over 338 trades. We run a lightweight 7-agent version
using gpt-4o-mini (~$0.0003 per market evaluation).

Strategy:
1. Fetch top Polymarket markets by volume (≥$100k CLOB liquidity)
2. Pull recent news context for each market via RSS
3. Run 7 parallel LLM evaluations — each agent has a different analytical persona
4. Aggregate: require ≥5/7 agents to agree on direction
5. Trade if consensus diverges ≥15% from market price
6. Log full reasoning trace to Discord #analyst channel
"""

import asyncio
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from config.settings import Settings
from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager

logger = logging.getLogger("polybot.swarm")

# ─── Strategy constants ────────────────────────────────────────────────────────
TRADE_SIZE_USD      = 10.0      # $10 per trade
MIN_DIVERGENCE      = 0.15      # 15% gap between swarm consensus and market price
MIN_AGENT_AGREEMENT = 5         # At least 5 out of 7 agents must agree on direction
MIN_LIQUIDITY_USD   = 100_000   # $100k minimum CLOB liquidity
PRICE_MIN           = 0.15      # Skip near-certain outcomes (lottery tickets)
PRICE_MAX           = 0.85
MAX_TRADES_PER_CYCLE = 2
CACHE_TTL_SECONDS   = 3600      # Re-evaluate each market at most once per hour

# ─── Agent personas ────────────────────────────────────────────────────────────
# Each persona brings a different analytical lens — the goal is diversity of thought.
# When 5/7 independent perspectives converge, the signal is much more reliable than
# any single analysis.
PERSONAS = [
    {
        "name": "Political Analyst",
        "prompt": (
            "You are a senior political analyst with 20 years of experience covering "
            "international affairs, elections, and geopolitics. You assess outcomes based "
            "on political incentives, historical precedent, and power dynamics."
        ),
    },
    {
        "name": "Statistical Base-Rate Modeler",
        "prompt": (
            "You are a quantitative researcher who relies on historical base rates, "
            "reference classes, and statistical patterns. You anchor on what has happened "
            "in similar situations in the past and adjust conservatively for new information."
        ),
    },
    {
        "name": "Devil's Advocate",
        "prompt": (
            "You are a contrarian analyst who always looks for reasons the consensus is wrong. "
            "You actively search for overlooked tail risks, hidden assumptions, and scenarios "
            "where conventional wisdom fails. You are comfortable assigning probability to "
            "outcomes that others dismiss."
        ),
    },
    {
        "name": "Domain Expert",
        "prompt": (
            "You are a domain expert with deep specialist knowledge in economics, finance, "
            "sports analytics, technology, and global events. You can quickly identify "
            "when a market price is inconsistent with ground truth in the relevant domain."
        ),
    },
    {
        "name": "Bayesian Updater",
        "prompt": (
            "You are a strict Bayesian reasoner. You start with a base rate prior, "
            "then methodically update based on each piece of new evidence. You quantify "
            "the weight of each news item and arrive at a posterior probability estimate. "
            "You are resistant to hype and media framing."
        ),
    },
    {
        "name": "News-Driven Momentum Analyst",
        "prompt": (
            "You are a news-driven trader who focuses on information flow and market "
            "reactions to recent developments. You assess how breaking news changes "
            "the probability of outcomes and whether markets have fully priced in "
            "the latest information."
        ),
    },
    {
        "name": "Risk Assessor",
        "prompt": (
            "You are a professional risk assessor who specialises in identifying "
            "low-probability, high-impact events. You are particularly attuned to "
            "tail risks, black swans, and scenarios where markets are overconfident. "
            "You provide well-calibrated probability estimates."
        ),
    },
]

# ─── Fast news fetch for context injection ────────────────────────────────────
NEWS_FEEDS = [
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.reuters.com/Reuters/PoliticsNews",
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.apnews.com/rss/apf-topnews",
]


class SwarmForecasterStrategy:
    """
    MiroFish-inspired swarm intelligence strategy for Polymarket.
    Runs 7 AI analyst agents per market to detect price divergences.
    """

    def __init__(self, settings: Settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)
        self._eval_cache: Dict[str, Tuple[float, float]] = {}  # cid -> (timestamp, consensus)
        self._traded: Dict[str, float] = {}  # cid -> last trade time
        self._news_cache: Optional[Tuple[float, List[str]]] = None

    # ─── Main cycle ────────────────────────────────────────────────────────────

    async def run_once(self, open_token_ids=None) -> None:
        if not self.settings.OPENAI_API_KEY:
            logger.info("SwarmForecaster: OPENAI_API_KEY not set — skipping")
            return

        logger.info("SwarmForecaster: starting multi-agent probability sweep")

        # 1. Fetch news context once for all markets
        news_headlines = await self._fetch_news_context()
        logger.info(f"SwarmForecaster: {len(news_headlines)} news headlines loaded")

        # 2. Get top liquid markets
        markets = await self._get_top_markets()
        if not markets:
            logger.info("SwarmForecaster: no eligible markets found")
            return
        logger.info(f"SwarmForecaster: evaluating {len(markets)} markets")

        # 3. Evaluate each market with the swarm
        trades = 0
        for market in markets:
            if trades >= MAX_TRADES_PER_CYCLE:
                break

            cid = market.get("condition_id", "")
            if self.portfolio.has_open_position(cid):
                continue
            if cid in self._traded and time.time() - self._traded[cid] < 14400:
                continue

            result = await self._evaluate_market(market, news_headlines)
            if not result:
                continue

            if await self._execute_trade(market, result):
                trades += 1

        logger.info(f"SwarmForecaster complete: {trades} trades executed")

    # ─── Market fetch ──────────────────────────────────────────────────────────

    async def _get_top_markets(self) -> List[Dict]:
        """Fetch top markets by volume with liquidity filter."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self.settings.GAMMA_HOST}/markets",
                    params={"closed": "false", "order": "liquidityClob",
                            "ascending": "false", "limit": "50"}
                )
            if resp.status_code != 200:
                return []

            now = datetime.now(timezone.utc)
            eligible = []
            for m in resp.json():
                # Liquidity gate
                liq = float(m.get("liquidityClob") or m.get("liquidityNum") or 0)
                if liq < MIN_LIQUIDITY_USD:
                    continue

                # Resolution time filter (2h - 30 days)
                end_str = m.get("endDate") or m.get("end_date_iso") or ""
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(
                            end_str.replace("Z", "+00:00"))
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                        hours = (end_dt - now).total_seconds() / 3600
                        if hours < 2 or hours > 720:
                            continue
                        m["_hours"] = hours
                    except Exception:
                        continue

                # Price filter — skip near-certain or binary extremes
                tokens = m.get("tokens", [])
                yes_t = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
                if not yes_t:
                    continue
                yes_p = float(yes_t.get("price") or 0)
                if not (PRICE_MIN <= yes_p <= PRICE_MAX):
                    continue

                m["_yes_price"] = yes_p
                m["_liquidity"] = liq
                eligible.append(m)

            # Sort by liquidity descending — most liquid = best price discovery
            eligible.sort(key=lambda x: x["_liquidity"], reverse=True)
            return eligible[:20]

        except Exception as e:
            logger.error(f"SwarmForecaster: market fetch error: {e}")
            return []

    # ─── News context ──────────────────────────────────────────────────────────

    async def _fetch_news_context(self) -> List[str]:
        """Return recent headlines from major feeds. Cached for 30 min."""
        now = time.time()
        if self._news_cache and now - self._news_cache[0] < 1800:
            return self._news_cache[1]

        headlines = []
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            tasks = [client.get(url, headers={"User-Agent": "PolyBot/1.0"})
                     for url in NEWS_FEEDS]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for resp in results:
            if isinstance(resp, Exception):
                continue
            try:
                if resp.status_code != 200:
                    continue
                root = ET.fromstring(resp.text)
                ch = root.find("channel")
                items = ch.findall("item") if ch is not None else []
                for item in items[:15]:
                    title = item.findtext("title") or ""
                    if title:
                        headlines.append(title.strip())
            except Exception:
                continue

        self._news_cache = (now, headlines[:60])
        return headlines[:60]

    # ─── Swarm evaluation ─────────────────────────────────────────────────────

    async def _evaluate_market(
        self, market: Dict, news: List[str]
    ) -> Optional[Dict]:
        """Run 7-agent swarm evaluation. Returns trade signal or None."""
        cid = market.get("condition_id", "")
        question = market.get("question", "")
        yes_price = market["_yes_price"]

        # Check cache
        cached = self._eval_cache.get(cid)
        if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
            consensus = cached[1]
            divergence = consensus - yes_price
            if abs(divergence) >= MIN_DIVERGENCE:
                return {"consensus": consensus, "divergence": divergence,
                        "estimates": [], "cached": True}
            return None

        # Build news context relevant to this market (keyword filter)
        q_words = set(re.findall(r'\b[A-Za-z]{4,}\b', question.lower()))
        relevant = [h for h in news if any(w in h.lower() for w in q_words)][:8]
        news_ctx = "\n".join(f"• {h}" for h in relevant) if relevant else "No directly relevant news found."

        # Run all 7 agents in parallel
        estimates = await asyncio.gather(
            *[self._agent_estimate(persona, question, yes_price, news_ctx)
              for persona in PERSONAS],
            return_exceptions=True
        )

        # Filter valid estimates
        valid = [e for e in estimates if isinstance(e, float) and 0.0 < e < 1.0]
        if len(valid) < 4:
            logger.debug(f"SwarmForecaster: not enough valid estimates for {question[:40]}")
            return None

        consensus = sum(valid) / len(valid)
        self._eval_cache[cid] = (time.time(), consensus)

        divergence = consensus - yes_price
        if abs(divergence) < MIN_DIVERGENCE:
            logger.debug(
                f"SwarmForecaster: {question[:40]} | consensus={consensus:.0%} "
                f"market={yes_price:.0%} divergence={divergence:+.0%} — below threshold"
            )
            return None

        # Check agreement direction
        side = "YES" if divergence > 0 else "NO"
        agrees = sum(
            1 for e in valid
            if (side == "YES" and e > yes_price + 0.05) or
               (side == "NO"  and e < yes_price - 0.05)
        )
        if agrees < MIN_AGENT_AGREEMENT:
            logger.info(
                f"SwarmForecaster: {question[:40]} — divergence {divergence:+.0%} "
                f"but only {agrees}/{len(valid)} agents agree — skipping"
            )
            return None

        logger.info(
            f"SwarmForecaster: 🎯 SIGNAL | {question[:55]} | "
            f"Swarm={consensus:.0%} Market={yes_price:.0%} "
            f"Divergence={divergence:+.0%} | Agreement: {agrees}/{len(valid)} | "
            f"Estimates: {[f'{e:.0%}' for e in valid]}"
        )
        return {
            "consensus": consensus,
            "divergence": divergence,
            "estimates": valid,
            "agrees": agrees,
            "side": side,
            "news_ctx": news_ctx,
            "cached": False,
        }

    async def _agent_estimate(
        self,
        persona: Dict,
        question: str,
        market_price: float,
        news_ctx: str,
    ) -> float:
        """Call LLM with a single analyst persona and return a probability."""
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=self.settings.OPENAI_API_KEY)

            prompt = (
                f"{persona['prompt']}\n\n"
                f"You are evaluating a Polymarket prediction market.\n\n"
                f"Market question: {question}\n"
                f"Current market implied probability (YES): {market_price:.0%}\n\n"
                f"Recent relevant news:\n{news_ctx}\n\n"
                f"Based on your expertise and the information above, what is your "
                f"probability estimate that the answer to this question resolves YES?\n"
                f"Think carefully, then respond with ONLY a single decimal number "
                f"between 0.0 and 1.0 (e.g., 0.72). Do not include any other text."
            )

            response = await client.chat.completions.create(
                model=self.settings.AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=10,
                temperature=0.3,
            )
            text = response.choices[0].message.content.strip()
            val = float(re.search(r'0?\.\d+|\d+\.\d+', text).group())
            return min(max(val, 0.01), 0.99)
        except Exception as e:
            logger.debug(f"SwarmForecaster agent {persona['name']} failed: {e}")
            return float("nan")

    # ─── Trade execution ───────────────────────────────────────────────────────

    async def _execute_trade(self, market: Dict, signal: Dict) -> bool:
        """Execute a swarm-backed trade."""
        cid = market.get("condition_id", "")
        question = market.get("question", "")
        tokens = market.get("tokens", [])
        yes_t = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
        no_t  = next((t for t in tokens if t.get("outcome", "").upper() == "NO"),  None)
        if not yes_t or not no_t:
            return False

        side = signal["side"]
        token_id = yes_t.get("token_id") if side == "YES" else no_t.get("token_id")
        price    = market["_yes_price"] if side == "YES" else (1 - market["_yes_price"])

        approved, reason = self.risk_manager.approve_trade(TRADE_SIZE_USD, "swarm_forecaster", cid)
        if not approved:
            logger.debug(f"SwarmForecaster trade rejected: {reason}")
            return False

        estimates_str = ", ".join(f"{e:.0%}" for e in signal.get("estimates", []))
        logger.info(
            f"[SWARM] ENTER {side} | {question[:55]} | "
            f"Price={price:.0%} | Swarm={signal['consensus']:.0%} | "
            f"Edge={signal['divergence']:+.0%} | "
            f"Agents: [{estimates_str}]"
        )

        result = await self.poly_client.place_market_order(
            token_id, TRADE_SIZE_USD, "BUY", self.settings.DRY_RUN
        )

        if result.success:
            trade = Trade(
                id=None,
                timestamp=datetime.utcnow().isoformat(),
                strategy="swarm_forecaster",
                market_id=cid,
                market_question=question,
                side=f"BUY_{side}",
                token_id=token_id,
                price=price,
                size_usd=TRADE_SIZE_USD,
                edge_pct=abs(signal["divergence"]),
                dry_run=self.settings.DRY_RUN,
                order_id=result.order_id,
                pnl=None,
                status="open",
            )
            self.portfolio.log_trade(trade)
            self._traded[cid] = time.time()

            # Post reasoning trace to Discord analyst channel
            await self._post_analyst_alert(market, signal, side, price)
            return True
        else:
            logger.warning(f"[SWARM] Order failed: {result.error}")
            return False

    async def _post_analyst_alert(
        self, market: Dict, signal: Dict, side: str, price: float
    ) -> None:
        """Send detailed swarm reasoning to Discord #analyst channel."""
        try:
            webhook = self.settings.DISCORD_WEBHOOK_RECON  # reuse Recon webhook
            if not webhook:
                return

            estimates = signal.get("estimates", [])
            bars = ""
            for persona, est in zip(PERSONAS[:len(estimates)], estimates):
                bar_len = int(est * 20)
                bar = "█" * bar_len + "░" * (20 - bar_len)
                bars += f"`{persona['name'][:20]:<20}` [{bar}] {est:.0%}\n"

            payload = {
                "username": "🧠 SwarmForecaster",
                "embeds": [{
                    "title": f"Swarm Signal — {side}",
                    "description": (
                        f"**{market.get('question', '')[:200]}**\n\n"
                        f"Market price: `{price:.0%}` → Swarm consensus: `{signal['consensus']:.0%}`\n"
                        f"Edge: `{signal['divergence']:+.0%}` | Agreement: `{signal.get('agrees', '?')}/7`\n"
                        f"Liquidity: `${market.get('_liquidity', 0):,.0f}`\n\n"
                        f"**Agent estimates:**\n{bars}\n"
                        f"**News context:**\n{signal.get('news_ctx', '')[:400]}"
                    ),
                    "color": 0x2ecc71 if signal["divergence"] > 0 else 0xe74c3c,
                }]
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(webhook, json=payload)
        except Exception as e:
            logger.debug(f"SwarmForecaster analyst alert failed: {e}")

    async def cleanup(self) -> None:
        logger.info(f"SwarmForecaster: {len(self._traded)} markets traded this session")
