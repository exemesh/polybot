"""
NewsArbitrageStrategy — Time-zone news latency arbitrage for Polymarket.

Strategy: Monitor international RSS feeds (Japanese gov, EU Parliament, Reuters, BBC)
for outcomes that resolve Polymarket markets before US traders react.

Best performance window: 02:00-06:00 EST (07:00-11:00 WAT) when US volume is low.
Seeks 15%+ edge on markets resolving within 7 days.

Inspired by: Polymarket traders who turned $12k→$45k exploiting timezone latency
on Japanese government RSS feeds and EU Parliament livestreams.
"""

import asyncio
import re
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

import httpx

from config.settings import Settings
from core.portfolio import Portfolio
from core.risk_manager import RiskManager
from core.polymarket_client import PolymarketClient
from core.portfolio import Trade

logger = logging.getLogger("polybot.news_arb")

# ─── RSS Feeds ────────────────────────────────────────────────────────────────
RSS_FEEDS = [
    {"name": "NHK World Politics", "url": "https://www3.nhk.or.jp/rss/news/cat6.xml",                        "region": "JP"},
    {"name": "NHK World Top",      "url": "https://www3.nhk.or.jp/rss/news/cat0.xml",                        "region": "JP"},
    {"name": "EU Parliament",      "url": "https://www.europarl.europa.eu/rss/doc/rss-newsroom-en.xml",       "region": "EU"},
    {"name": "BBC World",          "url": "http://feeds.bbci.co.uk/news/world/rss.xml",                       "region": "UK"},
    {"name": "Reuters World",      "url": "https://feeds.reuters.com/reuters/worldNews",                      "region": "GLOBAL"},
    {"name": "Al Jazeera",         "url": "https://www.aljazeera.com/xml/rss/all.xml",                        "region": "GLOBAL"},
]

YES_SIGNALS = [
    "signed", "passed", "approved", "won", "confirmed", "ceasefire", "deal", "agreement",
    "elected", "victory", "successful", "reached", "agreed", "ratified", "enacted",
    "completed", "achieved", "declared", "announced", "official", "resigned", "fired",
    "removed", "stepped down", "ousted", "arrested", "indicted", "convicted", "sentenced",
    "withdrawn", "withdrawal", "surrendered", "ended", "concluded", "resolved",
]

NO_SIGNALS = [
    "failed", "rejected", "denied", "lost", "cancelled", "collapse", "broke down",
    "walked away", "no deal", "vetoed", "blocked", "defeated", "suspended",
    "postponed", "delayed", "refused", "opposed", "stalled", "deadlock", "resumed",
    "continuing", "ongoing", "escalated", "worsened",
]

MIN_NEWS_EDGE = 0.15
MAX_NEWS_AGE_HOURS = 2
TRADE_SIZE_USD = 15.0
MAX_TRADES_PER_CYCLE = 3
MARKET_MAX_HOURS = 168


