"""
Polymarket CLOB Client Wrapper
Handles all API calls to Polymarket's Central Limit Order Book.

Uses Gamma API for market discovery (reliable active market filtering)
and CLOB API for order book queries and trade execution.
"""

import asyncio
import json
import logging
import time
from typing import Optional, Dict, List
from dataclasses import dataclass

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, MarketOrderArgs, OrderType, OrderBookSummary
)
from py_clob_client.order_builder.constants import BUY, SELL

logger = logging.getLogger("polybot.clob")


@dataclass
class OrderBook:
    token_id: str
    bids: List[Dict]  # [{price, size}]
    asks: List[Dict]
    mid_price: float
    spread: float
    liquidity_usd: float


@dataclass
class TradeResult:
    success: bool
    order_id: Optional[str]
    filled_price: Optional[float]
    filled_size: Optional[float]
    error: Optional[str] = None


def _normalize_gamma_market(m: Dict) -> Dict:
    """Normalize Gamma API field names to match CLOB API format.

    Gamma uses camelCase (conditionId, clobTokenIds, endDateIso)
    while CLOB uses snake_case (condition_id, tokens, end_date_iso).
    Strategies expect the CLOB format.
    """
    normalized = dict(m)

    # condition_id
    if "condition_id" not in normalized and "conditionId" in normalized:
        normalized["condition_id"] = normalized["conditionId"]

    # end_date_iso
    if "end_date_iso" not in normalized and "endDateIso" in normalized:
        normalized["end_date_iso"] = normalized["endDateIso"]
    elif "end_date_iso" not in normalized and "endDate" in normalized:
        normalized["end_date_iso"] = normalized["endDate"]

    # tokens — Gamma uses clobTokenIds (JSON string) + outcomes (JSON string)
    if "tokens" not in normalized or not isinstance(normalized.get("tokens"), list):
        clob_ids = normalized.get("clobTokenIds", "[]")
        outcomes_raw = normalized.get("outcomes", '["Yes", "No"]')

        try:
            if isinstance(clob_ids, str):
                token_ids = json.loads(clob_ids)
            else:
                token_ids = clob_ids
        except (json.JSONDecodeError, TypeError):
            token_ids = []

        try:
            if isinstance(outcomes_raw, str):
                outcomes = json.loads(outcomes_raw)
            else:
                outcomes = outcomes_raw
        except (json.JSONDecodeError, TypeError):
            outcomes = ["Yes", "No"]

        tokens = []
        for i, tid in enumerate(token_ids):
            outcome = outcomes[i] if i < len(outcomes) else ("Yes" if i == 0 else "No")
            tokens.append({
                "token_id": str(tid),
                "outcome": outcome,
            })
        normalized["tokens"] = tokens

    # active flag
    if "active" not in normalized:
        normalized["active"] = True

    return normalized


