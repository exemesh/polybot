"""Find the best tradeable Polymarket market for each coin.

AS OF APRIL 2026:
  Polymarket has replaced the old binary BTC/ETH "up-or-down" markets with
  *multi-strike* hourly/4-hourly events. Each event (e.g. "Ethereum above on
  April 24 7pm ET") contains ~10 strike markets like "Ethereum above $2,300",
  "above $2,350", etc. Each market is Yes/No.

ETH is the only coin with an hourly series. BTC has 4-hourly only.

Strategy: for the currently-active event, find the strike market whose
favorite-side ask price is closest to the midpoint of our edge zone
([0.75, 0.88], midpoint 0.815). That is the "active market" we trade.
"""
from __future__ import annotations

import json as _json
import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import aiohttp
from dateutil import parser as dateparser

log = logging.getLogger("polybot.discovery")

# Strike price embedded in slugs like: "ethereum-above-2295-on-april-24-2026-6pm-et"
_STRIKE_RE = re.compile(r"(?:bitcoin|ethereum|solana|xrp|sol|btc|eth)-above-(\d+)", re.I)


@dataclass
class ActiveMarket:
    """Represents ONE chosen strike market (a specific Yes/No binary) that we
    will treat as the tradeable favorite/underdog pair."""
    coin: str                    # "BTC", "ETH"
    slug: str                    # e.g. "ethereum-above-2295-on-april-24-2026-6pm-et"
    condition_id: str
    token_id_up: str             # YES token
    token_id_down: str           # NO token
    strike: float                # e.g. 2295.0
    start_ts: float
    end_ts: float
    fetched_at: float

    # Kept for interface compatibility with strategy.py. "UP"=YES, "DOWN"=NO.
    @property
    def seconds_elapsed(self) -> float:
        return max(0.0, time.time() - self.start_ts)

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.end_ts - time.time())

    @property
    def is_active(self) -> bool:
        now = time.time()
        return self.start_ts <= now <= self.end_ts


def _extract_token_ids(market: dict) -> Tuple[str, str]:
    """Return (yes_token_id, no_token_id). Convention: outcomes[0]=Yes, outcomes[1]=No."""
    raw = market.get("clobTokenIds") or "[]"
    if isinstance(raw, str):
        try:
            ids = _json.loads(raw)
        except _json.JSONDecodeError:
            ids = []
    else:
        ids = list(raw)
    if len(ids) < 2:
        raise ValueError(f"market {market.get('slug')} has <2 token ids")

    outcomes_raw = market.get("outcomes") or "[]"
    if isinstance(outcomes_raw, str):
        try:
            outcomes = _json.loads(outcomes_raw)
        except _json.JSONDecodeError:
            outcomes = []
    else:
        outcomes = list(outcomes_raw)

    yes_idx, no_idx = 0, 1
    if outcomes and len(outcomes) >= 2:
        for i, o in enumerate(outcomes):
            ol = str(o).lower()
            if ol in ("yes", "up", "above"):
                yes_idx = i
            elif ol in ("no", "down", "below"):
                no_idx = i
    return str(ids[yes_idx]), str(ids[no_idx])


def _strike_from_slug(slug: str) -> Optional[float]:
    m = _STRIKE_RE.search(slug or "")
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _last_yes_price(market: dict) -> Optional[float]:
    """Best-effort read of last YES price from a Gamma market dict."""
    lp = market.get("lastTradePrice") or market.get("outcomePrices")
    if isinstance(lp, str):
        try:
            prices = _json.loads(lp)
            if isinstance(prices, list) and prices:
                return float(prices[0])
        except (_json.JSONDecodeError, ValueError, TypeError):
            pass
    try:
        return float(lp) if lp is not None else None
    except (ValueError, TypeError):
        return None