class NewsArbitrageStrategy:
    """
    Monitors international RSS feeds for breaking news that resolves
    Polymarket markets before US traders react. Exploits time-zone latency.
    """

    def __init__(self, settings: Settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)
        self.traded_markets: Dict[str, float] = {}
        self._seen_headlines: set = set()

    async def run_once(self, open_token_ids=None) -> None:
        """Single news scan and trade cycle."""
        logger.info("NewsArb: scanning international feeds")

        markets = await self._get_active_markets()
        if not markets:
            logger.info("NewsArb: no active markets found")
            return

        news_items = await self._fetch_all_feeds()
        if not news_items:
            logger.info("NewsArb: no news items fetched this cycle")
            return

        logger.info(f"NewsArb: {len(news_items)} news items vs {len(markets)} markets")

        opportunities = self._find_opportunities(markets, news_items)
        if not opportunities:
            logger.info("NewsArb: no opportunities this cycle")
            return

        logger.info(f"NewsArb: {len(opportunities)} opportunities found")

        trades = 0
        for opp in opportunities[:MAX_TRADES_PER_CYCLE]:
            if await self._execute_trade(opp):
                trades += 1

        logger.info(f"NewsArb complete: {trades} trades from {len(opportunities)} opportunities")

    async def _get_active_markets(self) -> List[Dict]:
        """Fetch open markets resolving within 7 days, sorted by urgency."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self.settings.GAMMA_HOST}/markets",
                    params={"closed": "false", "order": "volume24hr",
                            "ascending": "false", "limit": "100"}
                )
            if resp.status_code != 200:
                logger.warning(f"NewsArb: Gamma API {resp.status_code}")
                return []

            now = datetime.now(timezone.utc)
            filtered = []
            for m in resp.json():
                end_str = m.get("endDate") or m.get("end_date_iso") or m.get("end_date")
                if not end_str:
                    continue
                try:
                    end_str = end_str.rstrip("Z")
                    if "T" in end_str:
                        end_dt = datetime.fromisoformat(end_str).replace(tzinfo=timezone.utc)
                    else:
                        end_dt = datetime.strptime(end_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    hours = (end_dt - now).total_seconds() / 3600
                    if 1 < hours <= MARKET_MAX_HOURS:
                        m["_hours_until"] = hours
                        filtered.append(m)
                except (ValueError, TypeError):
                    continue

            filtered.sort(key=lambda x: (x["_hours_until"], -float(x.get("volume24hr") or 0)))
            return filtered[:60]
        except Exception as e:
            logger.error(f"NewsArb: market fetch error: {e}")
            return []

    async def _fetch_all_feeds(self) -> List[Dict]:
        """Fetch all RSS feeds concurrently."""
        results = await asyncio.gather(
            *[self._fetch_feed(f) for f in RSS_FEEDS], return_exceptions=True
        )
        items = []
        for r in results:
            if isinstance(r, list):
                items.extend(r)
        return items

    async def _fetch_feed(self, feed: Dict) -> List[Dict]:
        """Fetch and parse a single RSS feed."""
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(
                    feed["url"],
                    headers={"User-Agent": "Mozilla/5.0 (compatible; PolyBot/1.0)"}
                )
            if resp.status_code != 200:
                return []
            return self._parse_rss(resp.text, feed["name"], feed["region"])
        except Exception as e:
            logger.debug(f"NewsArb: {feed['name']} fetch failed: {e}")
            return []

    def _parse_rss(self, xml_text: str, source: str, region: str) -> List[Dict]:
        """Parse RSS/Atom XML into item dicts."""
        items = []
        try:
            root = ET.fromstring(xml_text)
            channel = root.find("channel")
            if channel is not None:
                entries = channel.findall("item")
            else:
                entries = root.findall("{http://www.w3.org/2005/Atom}entry")

            for entry in entries[:20]:
                title = (entry.findtext("title") or
                         getattr(entry.find("{http://www.w3.org/2005/Atom}title"), "text", "") or "").strip()
                desc  = (entry.findtext("description") or
                         getattr(entry.find("{http://www.w3.org/2005/Atom}summary"), "text", "") or "").strip()

                key = title[:80]
                if not title or key in self._seen_headlines:
                    continue

                items.append({
                    "title":  title,
                    "desc":   desc[:300],
                    "source": source,
                    "region": region,
                    "text":   f"{title} {desc}".lower(),
                    "_key":   key,
                })
        except ET.ParseError as e:
            logger.debug(f"NewsArb: XML parse error ({source}): {e}")
        return items

    def _find_opportunities(self, markets: List[Dict], news: List[Dict]) -> List[Dict]:
        """Match news to markets and score trading opportunities."""
        opps = []
        for market in markets:
            cid = market.get("condition_id", "")
            if not cid:
                continue
            if self.portfolio.has_open_position(cid):
                continue
            if cid in self.traded_markets and time.time() - self.traded_markets[cid] < 7200:
                continue

            tokens = market.get("tokens", [])
            yes_tok = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_tok  = next((t for t in tokens if t.get("outcome", "").upper() == "NO"),  None)
            if not yes_tok or not no_tok:
                continue

            yes_price = float(yes_tok.get("price") or 0.5)
            no_price  = float(no_tok.get("price")  or 0.5)
            if yes_price <= 0.01 or yes_price >= 0.99:
                continue

            keywords = self._extract_keywords(market.get("question", ""))
            match = self._best_match(keywords, news)
            if not match:
                continue

            item, score, direction = match
            if score < 0.25:
                continue

            if direction == "YES":
                token_id  = yes_tok.get("token_id", "")
                cur_price = yes_price
            else:
                token_id  = no_tok.get("token_id", "")
                cur_price = no_price

            implied = min(0.95, cur_price + score * 0.4)
            edge = implied - cur_price
            if edge < MIN_NEWS_EDGE:
                continue

            opps.append({
                "condition_id":  cid,
                "question":      market.get("question", ""),
                "token_id":      token_id,
                "yes_token_id":  yes_tok.get("token_id", ""),
                "no_token_id":   no_tok.get("token_id", ""),
                "side":          direction,
                "current_price": cur_price,
                "implied_prob":  implied,
                "edge":          edge,
                "return_pct":    (edge / cur_price) * 100,
                "match_score":   score,
                "news_title":    item["title"],
                "news_source":   item["source"],
                "hours_until":   market.get("_hours_until", 999),
                "neg_risk":      market.get("negRisk", False),
                "score":         score * edge * 100 / max(1.0, market.get("_hours_until", 48)),
            })

        opps.sort(key=lambda x: x["score"], reverse=True)
        return opps

    def _extract_keywords(self, question: str) -> List[str]:
        """Extract meaningful keywords from a market question."""
        stop = {
            "will", "the", "a", "an", "in", "on", "at", "to", "for", "of", "and",
            "or", "by", "be", "is", "are", "was", "were", "have", "has", "had",
            "do", "does", "did", "this", "that", "with", "from", "as", "it", "its",
            "than", "after", "before", "any", "more", "least", "most", "over",
            "under", "between", "into", "through",
        }
        words = [w.lower() for w in re.findall(r'\b[A-Za-z]{3,}\b', question)
                 if w.lower() not in stop]
        named = [n.lower() for n in re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', question)
                 if n.lower() not in stop]
        return list(set(words + named))

    def _best_match(self, keywords: List[str], news: List[Dict]) -> Optional[Tuple[Dict, float, str]]:
        """Find best matching news item and determine trade direction."""
        best_item, best_score, best_dir = None, 0.0, None

        for item in news:
            text = item["text"]
            matches = sum(1 for kw in keywords if kw in text)
            if matches == 0:
                continue

            base  = min(1.0, matches / max(3, len(keywords) * 0.4))
            yes_n = sum(1 for s in YES_SIGNALS if s in text)
            no_n  = sum(1 for s in NO_SIGNALS  if s in text)

            if yes_n == 0 and no_n == 0:
                continue

            if yes_n >= no_n:
                direction = "YES"
                conf = yes_n / (yes_n + no_n)
            else:
                direction = "NO"
                conf = no_n / (yes_n + no_n)

            score = base * conf
            if score > best_score and score > 0.25:
                best_score, best_item, best_dir = score, item, direction

        return (best_item, best_score, best_dir) if best_item else None

    async def _execute_trade(self, opp: Dict) -> bool:
        """Execute a news arbitrage trade."""
        approved, reason = self.risk_manager.approve_trade(
            TRADE_SIZE_USD, "news_arb", opp["condition_id"]
        )
        if not approved:
            logger.debug(f"NewsArb rejected: {reason}")
            return False

        logger.info(
            f"[NEWS ARB] {opp['question'][:55]} | {opp['side']} @ {opp['current_price']:.3f} "
            f"| Edge: {opp['edge']:.1%} | Score: {opp['match_score']:.2f} "
            f"| {opp['news_source']}: \"{opp['news_title'][:50]}\""
        )

        result = await self.poly_client.place_market_order(
            opp["token_id"], TRADE_SIZE_USD, "BUY",
            self.settings.DRY_RUN, neg_risk=opp.get("neg_risk", False)
        )

        if result.success:
            self._seen_headlines.add(opp["news_title"][:80])
            trade = Trade(
                id=None,
                timestamp=datetime.utcnow().isoformat(),
                strategy="news_arb",
                market_id=opp["condition_id"],
                market_question=opp["question"],
                side=opp["side"],
                token_id=opp["token_id"],
                price=opp["current_price"],
                size_usd=TRADE_SIZE_USD,
                edge_pct=opp["edge"],
                dry_run=self.settings.DRY_RUN,
                order_id=result.order_id,
                pnl=None,
                status="open",
            )
            self.portfolio.log_trade(trade)
            self.traded_markets[opp["condition_id"]] = time.time()
            logger.info(f"[NEWS ARB] ✅ Order placed: {result.order_id}")
            return True
        else:
            logger.warning(f"[NEWS ARB] ❌ Order failed: {result.error}")
            return False
