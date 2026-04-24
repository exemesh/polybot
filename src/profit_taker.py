"""Exit manager: flip-stop, stop-loss, natural-resolution.

No tiered partial exits — positions resolve in <5 minutes, so we exit all or
nothing. The bid-ask-as-volume-proxy bug from the old polybot is explicitly
NOT reproduced.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("polybot.profit_taker")


@dataclass
class ExitDecision:
    close: bool
    reason: str = ""
    mark_price: float = 0.0
    realized_usd: float = 0.0
    use_limit_price: float = 0.0


class ProfitTaker:
    def __init__(
        self,
        stop_loss_usd_per_position: float,
        flip_stop_enabled: bool,
        flip_stop_threshold: float,
    ) -> None:
        self.stop_loss_usd = stop_loss_usd_per_position
        self.flip_stop_enabled = flip_stop_enabled
        self.flip_stop_threshold = flip_stop_threshold

    def evaluate(
        self,
        position,           # Position
        own_book,           # OrderBook of token we own
        opposite_book,      # OrderBook of the other side (for flip-stop)
    ) -> ExitDecision:
        # Natural resolution: let market close itself. We check elsewhere.
        if position.window_end_ts <= time.time() + 5:
            return ExitDecision(close=False, reason="waiting_for_resolution")

        own_mid = own_book.mid if own_book else None
        own_bid = own_book.best_bid if own_book else None
        opp_mid = opposite_book.mid if opposite_book else None
        if own_mid is None or own_bid is None:
            return ExitDecision(close=False, reason="no_own_book")

        mark = own_mid
        pnl = position.mark_pnl_usd(mark)

        # Flip-stop: our side has become the underdog (clear reversal).
        if self.flip_stop_enabled and opp_mid is not None:
            # The favorite is whichever mid is higher. We exit when WE'RE the underdog
            # AND our mark is below the flip_stop_threshold (prevent false trigger on
            # minor oscillation around 50/50).
            if opp_mid > own_mid and own_mid < self.flip_stop_threshold:
                return ExitDecision(
                    close=True,
                    reason="flip_stop",
                    mark_price=mark,
                    realized_usd=pnl,
                    use_limit_price=max(0.01, own_bid - 0.01),
                )

        # Stop-loss: unrealised loss exceeds hard $ cap.
        if pnl <= -self.stop_loss_usd:
            return ExitDecision(
                close=True,
                reason="stop_loss",
                mark_price=mark,
                realized_usd=pnl,
                use_limit_price=max(0.01, own_bid - 0.01),
            )

        return ExitDecision(close=False, reason="hold", mark_price=mark)
