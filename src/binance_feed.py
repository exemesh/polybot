"""Binance 1-second kline WebSocket feed for reference prices.

Maintains a rolling price history per coin so the strategy can compute
VWAP, momentum, and deviation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

import websockets

log = logging.getLogger("polybot.binance")


@dataclass
class PriceSample:
    ts: float           # unix seconds
    close: float        # 1s close
    volume: float       # 1s base-asset volume


@dataclass
class CoinState:
    symbol: str                                  # e.g. "btcusdt"
    samples: Deque[PriceSample] = field(default_factory=lambda: deque(maxlen=1200))  # 20 min
    last_update: float = 0.0

    @property
    def last_price(self) -> Optional[float]:
        return self.samples[-1].close if self.samples else None

    def price_n_seconds_ago(self, n: int) -> Optional[float]:
        """Return close price from ~n seconds ago (best-effort)."""
        if not self.samples:
            return None
        target = time.time() - n
        for s in reversed(self.samples):
            if s.ts <= target:
                return s.close
        return self.samples[0].close

    def momentum_pct(self, lookback_sec: int) -> Optional[float]:
        now = self.last_price
        then = self.price_n_seconds_ago(lookback_sec)
        if now is None or then is None or then == 0:
            return None
        return (now - then) / then * 100.0

    def window_return_pct(self, window_start_ts: float) -> Optional[float]:
        """Return % change from window_start_ts to now."""
        if not self.samples:
            return None
        # find the sample closest to window_start_ts
        start_px = None
        for s in self.samples:
            if s.ts >= window_start_ts:
                start_px = s.close
                break
        if start_px is None or start_px == 0:
            return None
        now = self.last_price
        if now is None:
            return None
        return (now - start_px) / start_px * 100.0


class BinanceFeed:
    """Multi-symbol 1s-kline subscriber. Reconnects on drop."""

    def __init__(self, ws_url: str, symbols: List[str]) -> None:
        self.ws_url = ws_url.rstrip("/")
        self.symbols = [s.lower() for s in symbols]
        self.state: Dict[str, CoinState] = {s: CoinState(symbol=s) for s in self.symbols}
        self._task: Optional[asyncio.Task] = None
        self._stop = False

    def is_healthy(self, max_staleness_sec: float = 5.0) -> bool:
        now = time.time()
        for st in self.state.values():
            if now - st.last_update > max_staleness_sec:
                return False
        return True

    def get(self, symbol: str) -> CoinState:
        return self.state[symbol.lower()]

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        streams = "/".join(f"{s}@kline_1s" for s in self.symbols)
        url = f"{self.ws_url}/stream?streams={streams}"
        backoff = 1.0
        while not self._stop:
            try:
                log.info("binance_ws connecting: %s", url)
                async with websockets.connect(url, ping_interval=20, ping_timeout=15) as ws:
                    log.info("binance_ws connected")
                    backoff = 1.0
                    async for msg in ws:
                        self._handle_message(msg)
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                log.warning("binance_ws error: %s; reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle_message(self, msg: str) -> None:
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return
        payload = data.get("data") or data
        k = payload.get("k") if isinstance(payload, dict) else None
        if not k:
            return
        symbol = payload.get("s", "").lower()
        if symbol not in self.state:
            return
        try:
            close = float(k["c"])
            vol = float(k.get("v", 0.0))
            close_time = float(k.get("T", k.get("t", 0))) / 1000.0
        except (KeyError, ValueError, TypeError):
            return
        if close_time <= 0:
            close_time = time.time()
        st = self.state[symbol]
        # Only append when the kline closes (k["x"] is bool true when final)
        if k.get("x"):
            st.samples.append(PriceSample(ts=close_time, close=close, volume=vol))
        st.last_update = time.time()