class MarketDiscovery:
    """Multi-strike-aware market picker.

    For each enabled coin we query the coin's series endpoint for currently-
    active events, pick the active event (start <= now <= end), then pick the
    best strike market inside it.

    Coin config supplies `series_id` (int). Series IDs as of Apr 2026:
      ETH hourly:   11373  (ethereum-multi-strikes-hourly)
      BTC 4-hourly: 10202  (bitcoin-multi-strikes-4h)
    """

    def __init__(self, gamma_host: str, coins_cfg: Dict[str, "CoinConfig"]) -> None:  # noqa: F821
        self.gamma_host = gamma_host.rstrip("/")
        self.coins = coins_cfg
        self._cache: Dict[str, ActiveMarket] = {}
        self._cache_ttl_sec = 20.0
        # Track last failed-query times so we don't hammer Gamma every tick
        # when no strike is in the edge zone.
        self._last_no_result_at: Dict[str, float] = {}
        self._no_result_backoff_sec = 10.0

    async def get_active(
        self,
        coin: str,
        current_spot: Optional[float] = None,
        min_fav_price: float = 0.75,
        max_fav_price: float = 0.88,
        target_fav_price: float = 0.815,
        force: bool = False,
    ) -> Optional[ActiveMarket]:
        now = time.time()
        cached = self._cache.get(coin)
        if cached and not force:
            if cached.is_active and (now - cached.fetched_at) < self._cache_ttl_sec:
                return cached
        # Backoff after a no-result so we don't spam Gamma + logs.
        # This applies even when force=True — force is for skipping success cache,
        # not for bypassing the no-result throttle.
        last_fail = self._last_no_result_at.get(coin, 0.0)
        if (now - last_fail) < self._no_result_backoff_sec:
            return None
        m = await self._pick_best_strike(coin, current_spot, min_fav_price, max_fav_price, target_fav_price)
        if m:
            self._cache[coin] = m
            self._last_no_result_at.pop(coin, None)
        else:
            self._last_no_result_at[coin] = now
        return m

    async def _pick_best_strike(
        self,
        coin: str,
        current_spot: Optional[float],
        min_fav: float,
        max_fav: float,
        target_fav: float,
    ) -> Optional[ActiveMarket]:
        cfg = self.coins.get(coin)
        if not cfg:
            return None
        series_id = getattr(cfg, "series_id", None)
        if not series_id:
            log.warning("coin %s has no series_id configured", coin)
            return None

        # Fetch all active events in the series
        url = f"{self.gamma_host}/events"
        params = {
            "series_id": str(series_id),
            "active": "true",
            "closed": "false",
            "limit": "50",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        log.warning("gamma events status %s for %s", r.status, coin)
                        return None
                    events = await r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("gamma events fetch error for %s: %s", coin, e)
            return None

        if not isinstance(events, list):
            return None
        now = time.time()

        # Find the single active event (currently running)
        active_event = None
        for ev in events:
            try:
                start_ts = dateparser.parse(ev.get("startDate") or "").timestamp()
                end_ts = dateparser.parse(ev.get("endDate") or "").timestamp()
            except Exception:
                continue
            if start_ts <= now <= end_ts:
                active_event = ev
                active_event["_start_ts"] = start_ts
                active_event["_end_ts"] = end_ts
                break

        if not active_event:
            log.info("no active event in series %s (%s) — %d events returned, none in time window",
                     series_id, coin, len(events))
            return None

        markets = active_event.get("markets", []) or []
        if not markets:
            log.debug("active event for %s has no markets", coin)
            return None

        # For each strike market, identify favorite and pick best candidate.
        # "favorite" = whichever side (YES or NO) has higher ask price.
        # If we have order-book-level ask via lastTradePrice only, use that as
        # a proxy; the WS order book will refine this at signal-evaluation time.
        candidates: List[Tuple[float, ActiveMarket, float]] = []
        for m in markets:
            slug = m.get("slug", "")
            strike = _strike_from_slug(slug)
            yes_px = _last_yes_price(m)
            if yes_px is None or strike is None:
                continue
            # Favorite price is max(yes_px, 1-yes_px); favorite is YES if yes_px > 0.5 else NO.
            fav_px = max(yes_px, 1.0 - yes_px)
            # Filter: only consider strikes in the edge zone
            if not (min_fav <= fav_px <= max_fav):
                continue
            try:
                yes_id, no_id = _extract_token_ids(m)
            except ValueError:
                continue
            candidate = ActiveMarket(
                coin=coin,
                slug=slug,
                condition_id=str(m.get("conditionId") or ""),
                token_id_up=yes_id,
                token_id_down=no_id,
                strike=strike,
                start_ts=float(active_event["_start_ts"]),
                end_ts=float(active_event["_end_ts"]),
                fetched_at=now,
            )
            # Score: how close is fav_px to our target (0.815)? Lower = better.
            score = abs(fav_px - target_fav)
            candidates.append((score, candidate, fav_px))

        if not candidates:
            # Show all strike fav_px so we know why nothing qualified
            debug_strikes = []
            for m in markets[:10]:
                yp = _last_yes_price(m)
                st = _strike_from_slug(m.get("slug", ""))
                if yp is not None and st is not None:
                    fp = max(yp, 1.0 - yp)
                    debug_strikes.append(f"${int(st)}:{fp:.2f}")
            log.info(
                "no strikes in edge zone [%.2f, %.2f] for %s — strikes: %s",
                min_fav, max_fav, coin, ", ".join(debug_strikes) or "none",
            )
            return None

        candidates.sort(key=lambda x: x[0])
        score, best, fav_px = candidates[0]
        log.info(
            "picked strike for %s: %s (strike $%s, fav_px=%.3f, %d candidates)",
            coin, best.slug, int(best.strike), fav_px, len(candidates),
        )
        return best

    def all_known_token_ids(self) -> List[str]:
        out: List[str] = []
        for m in self._cache.values():
            out.extend([m.token_id_up, m.token_id_down])
        return out
