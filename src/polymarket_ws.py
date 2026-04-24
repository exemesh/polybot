"""Polymarket CLOB order-book WebSocket client.

Subscribes to MARKET channel for the currently-active token IDs and keeps
a per-token order book in memory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import websockets

log = logging.getLogger("polybot.pm_ws")


@dataclass
class BookLevel:
    price: float
    size: float  # in contracts (shares)


@dataclass
class OrderBook:
    token_id: str
    bids: List[BookLevel] = field(default_factory=list)  # sorted desc by price
    asks: List[BookLevel] = field(default_factory=list)  # sorted asc by price
    last_update: float = 0.0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return (a + b) / 2.0

    @property
    def spread_cents(self) -> Optional[int]:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return int(round((a - b) * 100))

    def depth_usd_at_ask(self, n_levels: int = 3) -> float:
        """Approximate USD spendable at best ask over top-N levels."""
        total = 0.0
        for lvl in self.asks[:n_levels]:
            total += lvl.price * lvl.size
        return total

    def is_fresh(self, max_staleness_sec: float) -> bool:
        return (time.time() - self.last_update) <= max_staleness_sec


def _parse_levels(raw_levels: List[dict], reverse: bool) -> List[BookLevel]:
    out: List[BookLevel] = []
    for lvl in raw_levels:
        try:
            out.append(BookLevel(price=float(lvl["price"]), size=float(lvl["size"])))
        except (KeyError, ValueError, TypeError):
            continue
    out.sort(key=lambda x: x.price, reverse=reverse)
    return out


class PolymarketWS:
    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self.books: Dict[str, OrderBook] = {}
        self._subscribed: Set[str] = set()
        self._pending: Set[str] = set()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._lock = asyncio.Lock()

    def get_book(self, token_id: str) -> Optional[OrderBook]:
        return self.books.get(token_id)

    def ensure_subscribed(self, token_ids: List[str]) -> None:
        """Mark these token IDs as needed; the run loop picks them up."""
        for tid in token_ids:
            if tid and tid not in self._subscribed:
                self._pending.add(tid)
                self.books.setdefault(tid, OrderBook(token_id=tid))

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                log.info("pm_ws connecting")
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=15
                ) as ws:
                    self._ws = ws
                    # Re-subscribe to everything (after any reconnect)
                    self._pending.update(self._subscribed)
                    self._subscribed.clear()
                    await self._send_pending()
                    backoff = 1.0
                    while not self._stop:
                        # Periodically push any newly-added token IDs
                        if self._pending:
                            await self._send_pending()
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            self._handle_message(msg)
                        except asyncio.TimeoutError:
                            continue
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                log.warning("pm_ws error: %s; reconnecting in %.1fs", e, backoff)
                self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _send_pending(self) -> None:
        if not self._ws or not self._pending:
            return
        async with self._lock:
            to_send = list(self._pending)
            self._pending.clear()
            payload = {"type": "MARKET", "assets_ids": to_send}
            try:
                await self._ws.send(json.dumps(payload))
                self._subscribed.update(to_send)
                log.info("pm_ws subscribed to %d tokens", len(to_send))
            except Exception as e:  # noqa: BLE001
                log.warning("pm_ws subscribe failed: %s", e)
                # put them back
                self._pending.update(to_send)

    def _handle_message(self, msg: str) -> None:
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return

        # The PM CLOB WS can send single objects or arrays of events.
        events = data if isinstance(data, list) else [data]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype = ev.get("event_type") or ev.get("type")
            tid = ev.get("asset_id") or ev.get("token_id")
            if not tid:
                continue
            book = self.books.get(tid)
            if book is None:
                book = OrderBook(token_id=tid)
                self.books[tid] = book

            if etype == "book":
                bids = ev.get("bids") or ev.get("buys") or []
                asks = ev.get("asks") or ev.get("sells") or []
                book.bids = _parse_levels(bids, reverse=True)
                book.asks = _parse_levels(asks, reverse=False)
                book.last_update = time.time()
            elif etype == "price_change":
                # Incremental update: apply level changes
                changes = ev.get("changes") or []
                for ch in changes:
                    try:
                        price = float(ch["price"])
                        size = float(ch["size"])
                        side = (ch.get("side") or "").upper()
                    except (KeyError, ValueError, TypeError):
                        continue
                    levels = book.bids if side == "BUY" else book.asks
                    # remove existing level at this price
                    levels[:] = [lvl for lvl in levels if lvl.price != price]
                    if size > 0:
                        levels.append(BookLevel(price=price, size=size))
                    levels.sort(key=lambda x: x.price, reverse=(side == "BUY"))
                book.last_update = time.time()
            # other event types (last_trade_price, tick_size_change, etc.)
            # are ignored — we only care about book state for entries.
