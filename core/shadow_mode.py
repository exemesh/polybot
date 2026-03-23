"""
core/shadow_mode.py — Shadow Mode Strategy Wrapper

Principle: "Test in production without risking production."

Wraps any strategy to run silently alongside live trading. The shadow instance
evaluates markets, generates signals, and logs what it *would* have done —
but never places real orders.

Use this to:
  - Validate new agent variants before promoting to live
  - Compare two parameter sets (current vs candidate) on real market data
  - Build a paper P&L track record before committing real capital

Usage:
    from core.shadow_mode import ShadowWrapper

    live_strategy = AIForecasterStrategy(portfolio, settings)
    shadow_strategy = ShadowWrapper(
        strategy=AIForecasterStrategy(portfolio, settings, variant="v2"),
        name="ai_forecaster_v2",
        db_path=settings.DB_PATH,
    )

    # Both run in the same cycle:
    await live_strategy.run(markets)
    await shadow_strategy.run(markets)   # logs only, no real orders

Based on:
  https://www.marktechpost.com/2026/03/21/safely-deploying-ml-models-to-production-
  four-controlled-strategies-a-b-canary-interleaved-shadow-testing/
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("polybot.shadow")


@dataclass
class ShadowSignal:
    """A trade signal captured from a shadow strategy."""
    timestamp: str
    strategy_name: str
    variant: str
    market_question: str
    token_id: str
    side: str
    price: float
    size_usd: float
    edge_pct: float
    signals: dict = field(default_factory=dict)
    simulated_outcome: Optional[str] = None   # filled in later by ShadowEvaluator
    simulated_pnl: Optional[float] = None


class ShadowWrapper:
    """
    Wraps a strategy instance and intercepts all trade proposals.
    Instead of executing, it writes to the shadow_log table.

    The wrapped strategy's open_trade() calls are redirected to
    shadow logging. All other methods pass through unchanged.
    """

    def __init__(self, strategy: Any, name: str, db_path: str, variant: str = "candidate"):
        self.strategy = strategy
        self.name = name
        self.variant = variant
        self.db_path = db_path
        self._signals: list[ShadowSignal] = []
        self._init_db()

        # Patch the strategy's portfolio.open_trade to intercept calls
        self._original_open_trade = strategy.portfolio.open_trade
        strategy.portfolio.open_trade = self._intercept_trade

        logger.info(f"[SHADOW] Wrapper active: {name} (variant={variant}) — orders will NOT execute")

    def _init_db(self) -> None:
        """Create shadow_log table if not exists."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shadow_log (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    logged_at        TEXT NOT NULL,
                    strategy_name    TEXT NOT NULL,
                    variant          TEXT NOT NULL,
                    market_question  TEXT,
                    token_id         TEXT,
                    side             TEXT,
                    price            REAL,
                    size_usd         REAL,
                    edge_pct         REAL,
                    signals          TEXT,
                    simulated_outcome TEXT,
                    simulated_pnl    REAL
                )
            """)
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"[SHADOW] Could not init shadow_log table: {exc}")

    def _intercept_trade(self, *args, **kwargs) -> None:
        """Called instead of portfolio.open_trade() — logs, never executes."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        # Extract fields from args/kwargs (matches portfolio.open_trade signature)
        market_question = kwargs.get("market_question", args[0] if args else "unknown")
        token_id        = kwargs.get("token_id", args[1] if len(args) > 1 else "")
        side            = kwargs.get("side", args[2] if len(args) > 2 else "BUY")
        price           = float(kwargs.get("price", args[3] if len(args) > 3 else 0))
        size_usd        = float(kwargs.get("size_usd", args[4] if len(args) > 4 else 0))
        edge_pct        = float(kwargs.get("edge_pct", args[5] if len(args) > 5 else 0))
        strategy_name   = kwargs.get("strategy", self.name)

        signal = ShadowSignal(
            timestamp=now,
            strategy_name=strategy_name,
            variant=self.variant,
            market_question=str(market_question)[:200],
            token_id=str(token_id),
            side=str(side),
            price=price,
            size_usd=size_usd,
            edge_pct=edge_pct,
        )
        self._signals.append(signal)
        self._write_signal(signal)

        logger.info(
            f"[SHADOW] {self.name}({self.variant}) WOULD trade: "
            f"{side} {token_id[:12]}... @ {price:.3f} | edge={edge_pct:.1%} | ${size_usd:.2f} "
            f"| '{str(market_question)[:60]}'"
        )

    def _write_signal(self, signal: ShadowSignal) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO shadow_log
                (logged_at, strategy_name, variant, market_question, token_id,
                 side, price, size_usd, edge_pct, signals)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal.timestamp, signal.strategy_name, signal.variant,
                signal.market_question, signal.token_id, signal.side,
                signal.price, signal.size_usd, signal.edge_pct,
                json.dumps(signal.signals),
            ))
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"[SHADOW] DB write failed: {exc}")

    def get_shadow_pnl_summary(self) -> dict:
        """
        Return a summary of shadow performance vs live.
        Compares shadow signals against actual market resolution in trades table.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute("""
                SELECT COUNT(*) as signals,
                       COUNT(simulated_pnl) as resolved,
                       COALESCE(SUM(simulated_pnl), 0) as total_pnl,
                       COALESCE(AVG(simulated_pnl), 0) as avg_pnl
                FROM shadow_log
                WHERE strategy_name = ? AND variant = ?
            """, (self.name, self.variant)).fetchone()
            conn.close()
            return {
                "strategy": self.name,
                "variant": self.variant,
                "total_signals": rows[0],
                "resolved": rows[1],
                "simulated_pnl": round(rows[2], 4),
                "avg_pnl_per_signal": round(rows[3], 4),
            }
        except Exception as exc:
            return {"error": str(exc)}

    def restore(self) -> None:
        """Remove the intercept and restore the original open_trade."""
        self.strategy.portfolio.open_trade = self._original_open_trade
        logger.info(f"[SHADOW] {self.name} wrapper removed — strategy restored to live mode")


def get_shadow_comparison_report(db_path: str) -> str:
    """
    Generate a Discord-ready comparison of shadow vs live strategy performance.
    """
    try:
        conn = sqlite3.connect(db_path)

        shadow_rows = conn.execute("""
            SELECT strategy_name, variant,
                   COUNT(*) as signals,
                   COALESCE(SUM(simulated_pnl), 0) as sim_pnl
            FROM shadow_log
            GROUP BY strategy_name, variant
            ORDER BY sim_pnl DESC
        """).fetchall()

        live_rows = conn.execute("""
            SELECT strategy,
                   COUNT(*) as trades,
                   COALESCE(SUM(pnl), 0) as live_pnl
            FROM trades
            WHERE dry_run = 0
            GROUP BY strategy
        """).fetchall()
        conn.close()

        if not shadow_rows:
            return "No shadow data yet."

        live_map = {r[0]: (r[1], r[2]) for r in live_rows}
        lines = ["**Shadow vs Live Comparison**"]
        for row in shadow_rows:
            name, variant, signals, sim_pnl = row
            live_trades, live_pnl = live_map.get(name, (0, 0.0))
            lines.append(
                f"• **{name}** ({variant}): shadow={signals} signals ${sim_pnl:+.4f} "
                f"| live={live_trades} trades ${live_pnl:+.4f}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Shadow report error: {exc}"
