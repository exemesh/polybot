"""
P1: Sentiment Spike Rider (Polymarket)

Reacts instantly to breaking news:
1. Polls high-speed RSS feeds (Reuters, AP, BBC, CNN, FT)
2. Scores each headline for sentiment strength + direction
3. Matches headlines to open Polymarket markets via keyword overlap
4. Emits Signal objects for MetaAgent aggregation

Design: fast-in, fast-out — only trade on strong (≥0.4 strength) signals.
        Positions expected to resolve within 1-3 cycles as price catches up.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path

import httpx

from core.meta_agent import Signal

logger = logging.getLogger("polybot.p1_sentiment_spike")

# ── RSS feeds (ordered by speed/reliability) ─────────────────────────────────
FEEDS = [
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.reuters.com/Reuters/PoliticsNews",
    "https://rss.ap.org/rss/apf-topnews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.cnn.com/rss/edition.rss",
    "https://www.ft.com/?format=rss",
]

# Headline sentiment keywords
BULLISH_KW = {
    "confirmed", "approved", "signed", "elected", "won", "wins", "victory",
    "deal", "agreement", "passes", "higher", "increase", "surge", "record",
    "breakthrough", "positive", "announces", "ceasefire", "resolved",
}
BEARISH_KW = {
    "denied", "rejected", "failed", "loses", "lost", "collapse", "crash",
    "ban", "sanction", "lower", "decline", "drops", "miss", "negative",
    "veto", "blocked", "shutdown", "indicted", "arrested", "killed",
}

# Only use news from last 20 minutes (price impact window)
MAX_AGE_SECONDS    = 1200
MIN_SIGNAL_STRENGTH = 0.35     # Only strong sentiment
MIN_EDGE           = 0.08      # 8% min edge — fast strategy needs clear mispricing
DEFAULT_SIZE_USD   = 8.0       # Smaller size — fast entries carry more uncertainty
MAX_SIGNALS        = 3

_SEEN_PATH = Path("data/p1_seen_headlines.json")
_seen_hashes: set[str] = set()


def _load_seen():
    global _seen_hashes
    if _SEEN_PATH.exists():
        try:
            _seen_hashes = set(json.loads(_SEEN_PATH.read_text()))
        except Exception:
            _seen_hashes = set()


def _save_seen():
    recent = list(_seen_hashes)[-500:]
    try:
        _SEEN_PATH.write_text(json.dumps(recent))
    except Exception:
        pass


def _headline_hash(text: str) -> str:
    return hashlib.md5(text.lower().encode()).hexdigest()[:12]


class P1SentimentSpikeRider:
    """
    Monitors RSS feeds for breaking news and emits Signal objects
    for MetaAgent when strong sentiment coincides with market mispricing.
    """

    def __init__(self, settings, portfolio, risk_manager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        _load_seen()

    async def scan(self, open_token_ids: set = None) -> list[Signal]:
        """Fetch breaking news, match to markets, return Signals."""
        open_token_ids = open_token_ids or set()
        signals: list[Signal] = []

        news = await self._fetch_news()
        if not news:
            return signals

        markets = await self._fetch_markets()
        if not markets:
            return signals

        for item in news:
            headline = item["title"]
            h = _headline_hash(headline)
            if h in _seen_hashes:
                continue
            _seen_hashes.add(h)

            side, strength = self._score_sentiment(headline)
            if strength < MIN_SIGNAL_STRENGTH:
                continue

            matched = self._match_markets(headline, markets)
            for market in matched[:2]:
                token_id = (
                    market.get("yes_token_id", "")
                    if side == "YES"
                    else market.get("no_token_id", "")
                )
                if not token_id or token_id in open_token_ids:
                    continue

                yes_price = self._get_yes_mid(market)
                if yes_price <= 0.05 or yes_price >= 0.95:
                    continue

                market_price = yes_price if side == "YES" else 1.0 - yes_price

                # Estimate: news not yet fully priced in → push probability
                push = strength * 0.25
                agent_prob = (
                    min(0.95, market_price + push)
                    if side == "YES"
                    else min(0.95, market_price + push)
                )

                edge = abs(agent_prob - market_price) - 0.01  # deduct fee
                if edge < MIN_EDGE:
                    continue

                sig = Signal(
                    agent_name="P1_SentimentSpike",
                    market_id=market.get("conditionId", market.get("condition_id", "")),
                    market_question=market.get("question", ""),
                    token_id=token_id,
                    side=side,
                    agent_probability=round(agent_prob, 3),
                    market_price=round(market_price, 3),
                    confidence=min(0.9, strength),
                    size_usd=DEFAULT_SIZE_USD,
                    metadata={
                        "headline": headline[:100],
                        "source": item.get("source", ""),
                        "strength": strength,
                    },
                )
                signals.append(sig)
                logger.info(
                    f"P1: [{side}] '{market.get('question','')[:50]}' "
                    f"edge={edge:.1%} headline='{headline[:60]}'"
                )

            if len(signals) >= MAX_SIGNALS:
                break

        _save_seen()
        logger.info(f"P1 scan: {len(news)} headlines → {len(signals)} signals")
        return signals

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _score_sentiment(self, headline: str) -> tuple[str, float]:
        """Returns (side='YES'|'NO', strength=0-1)."""
        lower = headline.lower()
        bull = sum(1 for kw in BULLISH_KW if kw in lower)
        bear = sum(1 for kw in BEARISH_KW if kw in lower)
        total = bull + bear
        if total == 0:
            return "YES", 0.0
        if bull >= bear:
            return "YES", min(1.0, bull / total * 1.5)
        return "NO", min(1.0, bear / total * 1.5)

    def _match_markets(self, headline: str, markets: list[dict]) -> list[dict]:
        """Keyword overlap matching between headline and market questions."""
        h_words = set(re.findall(r'\b\w{4,}\b', headline.lower()))
        scored = []
        for m in markets:
            q_words = set(re.findall(r'\b\w{4,}\b', m.get("question", "").lower()))
            overlap = len(h_words & q_words)
            if overlap >= 2:
                scored.append((overlap, m))
        scored.sort(reverse=True)
        return [m for _, m in scored[:3]]

    def _get_yes_mid(self, market: dict) -> float:
        try:
            bid = float(market.get("bestBid", market.get("best_bid", 0.4)) or 0.4)
            ask = float(market.get("bestAsk", market.get("best_ask", 0.6)) or 0.6)
            return max(0.01, min(0.99, (bid + ask) / 2.0))
        except (TypeError, ValueError):
            return 0.5

    async def _fetch_news(self) -> list[dict]:
        """Fetch RSS feeds in parallel, return recent headlines."""
        now = time.time()
        async with httpx.AsyncClient(timeout=8.0) as client:
            results = await asyncio.gather(
                *[self._fetch_feed(client, url) for url in FEEDS],
                return_exceptions=True,
            )
        items = []
        for r in results:
            if not isinstance(r, Exception):
                items.extend(r)
        recent = [i for i in items if now - i.get("ts", 0) < MAX_AGE_SECONDS]
        recent.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return recent[:40]

    async def _fetch_feed(self, client: httpx.AsyncClient, url: str) -> list[dict]:
        try:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
            if resp.status_code != 200:
                return []
            text = resp.text
            items = []
            title_re = re.compile(
                r'<title[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>', re.DOTALL
            )
            pubdate_re = re.compile(r'<pubDate>(.*?)</pubDate>', re.DOTALL)
            titles    = [re.sub(r'<[^>]+>', '', m.group(1)).strip() for m in title_re.finditer(text)]
            pub_dates = pubdate_re.findall(text)
            domain    = url.split("/")[2]
            for i, title in enumerate(titles[1:]):   # skip feed title
                if not title:
                    continue
                ts = time.time()
                if i < len(pub_dates):
                    try:
                        from email.utils import parsedate_to_datetime
                        ts = parsedate_to_datetime(pub_dates[i].strip()).timestamp()
                    except Exception:
                        pass
                items.append({"title": title, "ts": ts, "source": domain})
            return items
        except Exception as e:
            logger.debug(f"P1: feed error {url}: {e}")
            return []

    async def _fetch_markets(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"closed": "false", "active": "true",
                            "limit": 100, "order": "volume24hr", "ascending": "false"},
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.warning(f"P1: markets fetch failed: {e}")
        return []

    async def cleanup(self):
        pass
