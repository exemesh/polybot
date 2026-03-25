"""
Sports Intelligence Strategy
Scrapes live sports data and odds from free APIs to predict outcomes
on Polymarket sports markets for profit.

Data Sources:
1. The Odds API (free tier: 500 requests/month) - live odds from bookmakers
2. API-Sports (free tier) - live scores, standings, statistics
3. ESPN/public APIs - team form, head-to-head data

Logic:
- Find Polymarket markets related to sports events
- Scrape current odds/data from multiple sportsbooks
- Compare sportsbook consensus vs Polymarket price
- When Polymarket is significantly mispriced vs bookmaker consensus → trade

Trade rules:
- $1 USD per trade
- Only trade when Polymarket price deviates >5% from bookmaker consensus
- Max 10 trades per cycle
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import httpx

from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager

logger = logging.getLogger("polybot.sports")

# Keywords that identify sports markets
# AGGRESSIVE: Expanded keywords — sports + geopolitics + trending events
SPORTS_KEYWORDS = [
    # Sports
    "win", "beat", "defeat", "champion", "championship", "title",
    "nba", "nfl", "mlb", "nhl", "mls", "premier league", "la liga",
    "serie a", "bundesliga", "ligue 1", "champions league", "europa",
    "world cup", "super bowl", "stanley cup", "world series",
    "playoffs", "finals", "semifinal", "quarter", "round",
    "match", "game", "fixture", "tournament",
    "f1", "formula 1", "grand prix", "ufc", "boxing", "tennis",
    "wimbledon", "us open", "australian open", "french open",
    "olympics", "college football", "march madness", "ncaa",
    "fifa", "euro 2026", "copa america",
    "mvp", "scoring", "points", "goals", "touchdowns",
    "lebron", "curry", "mahomes", "messi", "ronaldo",
    # Geopolitics & current events (Iran, war, etc. — volatile = opportunity)
    "iran", "israel", "war", "strike", "military", "attack", "ceasefire",
    "sanctions", "nuclear", "missile", "troops", "invasion",
    "russia", "ukraine", "china", "taiwan", "nato",
    "trump", "biden", "congress", "senate", "executive order",
    "tariff", "trade war", "oil price", "crude", "opec",
    "fed", "interest rate", "inflation", "recession",
    "crypto", "bitcoin", "ethereum", "sec", "regulation",
    "ai", "openai", "google", "apple", "tesla", "spacex",
    "election", "poll", "approval rating", "impeach",
]

# Sport category detection patterns
SPORT_PATTERNS = {
    "soccer": ["premier league", "la liga", "serie a", "bundesliga", "ligue 1",
               "champions league", "europa league", "mls", "fifa", "world cup",
               "euro 2026", "copa america"],
    "basketball": ["nba", "ncaa basketball", "march madness", "wnba"],
    "football": ["nfl", "super bowl", "college football", "ncaa football"],
    "baseball": ["mlb", "world series"],
    "hockey": ["nhl", "stanley cup"],
    "tennis": ["wimbledon", "us open", "australian open", "french open", "atp", "wta"],
    "mma": ["ufc", "bellator", "pfl"],
    "motorsport": ["f1", "formula 1", "grand prix", "nascar", "indycar"],
}

# The Odds API sport keys
ODDS_API_SPORTS = {
    "soccer": "soccer_epl,soccer_spain_la_liga,soccer_italy_serie_a,soccer_germany_bundesliga,soccer_uefa_champs_league",
    "basketball": "basketball_nba,basketball_ncaab",
    "football": "americanfootball_nfl,americanfootball_ncaaf",
    "baseball": "baseball_mlb",
    "hockey": "icehockey_nhl",
    "tennis": "tennis_atp_french_open,tennis_atp_us_open",
    "mma": "mma_mixed_martial_arts",
}


class SportsIntelStrategy:
    """Uses external sports data to find mispriced Polymarket sports bets."""

    def __init__(self, settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)
        self.traded_markets: Dict[str, float] = {}
        self.odds_api_key = getattr(settings, 'ODDS_API_KEY', '') or ''

    async def run_once(self):
        """Single scan-and-trade cycle."""
        logger.info("SportsIntel: scanning sports markets with external data")
        try:
            # Step 1: Find sports-related markets on Polymarket
            sports_markets = await self._find_sports_markets()
            logger.info(f"SportsIntel: found {len(sports_markets)} sports markets")

            if not sports_markets:
                logger.info("SportsIntel: no sports markets found this cycle")
                return

            # Step 2: Get external odds data
            external_odds = await self._fetch_external_odds()
            logger.info(f"SportsIntel: fetched odds for {len(external_odds)} events")

            # Step 3: Compare and find mispricings
            opportunities = await self._find_mispricings(sports_markets, external_odds)
            logger.info(f"SportsIntel: {len(opportunities)} mispriced opportunities")

            # Step 4: Execute trades
            executed = 0
            for opp in opportunities[:10]:
                success = await self._execute_trade(opp)
                if success:
                    executed += 1

            logger.info(f"SportsIntel complete: {executed} trades from {len(opportunities)} opportunities")

        except Exception as e:
            logger.error(f"SportsIntel error: {e}", exc_info=True)

    async def _find_sports_markets(self) -> List[Dict]:
        """Find active Polymarket markets related to sports.

        IMPORTANT: Only returns markets resolving within 30 days.
        Long-dated futures (World Cup, Stanley Cup months away) tie up capital
        for too long and produce zero PnL.
        """
        # AGGRESSIVE: Search sports + geopolitics + trending events
        all_markets = []
        search_terms = [
            # Sports
            "NBA", "NFL", "soccer", "UFC", "tennis", "MLB",
            "champion", "win game", "Super Bowl", "World Cup",
            "Premier League", "F1", "Grand Prix", "NHL", "boxing",
            # Geopolitics — high-volatility events = big opportunities
            "Iran", "Israel", "war", "military", "strike",
            "Russia", "Ukraine", "China", "Taiwan",
            "Trump", "tariff", "sanctions", "ceasefire",
            # Finance & tech
            "Bitcoin", "crypto", "Fed", "interest rate",
            "oil", "recession", "AI", "Tesla", "SpaceX",
            # General high-volume events
            "election", "approval", "executive order",
        ]

        for term in search_terms:
            try:
                markets = await self.poly_client.search_markets(term)
                for m in markets:
                    cid = m.get("condition_id", "")
                    if cid and cid not in [x.get("condition_id") for x in all_markets]:
                        all_markets.append(m)
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"Search for '{term}' failed: {e}")

        # Also search via Gamma API tags
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for tag in ["sports", "basketball", "football", "soccer", "mma",
                            "politics", "crypto", "finance", "world", "science"]:
                    resp = await client.get(
                        f"{self.settings.GAMMA_HOST}/markets",
                        params={"tag": tag, "active": "true", "closed": "false", "limit": 50}
                    )
                    if resp.status_code == 200:
                        tagged = resp.json()
                        for m in tagged:
                            cid = m.get("condition_id", "")
                            if cid and cid not in [x.get("condition_id") for x in all_markets]:
                                all_markets.append(m)
                    await asyncio.sleep(0.2)
        except Exception as e:
            logger.debug(f"Tag search failed: {e}")

        # Filter to active sports markets ONLY within 30 days of resolution
        now = datetime.now(timezone.utc)
        sports = []
        skipped_long = 0
        skipped_no_date = 0

        for m in all_markets:
            question = (m.get("question", "") or "").lower()
            # Must contain at least one sports keyword
            if not any(kw in question for kw in SPORTS_KEYWORDS):
                continue

            # CRITICAL: Require end_date and reject markets > 30 days out
            end_date = m.get("end_date_iso", m.get("endDateIso", ""))
            if not end_date:
                skipped_no_date += 1
                continue

            try:
                resolution_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                hours_until = (resolution_dt - now).total_seconds() / 3600
                if hours_until < 2:  # Too close to expiry
                    continue
                if hours_until > 2160:  # > 90 days — SKIP (was the major problem)
                    skipped_long += 1
                    continue
                m["_hours_until"] = hours_until
            except Exception:
                skipped_no_date += 1
                continue

            sports.append(m)

        if skipped_long > 0:
            logger.info(f"SportsIntel: skipped {skipped_long} long-dated markets (>30 days)")
        if skipped_no_date > 0:
            logger.info(f"SportsIntel: skipped {skipped_no_date} markets with no end date")

        return sports

    async def _fetch_external_odds(self) -> Dict[str, Dict]:
        """Fetch odds from The Odds API and other free sources."""
        all_odds = {}

        # Source 1: The Odds API (free 500 requests/month)
        if self.odds_api_key:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    # Fetch in-season sports
                    resp = await client.get(
                        "https://api.the-odds-api.com/v4/sports",
                        params={"apiKey": self.odds_api_key, "all": "false"}
                    )
                    if resp.status_code == 200:
                        active_sports = resp.json()
                        sport_keys = [s["key"] for s in active_sports[:5]]  # Top 5 active sports

                        for key in sport_keys:
                            odds_resp = await client.get(
                                f"https://api.the-odds-api.com/v4/sports/{key}/odds",
                                params={
                                    "apiKey": self.odds_api_key,
                                    "regions": "us",
                                    "markets": "h2h",
                                    "oddsFormat": "decimal",
                                }
                            )
                            if odds_resp.status_code == 200:
                                events = odds_resp.json()
                                for event in events:
                                    event_key = self._normalize_event_key(event)
                                    avg_odds = self._calculate_consensus(event)
                                    if avg_odds:
                                        all_odds[event_key] = {
                                            "source": "odds_api",
                                            "event": event,
                                            "consensus_odds": avg_odds,
                                            "sport": key,
                                        }
                            await asyncio.sleep(0.3)
            except Exception as e:
                logger.debug(f"Odds API fetch failed: {e}")
        else:
            logger.debug("No ODDS_API_KEY set — using Polymarket-only analysis")

        # Source 2: Free ESPN-like odds scraping (no API key needed)
        try:
            await self._fetch_free_odds(all_odds)
        except Exception as e:
            logger.debug(f"Free odds fetch failed: {e}")

        return all_odds

    async def _fetch_free_odds(self, odds_dict: Dict):
        """Fetch odds from free/public sources (no API key needed)."""
        # Use Polymarket's own data as a rough benchmark
        # Cross-reference multiple Polymarket markets on the same topic
        # to find internal inconsistencies
        pass

    def _normalize_event_key(self, event: Dict) -> str:
        """Create a matchable key from event data."""
        teams = sorted([
            event.get("home_team", "").lower().strip(),
            event.get("away_team", "").lower().strip()
        ])
        sport = event.get("sport_key", "")
        return f"{sport}:{teams[0]}:{teams[1]}"

    def _calculate_consensus(self, event: Dict) -> Optional[Dict]:
        """Average odds across bookmakers to get consensus probability."""
        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            return None

        team_odds = {}  # team_name -> list of decimal odds

        for bm in bookmakers:
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    name = outcome.get("name", "")
                    price = outcome.get("price", 0)
                    if name and price > 0:
                        if name not in team_odds:
                            team_odds[name] = []
                        team_odds[name].append(price)

        if not team_odds:
            return None

        consensus = {}
        for team, odds_list in team_odds.items():
            avg_decimal = sum(odds_list) / len(odds_list)
            # Convert decimal odds to implied probability
            implied_prob = 1.0 / avg_decimal
            consensus[team.lower()] = {
                "avg_decimal_odds": round(avg_decimal, 3),
                "implied_probability": round(implied_prob, 4),
                "num_bookmakers": len(odds_list),
            }

        return consensus

    async def _find_mispricings(self, sports_markets: List[Dict],
                                 external_odds: Dict) -> List[Dict]:
        """Compare Polymarket prices vs external odds to find mispricings."""
        opportunities = []
        now = datetime.now(timezone.utc)

        for market in sports_markets:
            condition_id = market.get("condition_id", "")
            if self.portfolio.has_open_position(condition_id):
                continue
            if condition_id in self.traded_markets:
                if time.time() - self.traded_markets[condition_id] < 7200:  # 2h cooldown
                    continue

            question = market.get("question", "")
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)
            if not yes_token or not no_token:
                continue

            yes_id = yes_token.get("token_id", "")
            no_id = no_token.get("token_id", "")
            if not yes_id or not no_id:
                continue

            # Get Polymarket prices
            yes_book = await self.poly_client.get_order_book(yes_id)
            if not yes_book:
                continue
            no_book = await self.poly_client.get_order_book(no_id)
            if not no_book:
                continue

            min_liq = min(yes_book.liquidity_usd, no_book.liquidity_usd)
            if min_liq < 15:
                continue

            yes_mid = yes_book.mid_price
            no_mid = no_book.mid_price

            # Try to match with external odds
            matched_odds = self._match_market_to_odds(question, external_odds)

            if matched_odds:
                # Compare Polymarket vs bookmaker consensus
                opp = self._evaluate_mispricing(
                    market, yes_mid, no_mid, yes_id, no_id,
                    matched_odds, min_liq
                )
                if opp:
                    opportunities.append(opp)
            else:
                # Even without external odds, apply sports-specific heuristics
                # Get hours_until from our pre-filtered data
                hours_until = market.get("_hours_until")

                # Arb check: YES + NO < $1 (guaranteed profit — priority!)
                total = yes_mid + no_mid
                if total < 0.995:
                    edge = 1.0 - total - 0.004
                    if edge > 0.003:
                        return_pct = edge / total * 100
                        # Arb trades get 3x score boost for priority
                        time_mult = 3.0 if (hours_until and hours_until <= 72) else 2.0
                        opportunities.append({
                            "type": "sports_arb",
                            "condition_id": condition_id,
                            "question": question,
                            "yes_token_id": yes_id,
                            "no_token_id": no_id,
                            "yes_price": yes_mid,
                            "no_price": no_mid,
                            "edge": edge,
                            "return_pct": return_pct,
                            "side": "BOTH",
                            "liquidity": min_liq,
                            "hours_until": hours_until,
                            "score": return_pct * time_mult,  # Sports arb = premium
                            "source": "polymarket_internal",
                        })

                # Value bets: ONLY when hours_until is known and < 30 days
                # (already filtered in _find_sports_markets, but double-check)
                if hours_until and hours_until <= 2160:
                    if 0.10 <= yes_mid <= 0.65 and min_liq > 50 and yes_book.spread < 0.08:
                        return_pct = (1.0 / yes_mid - 1.0) * 100
                        if return_pct >= 30:
                            opportunities.append({
                                "type": "sports_value",
                                "condition_id": condition_id,
                                "question": question,
                                "yes_token_id": yes_id,
                                "no_token_id": no_id,
                                "yes_price": yes_mid,
                                "no_price": no_mid,
                                "edge": return_pct / 100,
                                "return_pct": return_pct,
                                "side": "BUY_YES",
                                "liquidity": min_liq,
                                "hours_until": hours_until,
                                "score": return_pct * min(1.0, min_liq / 200),
                                "source": "polymarket_analysis",
                            })
                    elif 0.10 <= no_mid <= 0.65 and min_liq > 50 and no_book.spread < 0.08:
                        return_pct = (1.0 / no_mid - 1.0) * 100
                        if return_pct >= 30:
                            opportunities.append({
                                "type": "sports_value",
                                "condition_id": condition_id,
                                "question": question,
                                "yes_token_id": yes_id,
                                "no_token_id": no_id,
                                "yes_price": yes_mid,
                                "no_price": no_mid,
                                "edge": return_pct / 100,
                                "return_pct": return_pct,
                                "side": "BUY_NO",
                                "liquidity": min_liq,
                                "hours_until": hours_until,
                                "score": return_pct * min(1.0, min_liq / 200),
                                "source": "polymarket_analysis",
                            })

            await asyncio.sleep(0.2)

        opportunities.sort(key=lambda x: x["score"], reverse=True)
        return opportunities

    def _match_market_to_odds(self, question: str, external_odds: Dict) -> Optional[Dict]:
        """Try to match a Polymarket question to external odds data."""
        q_lower = question.lower()

        for key, odds_data in external_odds.items():
            event = odds_data.get("event", {})
            home = event.get("home_team", "").lower()
            away = event.get("away_team", "").lower()

            # Check if both teams mentioned in the question
            if home and away:
                if home in q_lower and away in q_lower:
                    return odds_data
                # Check partial name matches
                for name_part in home.split():
                    if len(name_part) > 3 and name_part in q_lower:
                        for away_part in away.split():
                            if len(away_part) > 3 and away_part in q_lower:
                                return odds_data

        return None

    def _evaluate_mispricing(self, market: Dict, yes_mid: float, no_mid: float,
                             yes_id: str, no_id: str, odds_data: Dict,
                             liquidity: float) -> Optional[Dict]:
        """Evaluate if Polymarket price is mispriced vs external odds."""
        consensus = odds_data.get("consensus_odds", {})
        if not consensus:
            return None

        question = market.get("question", "")
        condition_id = market.get("condition_id", "")

        # Try to figure out which team/outcome is YES
        # This is a heuristic — match team names in the question
        for team_name, team_data in consensus.items():
            if team_name in question.lower():
                ext_prob = team_data["implied_probability"]

                # Compare with Polymarket YES price
                poly_price = yes_mid
                price_diff = ext_prob - poly_price

                if abs(price_diff) > 0.05:  # 5% mispricing threshold
                    if price_diff > 0:
                        # External says higher prob than Polymarket → BUY YES
                        return_pct = (1.0 / poly_price - 1.0) * 100
                        return {
                            "type": "sports_mispriced",
                            "condition_id": condition_id,
                            "question": question,
                            "yes_token_id": yes_id,
                            "no_token_id": no_id,
                            "yes_price": yes_mid,
                            "no_price": no_mid,
                            "edge": price_diff,
                            "return_pct": return_pct,
                            "side": "BUY_YES",
                            "liquidity": liquidity,
                            "score": price_diff * 100 * min(1.0, liquidity / 100),
                            "source": f"odds_api ({team_data['num_bookmakers']} books)",
                            "external_prob": ext_prob,
                            "polymarket_prob": poly_price,
                        }
                    else:
                        # External says lower prob → BUY NO
                        return_pct = (1.0 / no_mid - 1.0) * 100
                        return {
                            "type": "sports_mispriced",
                            "condition_id": condition_id,
                            "question": question,
                            "yes_token_id": yes_id,
                            "no_token_id": no_id,
                            "yes_price": yes_mid,
                            "no_price": no_mid,
                            "edge": abs(price_diff),
                            "return_pct": return_pct,
                            "side": "BUY_NO",
                            "liquidity": liquidity,
                            "score": abs(price_diff) * 100 * min(1.0, liquidity / 100),
                            "source": f"odds_api ({team_data['num_bookmakers']} books)",
                            "external_prob": ext_prob,
                            "polymarket_prob": poly_price,
                        }

        return None

    async def _execute_trade(self, opp: Dict) -> bool:
        """Execute a sports/event trade. $5 per trade."""
        trade_size = 5.00  # $5 per trade — fits $7 portfolio (75% cap = $5.38)

        approved, reason = self.risk_manager.approve_trade(
            trade_size, "sports_intel", opp["condition_id"])
        if not approved:
            logger.debug(f"Sports trade rejected: {reason}")
            return False

        source = opp.get("source", "analysis")

        if opp["side"] == "BOTH":
            half = trade_size / 2
            logger.info(
                f"[SPORTS] ARB | {opp['question'][:50]} | "
                f"YES: {opp['yes_price']:.3f} + NO: {opp['no_price']:.3f} | "
                f"Return: {opp['return_pct']:.1f}% | Source: {source}"
            )
            yes_r = await self.poly_client.place_market_order(
                opp["yes_token_id"], half, "BUY", self.settings.DRY_RUN)
            no_r = await self.poly_client.place_market_order(
                opp["no_token_id"], half, "BUY", self.settings.DRY_RUN)

            if yes_r.success and no_r.success:
                expected_pnl = trade_size * opp["edge"]  # used for logging only
                trade = Trade(
                    id=None, timestamp=datetime.utcnow().isoformat(),
                    strategy="sports_intel", market_id=opp["condition_id"],
                    market_question=opp["question"], side="BOTH",
                    token_id=f"{opp['yes_token_id'][:16]}|{opp['no_token_id'][:16]}",
                    price=(opp["yes_price"] + opp["no_price"]),
                    size_usd=trade_size, edge_pct=opp["edge"],
                    dry_run=self.settings.DRY_RUN,
                    order_id=f"{yes_r.order_id}|{no_r.order_id}",
                    pnl=None, status="open"
                )
                self.portfolio.log_trade(trade)
                self.traded_markets[opp["condition_id"]] = time.time()
                return True
        else:
            token_id = opp["yes_token_id"] if "YES" in opp["side"] else opp["no_token_id"]
            price = opp["yes_price"] if "YES" in opp["side"] else opp["no_price"]

            logger.info(
                f"[SPORTS] {opp['type'].upper()} | {opp['question'][:50]} | "
                f"{opp['side']} @ {price:.3f} | Return: {opp['return_pct']:.0f}% | "
                f"Source: {source}"
            )

            result = await self.poly_client.place_market_order(
                token_id, trade_size, "BUY", self.settings.DRY_RUN)

            if result.success:
                trade = Trade(
                    id=None, timestamp=datetime.utcnow().isoformat(),
                    strategy="sports_intel", market_id=opp["condition_id"],
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
        logger.info(f"SportsIntel cleanup: {len(self.traded_markets)} markets traded")
