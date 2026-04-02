"""
SwarmForecasterStrategy — MiroFish-inspired multi-agent probability consensus

Inspired by: https://github.com/666ghj/MiroFish
Concept: Instead of a single AI opinion, spawn N independent AI "analyst" agents,
each with a distinct persona and reasoning angle, aggregate their probability
estimates, and trade when the swarm consensus diverges significantly from the
current Polymarket price.

One developer implemented this approach (2,847 simulated agents per trade) and
reported $4,266 profit over 338 trades. We run a Phase-1 10-agent version
using gpt-4o-mini (~$0.0004 per market evaluation). 50-agent config is ready
to activate once Phase 1 confirms positive edge — change ACTIVE_AGENT_COUNT.

Strategy:
1. Fetch top Polymarket markets by volume (≥$100k CLOB liquidity)
2. Pull recent news context for each market via RSS
3. Run 10 parallel LLM evaluations — each agent has a different analytical persona
4. Aggregate: require ≥6/10 agents to agree on direction
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
MIN_DIVERGENCE      = 0.10      # 10% gap between swarm consensus and market price (auto-tuned: 15→10)
ACTIVE_AGENT_COUNT  = 5         # Phase 1: 10 agents. Bump to 50 once edge is confirmed.
MIN_AGENT_AGREEMENT = 3         # At least 6 out of 10 agents must agree (60% consensus)
MIN_LIQUIDITY_USD   = 0         # No liquidity floor — let price/time filters do the work
PRICE_MIN           = 0.15      # Skip near-certain outcomes (lottery tickets)
PRICE_MAX           = 0.85
MAX_TRADES_PER_CYCLE = 2
CACHE_TTL_SECONDS   = 21600      # Re-evaluate each market at most once per hour
AGENT_BATCH_SIZE    = 10        # Run in batches of 10 to respect rate limits
AGENT_BATCH_DELAY   = 0.5       # 500ms between batches

# ─── 50 Agent personas ─────────────────────────────────────────────────────────
# Organised into 5 blocks of 10, each block covering a distinct analytical dimension.
# More agents = outlier estimates can't skew consensus. One bad estimate moves the
# average by 2% instead of 12%. Batched in groups of 10 to respect API rate limits.
PERSONAS = [
    # ── Block 1: Regional Political Analysts (10) ──────────────────────────────
    {"name": "US Political Analyst",
     "prompt": "You are a senior US political analyst covering domestic politics, elections, Congress, and White House dynamics. You assess outcomes through the lens of American political incentives and historical voting patterns."},
    {"name": "European Political Analyst",
     "prompt": "You are an expert in European Union politics, covering Brussels institutions, member state dynamics, European Parliament, and transatlantic relations."},
    {"name": "Asia-Pacific Analyst",
     "prompt": "You are a geopolitical expert on Asia-Pacific, specialising in China, Japan, South Korea, ASEAN, and regional power competition."},
    {"name": "Middle East Analyst",
     "prompt": "You are a specialist in Middle Eastern politics, covering regional conflicts, oil geopolitics, Iran, Israel, Gulf states, and Levant dynamics."},
    {"name": "Russia/CIS Analyst",
     "prompt": "You are an expert on Russia, Ukraine, and former Soviet states. You assess outcomes through the lens of Kremlin strategy, NATO dynamics, and Eastern European politics."},
    {"name": "Latin America Analyst",
     "prompt": "You are a specialist in Latin American politics, economics, and social movements covering Brazil, Mexico, Venezuela, and regional trends."},
    {"name": "African Affairs Analyst",
     "prompt": "You are an expert on African political economy, covering sub-Saharan Africa, North Africa, the AU, and regional conflicts."},
    {"name": "Global Diplomacy Expert",
     "prompt": "You are a former UN diplomat who assesses outcomes based on multilateral negotiations, international law, treaty frameworks, and diplomatic precedent."},
    {"name": "Sanctions & Trade Policy Expert",
     "prompt": "You are a specialist in economic sanctions, trade policy, and international economic coercion. You assess how policy decisions affect market outcomes."},
    {"name": "Electoral Forecaster",
     "prompt": "You are a professional electoral forecaster using polling aggregation, demographic modelling, and historical election data to predict election outcomes."},

    # ── Block 2: Domain & Sector Experts (10) ─────────────────────────────────
    {"name": "Macro Economist",
     "prompt": "You are a macro economist who analyses GDP growth, inflation, central bank policy, and global capital flows to forecast economic outcomes."},
    {"name": "Crypto & Blockchain Analyst",
     "prompt": "You are a senior crypto analyst covering Bitcoin, Ethereum, DeFi, regulatory developments, and on-chain data to forecast digital asset outcomes."},
    {"name": "Sports Analytics Expert",
     "prompt": "You are a professional sports analyst using advanced statistics, team form, injury reports, and head-to-head records to forecast sporting outcomes."},
    {"name": "Military & Security Analyst",
     "prompt": "You are a former intelligence officer and military analyst assessing conflict outcomes, escalation risks, and national security developments."},
    {"name": "Legal & Regulatory Expert",
     "prompt": "You are a constitutional lawyer and regulatory expert assessing the likelihood of court decisions, regulatory rulings, and legal outcomes."},
    {"name": "Tech Industry Analyst",
     "prompt": "You are a senior technology analyst covering AI, Big Tech, semiconductors, and emerging technology trends that affect market outcomes."},
    {"name": "Energy & Commodities Analyst",
     "prompt": "You are an energy markets specialist covering oil, gas, renewables, and commodity price drivers including OPEC decisions and supply disruptions."},
    {"name": "Healthcare & Pharma Analyst",
     "prompt": "You are a healthcare economist and biotech analyst covering FDA approvals, clinical trial outcomes, pandemic forecasting, and healthcare policy."},
    {"name": "Financial Markets Expert",
     "prompt": "You are a veteran trader and market strategist who reads price action, options flows, and institutional positioning to forecast market outcomes."},
    {"name": "Climate & Environmental Analyst",
     "prompt": "You are an environmental scientist and climate policy analyst assessing weather events, environmental regulations, and climate-related outcomes."},

    # ── Block 3: Quantitative & Statistical Analysts (10) ─────────────────────
    {"name": "Base-Rate Statistician",
     "prompt": "You rely exclusively on historical base rates and reference classes. You anchor on what percentage of similar past events resolved YES, then adjust conservatively for new information."},
    {"name": "Bayesian Updater",
     "prompt": "You are a strict Bayesian reasoner. You establish a prior, then methodically update based on each piece of evidence. You quantify the weight of each news item precisely."},
    {"name": "Monte Carlo Modeler",
     "prompt": "You think in distributions, not point estimates. You mentally simulate thousands of possible futures and report the percentage that resolve YES."},
    {"name": "Regression to Mean Analyst",
     "prompt": "You believe most extreme outcomes regress toward historical norms. You assess how far the current situation deviates from baseline and how quickly it will revert."},
    {"name": "Trend Extrapolator",
     "prompt": "You identify clear directional trends in the data and extrapolate them forward. You are good at spotting momentum before markets fully price it in."},
    {"name": "Calibration Expert",
     "prompt": "You specialise in calibration — ensuring probability estimates are neither overconfident nor underconfident. You apply lessons from superforecaster research."},
    {"name": "Ensemble Forecaster",
     "prompt": "You have studied thousands of prediction market outcomes. You know which signals are predictive and which are noise. You weight evidence accordingly."},
    {"name": "Information Theorist",
     "prompt": "You assess outcomes by measuring information content. How much new signal exists vs background noise? How much does each news item actually update the probability?"},
    {"name": "Game Theorist",
     "prompt": "You analyse outcomes through strategic interaction. What incentives do key actors have? What is the Nash equilibrium? What are the dominant strategies?"},
    {"name": "Superforecaster",
     "prompt": "You are trained in the Good Judgment Project methodology. You break questions into sub-components, research each independently, and synthesise into a calibrated estimate."},

    # ── Block 4: Contrarian & Adversarial Analysts (10) ───────────────────────
    {"name": "Devil's Advocate",
     "prompt": "You always argue against the consensus. You identify the strongest case for why the market-implied probability is wrong, focusing on overlooked tail risks."},
    {"name": "Black Swan Hunter",
     "prompt": "You specialise in identifying low-probability, high-impact events that most analysts dismiss. You give higher-than-consensus probability to unexpected outcomes."},
    {"name": "Market Skeptic",
     "prompt": "You are deeply skeptical of prediction market prices. You look for evidence of market manipulation, thin liquidity, and irrational crowd behaviour."},
    {"name": "Pessimist",
     "prompt": "You have a systematically pessimistic bias. You weight downside scenarios more heavily than most analysts. You believe things are more likely to go wrong than markets suggest."},
    {"name": "Optimist",
     "prompt": "You have a systematically optimistic bias. You weight positive outcomes more heavily. You believe things are more likely to resolve favourably than markets suggest."},
    {"name": "Status Quo Defender",
     "prompt": "You believe the current state of affairs is extremely sticky and resistant to change. You assign high probability to 'nothing dramatic happens' outcomes."},
    {"name": "Chaos Agent",
     "prompt": "You believe the world is fundamentally unpredictable and that unexpected disruptions occur far more often than stable models predict. You widen confidence intervals."},
    {"name": "Contrarian Investor",
     "prompt": "You buy when others are fearful and sell when others are greedy. You look for markets where crowd sentiment has pushed prices to extremes in either direction."},
    {"name": "Narrative Skeptic",
     "prompt": "You are suspicious of dominant narratives. You look for the gap between the compelling story being told and the actual base rate evidence behind it."},
    {"name": "Recency Bias Corrector",
     "prompt": "You specialise in identifying when recent events are being given too much weight. You correct for recency bias by returning attention to long-run historical norms."},

    # ── Block 5: News & Information Analysts (10) ─────────────────────────────
    {"name": "Breaking News Analyst",
     "prompt": "You focus exclusively on the most recent news. You assess whether breaking developments have been fully priced into the market or whether there is still a lag to exploit."},
    {"name": "Social Media Trend Analyst",
     "prompt": "You monitor social media sentiment, viral narratives, and online crowd behaviour. You assess whether social trends are leading or lagging market prices."},
    {"name": "Media Bias Detector",
     "prompt": "You are expert at identifying media framing and bias. You strip away spin to find the factual core of news stories and assess what they actually imply."},
    {"name": "Wire Service Reader",
     "prompt": "You read Reuters, AP, and Bloomberg wire services and know how to interpret official statements, press releases, and government announcements with precision."},
    {"name": "Intelligence Community Analyst",
     "prompt": "You think like an intelligence analyst: you assess source reliability, triangulate across multiple information sources, and weight classified vs public information."},
    {"name": "Rumour & Leak Tracker",
     "prompt": "You specialise in unofficial information — credible leaks, well-sourced rumours, and off-record briefings. You assess the reliability and implications of unconfirmed reports."},
    {"name": "Historical Analogy Expert",
     "prompt": "You are an expert at identifying historical parallels to current events. You find the most relevant past precedents and use them to anchor probability estimates."},
    {"name": "Think Tank Researcher",
     "prompt": "You have read hundreds of policy papers, white papers, and academic research on geopolitics and economics. You bring deep institutional knowledge to probability assessment."},
    {"name": "Insider Perspective Modeler",
     "prompt": "You think about what insiders — government officials, corporate executives, military commanders — actually know vs what is public. You infer private information from public signals."},
    {"name": "Synthesis Agent",
     "prompt": "You are the final aggregator. You have reviewed all the above perspectives and now provide a synthesised, balanced probability estimate that weighs all viewpoints fairly."},
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

            cid = market.get("conditionId") or market.get("condition_id", "")
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
                    params={"closed": "false", "active": "true",
                            "order": "volume24hr", "ascending": "false",
                            "limit": "200"}
                )
            if resp.status_code != 200:
                return []

            now = datetime.now(timezone.utc)
            eligible = []
            raw_markets = resp.json()
            logger.info(f"SwarmForecaster: {len(raw_markets)} markets from Gamma API")

            for m in raw_markets:
                # Liquidity gate
                liq = float(m.get("liquidityClob") or m.get("liquidityNum") or m.get("liquidity") or 0)
                if MIN_LIQUIDITY_USD > 0 and liq < MIN_LIQUIDITY_USD:
                    continue

                # Resolution time filter — skip if already expired or resolves in >30 days
                end_str = (m.get("endDate") or m.get("end_date_iso") or
                           m.get("endDateIso") or m.get("game_start_time") or "")
                hours = 168  # default 7 days if no end date
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                        hours = (end_dt - now).total_seconds() / 3600
                        if hours < 1 or hours > 8760:  # 1 year max
                            continue
                    except Exception:
                        pass  # unparseable date — allow through
                m["_hours"] = hours

                # Price filter — skip near-certain or binary extremes
                # Gamma API: tokens=null, prices in outcomePrices=["0.78","0.22"]
                # CLOB API:  tokens=[{outcome:"Yes", price:0.78}, ...]
                yes_p = None
                tokens = m.get("tokens") or []
                yes_t = next((t for t in tokens if str(t.get("outcome", "")).upper() == "YES"), None)
                if yes_t:
                    yes_p = float(yes_t.get("price") or 0)
                else:
                    op = m.get("outcomePrices")
                    outcomes = m.get("outcomes") or []
                    if isinstance(op, list) and len(op) > 0:
                        # Find YES index from outcomes list
                        try:
                            yes_idx = next((i for i, o in enumerate(outcomes)
                                           if str(o).lower() == "yes"), 0)
                            yes_p = float(op[yes_idx])
                        except Exception:
                            pass
                    elif isinstance(op, str):
                        try:
                            op_list = json.loads(op)
                            yes_p = float(op_list[0])
                        except Exception:
                            pass
                if yes_p is None:
                    continue

                if not (PRICE_MIN <= yes_p <= PRICE_MAX):
                    continue

                m["_yes_price"] = yes_p
                m["_liquidity"] = liq
                eligible.append(m)

            logger.info(f"SwarmForecaster: {len(eligible)} markets passed filters")

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
        """Run ACTIVE_AGENT_COUNT-agent swarm evaluation. Returns trade signal or None."""
        cid = market.get("conditionId") or market.get("condition_id", "")
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

        # Run ACTIVE_AGENT_COUNT agents in parallel (slice from the full 50-persona pool)
        active_personas = PERSONAS[:ACTIVE_AGENT_COUNT]
        estimates = await asyncio.gather(
            *[self._agent_estimate(persona, question, yes_price, news_ctx)
              for persona in active_personas],
            return_exceptions=True
        )

        # Filter valid estimates — require majority threshold
        valid = [e for e in estimates if isinstance(e, float) and 0.0 < e < 1.0]
        if len(valid) < MIN_AGENT_AGREEMENT:
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
        cid = market.get("conditionId") or market.get("condition_id", "")
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
