"""Find the currently-active 15-minute up/down market for each coin via Gamma API.

Polymarket runs a rolling series of 15-min binary markets. At any moment there's
one active market per coin (the one about to close within the next 15 minutes).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import aiohttp
from dateutil import parser as dateparser

log = logging.getLogger("polybot.discovery")


@dataclass
class ActiveMarket:
    coin: str                    # "BTC", "ETH", etc.
    slug: str                    # e.g. "bitcoin-up-or-down-apr-20-2026-2pm"
    condition_id: str
    token_id_up: str
    token_id_down: str
    start_ts: float              # unix seconds (window open)
    end_ts: float                # unix seconds (window close)
    fetched_at: float

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


def _extract_token_ids(market: dict) -> tuple[str, str]:
    """Given a Gamma market JSON, return (up_token_id, down_token_id).

    The Gamma API returns clobTokenIds as a stringified JSON list. Convention:
    index 0 = YES / UP, index 1 = NO / DOWN. Outcomes list is aligned.
    """
    import json as _json
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

    up_idx, down_idx = 0, 1
    if outcomes and len(outcomes) >= 2:
        for i, o in enumerate(outcomes):
            ol = str(o).lower()
            if ol in ("up", "yes"):
                up_idx = i
            elif ol in ("down", "no"):
                down_idx = i
    return str(ids[up_idx]), str(ids[down_idx])


class MarketDiscovery:
    def __init__(self, gamma_host: str, coins_cfg: Dict[str, "CoinConfig"]) -> None:  # noqa: F821
        self.gamma_host = gamma_host.rstrip("/")
        self.coins = coins_cfg
        self._cache: Dict[str, ActiveMarket] = {}  # coin -> ActiveMarket
        self._cache_ttl_sec = 30.0  # refresh at most every 30s unless forced

    async def get_active(self, coin: str, force: bool = False) -> Optional[ActiveMarket]:
        cached = self._cache.get(coin)
        now = time.time()
        if cached and not force:
            # If the market is still active and cache is fresh, return it
            if cached.is_active and (now - cached.fetched_at) < self._cache_ttl_sec:
                return cached
            if cached.is_active and cached.seconds_left > 60:
                return cached
        market = await self._fetch_active(coin)
        if market:
            self._cache[coin] = market
        return market

    async def _fetch_active(self, coin: str) -> Optional[ActiveMarket]:
        cfg = self.coins.get(coin)
        if not cfg:
            return None
        url = f"{self.gamma_host}/markets"
        params = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "limit": "50",
            "order": "endDate",
            "ascending": "true",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        log.warning("gamma status %s for %s", r.status, coin)
                        return None
                    items = await r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("gamma fetch error for %s: %s", coin, e)
            return None

        now = time.time()
        best: Optional[dict] = None
        for m in items if isinstance(items, list) else []:
            slug = (m.get("slug") or "").lower()
            if not slug.startswith(cfg.market_slug_pattern.lower()):
                continue
            try:
                end_iso = m.get("endDate") or m.get("endDateIso")
                start_iso = m.get("startDate") or m.get("startDateIso")
                if not (end_iso and start_iso):
                    continue
                end_ts = dateparser.parse(end_iso).timestamp()
                start_ts = dateparser.parse(start_iso).timestamp()
            except Exception:
                continue
            if start_ts <= now <= end_ts:
                best = m
                best["_start_ts"] = start_ts
                best["_end_ts"] = end_ts
                break  # first match wins (earliest ending active)

        if not best:
            log.debug("no active market found for %s", coin)
            return None

        try:
            up_id, down_id = _extract_token_ids(best)
        except ValueError as e:
            log.warning("token id parse failed: %s", e)
            return None

        return ActiveMarket(
            coin=coin,
            slug=best.get("slug", ""),
            condition_id=str(best.get("conditionId") or best.get("condition_id") or ""),
            token_id_up=up_id,
            token_id_down=down_id,
            start_ts=float(best["_start_ts"]),
            end_ts=float(best["_end_ts"]),
            fetched_at=time.time(),
        )

    def all_known_token_ids(self) -> List[str]:
        out: List[str] = []
        for m in self._cache.values():
            out.extend([m.token_id_up, m.token_id_down])
        return out
