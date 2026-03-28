"""
Weather Arbitrage Strategy
Compares Open-Meteo/NOAA forecast probabilities against Polymarket weather market prices.
Buys underpriced temperature buckets when forecast confidence > market price.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Tuple

import httpx

from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager

logger = logging.getLogger("polybot.weather")

TEMP_BUCKET_REGEX = re.compile(r"([-\d]+)\s*(?:to|-)\s*([-\d]+)\s*[°]?[FC]?", re.IGNORECASE)
CITY_PATTERNS = {
    "New York": ["new york", "nyc", "new york city"],
    "London": ["london"],
    "Chicago": ["chicago"],
    "Seoul": ["seoul"],
    "Sydney": ["sydney"],
    "Dallas": ["dallas"],
    "Miami": ["miami"],
    "Seattle": ["seattle"],
    "Atlanta": ["atlanta"],
    "Buenos Aires": ["buenos aires", "ba temperature"],
}


class WeatherForecast:
    BASE_URL = "https://api.open-meteo.com/v1/forecast"

    async def get_forecast(self, lat: float, lon: float) -> Optional[Dict]:
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,precipitation_probability,weathercode",
            "temperature_unit": "fahrenheit",
            "forecast_days": 3,
            "timezone": "auto",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(self.BASE_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                return self._process_forecast(data)
        except Exception as e:
            logger.error(f"Forecast fetch failed ({lat},{lon}): {e}")
            return None

    def _process_forecast(self, raw: Dict) -> Dict:
        hourly = raw.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        precip_prob = hourly.get("precipitation_probability", [])

        processed = []
        for i, (t, temp) in enumerate(zip(times, temps)):
            if temp is None:
                continue
            dt = datetime.fromisoformat(t)
            processed.append({
                "datetime": dt,
                "temp_f": round(temp, 1),
                "precip_prob": precip_prob[i] if i < len(precip_prob) else None,
            })

        daily = {}
        for entry in processed:
            day_key = entry["datetime"].date().isoformat()
            if day_key not in daily:
                daily[day_key] = {"highs": [], "lows": [], "entries": []}
            daily[day_key]["highs"].append(entry["temp_f"])
            daily[day_key]["lows"].append(entry["temp_f"])
            daily[day_key]["entries"].append(entry)

        daily_summary = {}
        for day, vals in daily.items():
            daily_summary[day] = {
                "high_f": max(vals["highs"]),
                "low_f": min(vals["lows"]),
                "hourly": vals["entries"]
            }

        return {
            "hourly": processed,
            "daily": daily_summary,
            "timezone": raw.get("timezone", "UTC")
        }

    def get_high_probability(self, forecast: Dict, target_date: str) -> Tuple[float, float]:
        if target_date not in forecast["daily"]:
            return None, 0.0

        day_data = forecast["daily"][target_date]
        predicted_high = day_data["high_f"]

        days_out = (datetime.fromisoformat(target_date) - datetime.now()).days
        if days_out <= 1:
            confidence = 0.88
        elif days_out <= 2:
            confidence = 0.82
        else:
            confidence = 0.70

        return predicted_high, confidence


class WeatherArbStrategy:
    def __init__(self, settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)
        self.weather_api = WeatherForecast()
        self.active_positions: Dict[str, Dict] = {}

    async def run(self):
        logger.info("WeatherArbStrategy started")
        scan_count = 0
        while True:
            try:
                scan_count += 1
                logger.debug(f"Weather scan #{scan_count}")
                opportunities = await self._scan_opportunities()
                for opp in opportunities:
                    await self._execute_opportunity(opp)
                await asyncio.sleep(self.settings.WEATHER_SCAN_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Weather strategy error: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def run_once(self, open_token_ids=None):
        """Single scan-and-trade cycle."""
        logger.info("WeatherArbStrategy: running single scan")
        try:
            opportunities = await self._scan_opportunities()
            for opp in opportunities:
                await self._execute_opportunity(opp)
            logger.info(f"Weather scan complete: {len(opportunities)} opportunities found")
        except Exception as e:
            logger.error(f"Weather strategy error: {e}", exc_info=True)

    async def _scan_opportunities(self) -> List[Dict]:
        opportunities = []
        weather_markets = await self.poly_client.search_markets("temperature high")
        weather_markets += await self.poly_client.search_markets("weather forecast")

        logger.debug(f"Found {len(weather_markets)} weather markets to analyze")

        for market in weather_markets:
            opp = await self._analyze_market(market)
            if opp:
                opportunities.append(opp)

        opportunities.sort(key=lambda x: x["edge"], reverse=True)
        logger.info(f"Weather scan: {len(opportunities)} opportunities found")
        return opportunities[:5]

    async def _analyze_market(self, market: Dict) -> Optional[Dict]:
        question = market.get("question", "")
        if not question:
            return None

        city_config = self._match_city(question)
        if not city_config:
            return None

        bucket = self._parse_temp_bucket(question)
        if not bucket:
            return None

        low_f, high_f = bucket

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

        if order_book.liquidity_usd < self.settings.WEATHER_MIN_LIQUIDITY:
            return None

        market_price = order_book.mid_price

        forecast = await self.weather_api.get_forecast(city_config["lat"], city_config["lon"])
        if not forecast:
            return None

        end_date = market.get("end_date_iso", "")
        if not end_date:
            return None

        try:
            resolution_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            target_date = resolution_dt.date().isoformat()
            hours_until = (resolution_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        except Exception:
            return None

        if hours_until > self.settings.WEATHER_MAX_HOURS_OUT:
            return None
        if hours_until < self.settings.WEATHER_MIN_HOURS_OUT:
            logger.debug(f"Skipping {question[:40]}: resolves in {hours_until:.0f}h (min {self.settings.WEATHER_MIN_HOURS_OUT}h)")
            return None

        predicted_high, confidence = self.weather_api.get_high_probability(forecast, target_date)
        if not predicted_high:
            return None

        in_bucket = (low_f <= predicted_high <= high_f)
        forecast_prob = confidence if in_bucket else (1 - confidence)

        edge = forecast_prob - market_price

        if edge < self.settings.WEATHER_MIN_EDGE:
            return None

        return {
            "market_id": market.get("condition_id", ""),
            "market_question": question,
            "token_id": token_id,
            "market_price": market_price,
            "forecast_prob": forecast_prob,
            "edge": edge,
            "predicted_high_f": predicted_high,
            "bucket_low": low_f,
            "bucket_high": high_f,
            "city": city_config["name"],
            "hours_until_resolution": hours_until,
            "order_book": order_book,
            "confidence": confidence,
        }

    async def _execute_opportunity(self, opp: Dict):
        portfolio_val = self.portfolio.get_portfolio_value()
        market_price = opp["market_price"]
        if not market_price or market_price <= 0:
            logger.debug(f"Skipping weather trade: market_price={market_price} (zero/invalid)")
            return
        odds = (1 - market_price) / market_price
        size = self.risk_manager.kelly_size(opp["edge"], odds, portfolio_val)
        size = min(size, self.settings.WEATHER_MAX_BET_USD)

        approved, reason = self.risk_manager.approve_trade(size, "weather_arb", opp["market_id"])
        if not approved:
            logger.debug(f"Trade rejected: {reason}")
            return

        logger.info(
            f"Weather Opportunity | {opp['city']} | "
            f"Bucket: {opp['bucket_low']}-{opp['bucket_high']}F | "
            f"Forecast: {opp['predicted_high_f']:.1f}F | "
            f"Market: {opp['market_price']:.2%} | "
            f"Forecast: {opp['forecast_prob']:.2%} | "
            f"Edge: {opp['edge']:.2%} | "
            f"Size: ${size:.2f}"
        )

        result = await self.poly_client.place_market_order(
            token_id=opp["token_id"],
            amount_usd=size,
            side="BUY",
            dry_run=self.settings.DRY_RUN
        )

        trade = Trade(
            id=None,
            timestamp=datetime.utcnow().isoformat(),
            strategy="weather_arb",
            market_id=opp["market_id"],
            market_question=opp["market_question"],
            side="BUY",
            token_id=opp["token_id"],
            price=opp["market_price"],
            size_usd=size,
            edge_pct=opp["edge"],
            dry_run=self.settings.DRY_RUN,
            order_id=result.order_id if result.success else None,
            status="open"
        )

        trade_id = self.portfolio.log_trade(trade)

        if result.success:
            logger.info(f"Weather trade placed: ${size:.2f} on {opp['city']} | Trade ID: {trade_id}")
            self.active_positions[opp["token_id"]] = {
                "trade_id": trade_id,
                "entry_price": opp["market_price"],
                "size": size,
                "edge": opp["edge"],
                "city": opp["city"],
                "resolution_hours": opp["hours_until_resolution"]
            }
        else:
            logger.warning(f"Weather trade failed: {result.error}")

    def _match_city(self, question: str) -> Optional[Dict]:
        question_lower = question.lower()
        for city_config in self.settings.WEATHER_CITIES:
            city_name = city_config["name"]
            patterns = CITY_PATTERNS.get(city_name, [city_name.lower()])
            for pattern in patterns:
                if pattern in question_lower:
                    return city_config
        return None

    def _parse_temp_bucket(self, question: str) -> Optional[Tuple[float, float]]:
        match = TEMP_BUCKET_REGEX.search(question)
        if match:
            try:
                low = float(match.group(1))
                high = float(match.group(2))
                return (low, high)
            except ValueError:
                pass
        return None

    async def cleanup(self):
        logger.info(f"WeatherArbStrategy cleanup: {len(self.active_positions)} open positions")