class PolymarketClient:
    """Async wrapper around Polymarket's py-clob-client."""

    def __init__(self, settings):
        self.settings = settings
        self._client: Optional[ClobClient] = None
        self._rate_limit_delay = 1.0 / 60  # 60 orders/min limit
        self._last_request_time = 0

    def _get_client(self) -> ClobClient:
        """
        Lazy-init the CLOB client (creates API creds on first call).

        Security: checks KeyVault first — key never leaves the vault.
        Falls back to settings.PRIVATE_KEY for backward compatibility.
        """
        if self._client is None:
            # Preferred path: use KeyVault (private key stays in vault)
            try:
                from core.key_vault import is_ready as vault_ready, get_client as vault_client
                if vault_ready():
                    client = vault_client()
                    if client is not None:
                        creds = client.create_or_derive_api_creds()
                        client.set_api_creds(creds)
                        self._client = client
                        logger.info("CLOB client authenticated via KeyVault")
                        return self._client
            except ImportError:
                pass

            # Legacy fallback: direct key from settings
            if self.settings.PRIVATE_KEY:
                try:
                    funder = getattr(self.settings, "FUNDER_ADDRESS", "")
                    kwargs = dict(chain_id=self.settings.CHAIN_ID, signature_type=2)  # POLY_PROXY
                    if funder:
                        kwargs["funder"] = funder
                    self._client = ClobClient(
                        self.settings.CLOB_HOST,
                        key=self.settings.PRIVATE_KEY,
                        **kwargs,
                    )
                    try:
                        creds = self._client.create_or_derive_api_creds()
                        self._client.set_api_creds(creds)
                    except Exception as e:
                        logger.critical(f"CLOB client create_or_derive_api_creds failed with PRIVATE_KEY: {e}")
                        raise
                    logger.warning(
                        "CLOB client using PRIVATE_KEY from settings (legacy). "
                        "Prefer key_vault.init_vault() at startup."
                    )
                except Exception as e:
                    logger.error(f"CLOB client auth failed: {e}")
                    self._client = ClobClient(self.settings.CLOB_HOST)
                    logger.info("Falling back to read-only CLOB client")
            else:
                self._client = ClobClient(self.settings.CLOB_HOST)
                logger.info("Polymarket CLOB client in read-only mode (no private key)")
        return self._client

    async def _rate_limit(self):
        """Enforce API rate limits."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._rate_limit_delay:
            await asyncio.sleep(self._rate_limit_delay - elapsed)
        self._last_request_time = time.monotonic()

    async def get_markets(self, tag: Optional[str] = None, active_only: bool = True) -> List[Dict]:
        """Fetch active markets from the Gamma API.

        Uses Gamma API because it supports proper filtering (closed=false,
        active=true) and returns markets sorted by volume. The CLOB API
        returns old/closed markets first and has unreliable pagination.
        """
        try:
            markets = []
            offset = 0
            limit = 100
            max_pages = 5  # Up to 500 markets

            for page in range(max_pages):
                try:
                    params = {
                        "limit": limit,
                        "offset": offset,
                        "active": "true",
                        "closed": "false",
                        "order": "volume24hr",
                        "ascending": "false",
                    }
                    if tag:
                        params["tag"] = tag

                    async with httpx.AsyncClient(timeout=15) as http:
                        resp = await http.get(
                            f"{self.settings.GAMMA_HOST}/markets",
                            params=params,
                        )
                        resp.raise_for_status()
                        result = resp.json()

                    if isinstance(result, list):
                        data = result
                    elif isinstance(result, dict):
                        data = result.get("data", result.get("markets", []))
                    else:
                        logger.warning(f"get_markets page {page}: unexpected type {type(result)}")
                        break

                    if not data:
                        break

                    logger.debug(f"get_markets page {page}: {len(data)} markets fetched")

                    for m in data:
                        if not isinstance(m, dict):
                            continue
                        # Normalize Gamma field names → CLOB format
                        normalized = _normalize_gamma_market(m)

                        if active_only and normalized.get("closed", False):
                            continue
                        if active_only and not normalized.get("active", False):
                            continue
                        markets.append(normalized)

                    offset += limit
                    if len(data) < limit:
                        break  # No more pages

                except Exception as e:
                    logger.warning(f"get_markets page {page} failed: {e}")
                    break

            logger.info(f"Fetched {len(markets)} active markets from Gamma API")
            return markets
        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            return []

    async def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Get current order book for a market token."""
        await self._rate_limit()
        try:
            client = self._get_client()
            book = client.get_order_book(token_id)
            bids = [{"price": float(b.price), "size": float(b.size)} for b in (book.bids or [])]
            asks = [{"price": float(a.price), "size": float(a.size)} for a in (book.asks or [])]

            if not bids or not asks:
                return None

            best_bid = max(bids, key=lambda x: x["price"])["price"]
            best_ask = min(asks, key=lambda x: x["price"])["price"]
            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
            liquidity = sum(b["price"] * b["size"] for b in bids) + sum(a["price"] * a["size"] for a in asks)

            return OrderBook(
                token_id=token_id,
                bids=bids,
                asks=asks,
                mid_price=mid,
                spread=spread,
                liquidity_usd=liquidity
            )
        except Exception as e:
            logger.debug(f"Order book fetch failed for {token_id[:16]}...: {e}")
            return None

    async def get_market_price(self, token_id: str, side: str = "MID") -> Optional[float]:
        """Get current mid-price for a token."""
        try:
            client = self._get_client()
            await self._rate_limit()
            if side == "MID":
                result = client.get_midpoint(token_id)
                return float(result.get("mid", 0))
            else:
                result = client.get_price(token_id, side=side)
                return float(result.get("price", 0))
        except Exception as e:
            logger.debug(f"Price fetch failed for {token_id[:16]}...: {e}")
            return None

    async def search_markets(self, query: str) -> List[Dict]:
        """Search markets by keyword (uses Gamma API for text search)."""
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                resp = await http.get(
                    f"{self.settings.GAMMA_HOST}/markets",
                    params={
                        "search": query,
                        "active": "true",
                        "closed": "false",
                        "limit": 50,
                    },
                )
                resp.raise_for_status()
                result = resp.json()

                # Gamma API returns a list directly, not {"markets": [...]}
                if isinstance(result, list):
                    raw_markets = result
                elif isinstance(result, dict):
                    raw_markets = result.get("markets", result.get("data", []))
                else:
                    return []

                # Normalize all field names
                return [_normalize_gamma_market(m) for m in raw_markets if isinstance(m, dict)]
        except Exception as e:
            logger.error(f"Market search failed for '{query}': {e}")
            return []

    async def place_limit_order(
        self, token_id: str, price: float, size: float, side: str, dry_run: bool = True,
        neg_risk: bool = False
    ) -> TradeResult:
        """Place a limit order on the CLOB."""
        side_const = BUY if side.upper() == "BUY" else SELL
        logger.info(f"{'[DRY RUN] ' if dry_run else ''}LIMIT {side} {size:.2f} @ ${price:.4f} token={token_id[:16]}... neg_risk={neg_risk}")

        if dry_run:
            # Simulated slippage for realistic paper trading
            slippage = 0.003  # 0.3% slippage simulation
            if side.upper() == "BUY":
                simulated_price = price * (1 + slippage)
            else:
                simulated_price = price * (1 - slippage)
            return TradeResult(success=True, order_id="dry_run_" + str(int(time.time())),
                               filled_price=simulated_price, filled_size=size)

        await self._rate_limit()
        try:
            client = self._get_client()
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side_const,
                neg_risk=neg_risk,
            )
            signed = client.create_order(order_args)
            resp = client.post_order(signed, OrderType.GTC)

            if resp.get("success"):
                order_id = resp.get("orderID", "unknown")
                logger.info(f"Order placed: {order_id}")
                return TradeResult(success=True, order_id=order_id, filled_price=price, filled_size=size)
            else:
                err = resp.get("errorMsg", "Unknown error")
                logger.warning(f"Order failed: {err}")
                return TradeResult(success=False, order_id=None, filled_price=None, filled_size=None, error=err)
        except Exception as e:
            logger.error(f"Order placement exception: {e}")
            return TradeResult(success=False, order_id=None, filled_price=None, filled_size=None, error=str(e))

    async def place_market_order(
        self, token_id: str, amount_usd: float, side: str, dry_run: bool = True,
        neg_risk: bool = False
    ) -> TradeResult:
        """Place a market order (uses FAK for best fill)."""
        logger.info(f"{'[DRY RUN] ' if dry_run else ''}MARKET {side} ${amount_usd:.2f} token={token_id[:16]}... neg_risk={neg_risk}")

        if dry_run:
            # Simulated slippage for realistic paper trading
            slippage = 0.003  # 0.3% slippage simulation
            if side.upper() == "BUY":
                simulated_fill_size = amount_usd * (1 - slippage)
            else:
                simulated_fill_size = amount_usd * (1 - slippage)
            return TradeResult(success=True, order_id="dry_run_mkt_" + str(int(time.time())),
                               filled_price=None, filled_size=simulated_fill_size)

        await self._rate_limit()
        try:
            client = self._get_client()
            side_const = BUY if side.upper() == "BUY" else SELL
            # MarketOrderArgs does not support neg_risk — neg_risk only applies to limit OrderArgs
            order_args = MarketOrderArgs(token_id=token_id, amount=amount_usd, side=side_const)
            signed = client.create_market_order(order_args)
            # Use FAK (Fill and Kill) — matches user's Polymarket settings
            resp = client.post_order(signed, OrderType.FAK)

            if resp.get("success"):
                order_id = resp.get("orderID")
                logger.info(f"Market order placed: orderID={order_id} side={side} amount=${amount_usd:.2f} token={token_id[:16]}...")
                return TradeResult(success=True, order_id=order_id, filled_price=None, filled_size=amount_usd)
            else:
                err_msg = resp.get("errorMsg", "no errorMsg field")
                logger.warning(
                    f"Market order REJECTED by CLOB: side={side} amount=${amount_usd:.2f} "
                    f"token={token_id[:16]}... | errorMsg={err_msg!r} | full_response={resp}"
                )
                return TradeResult(success=False, order_id=None, filled_price=None, filled_size=None,
                                   error=err_msg)
        except Exception as e:
            logger.error(
                f"Market order EXCEPTION: side={side} amount=${amount_usd:.2f} "
                f"token={token_id[:16]}... | {type(e).__name__}: {e}",
                exc_info=True,
            )
            return TradeResult(success=False, order_id=None, filled_price=None, filled_size=None, error=str(e))

    async def cancel_order(self, order_id: str, dry_run: bool = True) -> bool:
        """Cancel an open order."""
        if dry_run:
            logger.info(f"[DRY RUN] Cancel order {order_id}")
            return True
        try:
            client = self._get_client()
            resp = client.cancel(order_id)
            return resp.get("canceled", False)
        except Exception as e:
            logger.error(f"Cancel failed for {order_id}: {e}")
            return False

    async def get_open_orders(self) -> List[Dict]:
        """Get all open orders for authenticated wallet."""
        try:
            client = self._get_client()
            resp = client.get_orders()
            return resp if isinstance(resp, list) else []
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []
