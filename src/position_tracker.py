"""Track open positions with pickle-based persistence across restarts."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("polybot.positions")

PERSIST_PATH = "data/positions.json"


@dataclass
class Position:
    coin: str
    market_slug: str
    condition_id: str
    token_id: str            # the side we own (UP or DOWN)
    opposite_token_id: str   # the other side (for flip-stop)
    side: str                # "UP" or "DOWN"
    entry_price: float
    size_contracts: float
    spent_usd: float
    opened_at: float
    order_id: str
    window_end_ts: float
    # Updated live:
    last_mark_price: float = 0.0
    last_update: float = 0.0
    # Closed-position state:
    closed_at: Optional[float] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None   # "flip_stop" | "stop_loss" | "natural" | "emergency"
    realized_usd: Optional[float] = None  # net P&L in USD

    def mark_pnl_usd(self, mark_price: float) -> float:
        return (mark_price - self.entry_price) * self.size_contracts

    def is_open(self) -> bool:
        return self.closed_at is None


class PositionTracker:
    def __init__(self, persist_path: str = PERSIST_PATH) -> None:
        self.persist_path = persist_path
        self.positions: Dict[str, Position] = {}   # order_id -> Position
        self._load()

    # ---- public API ---------------------------------------------------
    def open_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.is_open())

    def open_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.is_open()]

    def has_open_on_coin(self, coin: str) -> bool:
        return any(p.coin == coin and p.is_open() for p in self.positions.values())

    def add(self, p: Position) -> None:
        self.positions[p.order_id] = p
        self._save()

    def update_mark(self, order_id: str, mark_price: float) -> None:
        p = self.positions.get(order_id)
        if not p:
            return
        p.last_mark_price = mark_price
        p.last_update = time.time()

    def close(
        self,
        order_id: str,
        close_price: float,
        reason: str,
        realized_usd: float,
    ) -> None:
        p = self.positions.get(order_id)
        if not p:
            return
        p.closed_at = time.time()
        p.close_price = close_price
        p.close_reason = reason
        p.realized_usd = realized_usd
        self._save()

    def prune_older_than(self, max_age_days: int = 30) -> None:
        """Keep only recent closed positions in the persistence file."""
        cutoff = time.time() - max_age_days * 86400
        new: Dict[str, Position] = {}
        for oid, p in self.positions.items():
            if p.is_open() or (p.closed_at and p.closed_at > cutoff):
                new[oid] = p
        self.positions = new
        self._save()

    # ---- persistence ---------------------------------------------------
    def _save(self) -> None:
        Path(self.persist_path).parent.mkdir(parents=True, exist_ok=True)
        tmp = self.persist_path + ".tmp"
        payload = {oid: asdict(p) for oid, p in self.positions.items()}
        with open(tmp, "w") as f:
            json.dump(payload, f, default=str, indent=2)
        os.replace(tmp, self.persist_path)

    def _load(self) -> None:
        if not Path(self.persist_path).exists():
            return
        try:
            with open(self.persist_path) as f:
                raw = json.load(f)
            for oid, d in raw.items():
                self.positions[oid] = Position(**d)
            open_n = sum(1 for p in self.positions.values() if p.is_open())
            log.info("loaded %d positions (%d open) from %s", len(self.positions), open_n, self.persist_path)
        except Exception as e:  # noqa: BLE001
            log.warning("positions load failed: %s (starting empty)", e)
