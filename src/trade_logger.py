"""Trade + signal logging to JSONL files.

One line per event. Easy to `tail -f` and `jq` over. Used by
scripts/analyze_trades.py to compute win rates per (price_bin, time_bin).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger("polybot.trade_log")


class TradeLogger:
    def __init__(self, trade_path: str, signal_path: str) -> None:
        for p in (trade_path, signal_path):
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        self.trade_path = trade_path
        self.signal_path = signal_path

    # --- signal evaluations (every tick) ------------------------------
    def log_signal(self, signal) -> None:
        """Write one line per signal evaluation. Emit only fires + notable rejects."""
        # Only log fires and rejects with reasons that indicate "almost fired"
        if not signal.fire and signal.reason.startswith(("elapsed=", "left=", "window")):
            # skip simple-time rejects to keep volume down
            return
        payload = {
            "ts": time.time(),
            "kind": "signal",
            "fire": signal.fire,
            "coin": signal.coin,
            "side": signal.side,
            "entry_price": signal.entry_price,
            "favorite_price": signal.favorite_price,
            "underdog_price": signal.underdog_price,
            "seconds_left": signal.seconds_left,
            "seconds_elapsed": signal.seconds_elapsed,
            "momentum_pct": signal.binance_momentum_pct,
            "vwap_deviation_pct": signal.vwap_deviation_pct,
            "reason": signal.reason,
        }
        with open(self.signal_path, "a") as f:
            f.write(json.dumps(payload, default=str) + "\n")

    # --- trades (entries + exits) -------------------------------------
    def log_entry(self, position, dry_run: bool) -> None:
        payload = {
            "ts": time.time(),
            "kind": "entry",
            "dry_run": dry_run,
            "order_id": position.order_id,
            "coin": position.coin,
            "side": position.side,
            "token_id": position.token_id,
            "entry_price": position.entry_price,
            "size_contracts": position.size_contracts,
            "spent_usd": position.spent_usd,
            "market_slug": position.market_slug,
            "window_end_ts": position.window_end_ts,
        }
        with open(self.trade_path, "a") as f:
            f.write(json.dumps(payload, default=str) + "\n")

    def log_exit(self, position, reason: str, close_price: float, realized_usd: float, dry_run: bool) -> None:
        payload = {
            "ts": time.time(),
            "kind": "exit",
            "dry_run": dry_run,
            "order_id": position.order_id,
            "coin": position.coin,
            "side": position.side,
            "entry_price": position.entry_price,
            "close_price": close_price,
            "size_contracts": position.size_contracts,
            "realized_usd": realized_usd,
            "reason": reason,
            "market_slug": position.market_slug,
            "seconds_held": time.time() - position.opened_at,
        }
        with open(self.trade_path, "a") as f:
            f.write(json.dumps(payload, default=str) + "\n")
