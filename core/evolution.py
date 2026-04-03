"""
Evolution Engine — V3 Self-Improving Agent Layer (Polymarket)

Tracks per-agent performance and adjusts capital weight multipliers:
  - ROI (return on investment)
  - Win rate (% profitable trades)
  - Sharpe ratio (risk-adjusted)
  - Max drawdown

Every EVOLVE_EVERY_N_CYCLES cycles:
  - Ranks agents by composite score
  - Bottom 30%: weight → 0 (killed)
  - Top 20%: weight × 1.2 (boosted, max 1.5×)
  - Middle 50%: weight drifts toward 1.0

Persists agent_weights.json + agent_performance.json in data/.
"""

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("polybot.evolution")

# ── Evolution constants ───────────────────────────────────────────────────────
KILL_PERCENTILE       = 0.30    # Bottom 30% get weight halved
BOOST_PERCENTILE      = 0.20    # Top 20% get weight boosted
MAX_WEIGHT            = 1.50    # Cap on agent weight multiplier
MIN_WEIGHT            = 0.10    # Floor — never fully disable without manual override
DEFAULT_WEIGHT        = 1.00    # Weight for new / unrated agents
MIN_TRADES_TO_RANK    = 5       # Don't rank agents with fewer trades
EVOLVE_EVERY_N_CYCLES = 20      # Run evolution pass this often

# Score composition
SCORE_WEIGHTS = {"roi": 0.40, "win_rate": 0.35, "sharpe": 0.25}


class EvolutionEngine:
    """Tracks agent performance and adjusts capital weight multipliers."""

    def __init__(self, settings):
        self.settings = settings
        self._dir = Path(settings.DATA_DIR)
        self._weights_path = self._dir / "agent_weights.json"
        self._perf_path    = self._dir / "agent_performance.json"
        self._cycle_path   = self._dir / "evolution_cycle.txt"
        self._weights: dict[str, float] = {}
        self._perf: dict[str, dict]     = {}
        self._cycle = 0
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_weights(self) -> dict[str, float]:
        return dict(self._weights)

    def get_weight(self, agent_name: str) -> float:
        return self._weights.get(agent_name, DEFAULT_WEIGHT)

    def increment_cycle(self) -> int:
        """Increment cycle counter, return new value."""
        self._cycle += 1
        try:
            self._cycle_path.write_text(str(self._cycle))
        except Exception:
            pass
        return self._cycle

    def should_evolve(self) -> bool:
        return self._cycle > 0 and self._cycle % EVOLVE_EVERY_N_CYCLES == 0

    def record_trade(self, agent_name: str, pnl: float, size_usd: float):
        """Record a completed trade result for an agent."""
        if agent_name not in self._perf:
            self._perf[agent_name] = {
                "trades": [], "total_pnl": 0.0, "total_invested": 0.0,
                "wins": 0, "losses": 0, "last_updated": None,
            }
        perf = self._perf[agent_name]
        roi_pct = pnl / size_usd if size_usd > 0 else 0.0
        perf["trades"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "pnl": round(pnl, 4), "size_usd": round(size_usd, 4),
            "roi_pct": round(roi_pct, 4),
        })
        if len(perf["trades"]) > 100:
            perf["trades"] = perf["trades"][-100:]
        perf["total_pnl"]      += pnl
        perf["total_invested"] += size_usd
        perf["wins"]           += 1 if pnl > 0 else 0
        perf["losses"]         += 1 if pnl <= 0 else 0
        perf["last_updated"]    = datetime.now(timezone.utc).isoformat()
        self._save()
        logger.info(f"Evolution: {agent_name} trade pnl=${pnl:+.2f} roi={roi_pct:+.1%}")

    def sync_from_portfolio(self, portfolio):
        """
        Pull closed trades from the portfolio DB and attribute them to agents.
        Call this at the end of each cycle to keep performance data fresh.
        """
        try:
            closed = portfolio.get_closed_trades_since(
                self._perf.get("_last_sync_ts", "2000-01-01T00:00:00")
            )
            for trade in closed:
                agent = trade.get("strategy", "unknown")
                pnl   = float(trade.get("pnl") or 0)
                size  = float(trade.get("size_usd") or 0)
                if size > 0:
                    self.record_trade(agent, pnl, size)
            # Update sync timestamp
            self._perf["_last_sync_ts"] = datetime.now(timezone.utc).isoformat()
            self._save()
        except Exception as e:
            logger.debug(f"Evolution: portfolio sync failed: {e}")

    def evolve(self) -> dict[str, float]:
        """
        Run one evolution pass. Returns updated weights dict.
        Kills bottom 30%, boosts top 20%, adjusts others toward 1.0.
        """
        scorable = {
            name: self._score(perf)
            for name, perf in self._perf.items()
            if isinstance(perf, dict) and not name.startswith("_")
            and (perf.get("wins", 0) + perf.get("losses", 0)) >= MIN_TRADES_TO_RANK
        }

        if len(scorable) < 2:
            logger.info("Evolution: not enough ranked agents — skipping weight update")
            return self._weights

        ranked = sorted(scorable.items(), key=lambda x: x[1])
        n = len(ranked)
        kill_n  = max(0, int(n * KILL_PERCENTILE))
        boost_n = max(0, int(n * BOOST_PERCENTILE))

        new_weights = dict(self._weights)
        for i, (name, score) in enumerate(ranked):
            current = self._weights.get(name, DEFAULT_WEIGHT)
            if i < kill_n:
                new_w = max(MIN_WEIGHT, current * 0.60)
                action = "KILL"
            elif i >= n - boost_n:
                new_w = min(MAX_WEIGHT, current * 1.20)
                action = "BOOST"
            else:
                # Regress gently toward 1.0
                new_w = current * 0.95 + DEFAULT_WEIGHT * 0.05
                action = "HOLD"

            new_weights[name] = round(new_w, 3)
            logger.info(
                f"Evolution [{action}] {name}: score={score:.3f} "
                f"weight {current:.2f}→{new_w:.2f}"
            )

        self._weights = new_weights
        self._save()
        return new_weights

    def get_summary(self) -> str:
        """Human-readable performance summary for Discord."""
        lines = ["**V3 Agent Performance**"]
        for name, perf in sorted(self._perf.items()):
            if not isinstance(perf, dict) or name.startswith("_"):
                continue
            n = perf.get("wins", 0) + perf.get("losses", 0)
            if n == 0:
                continue
            wr  = perf.get("wins", 0) / n * 100
            inv = perf.get("total_invested", 1) or 1
            roi = perf.get("total_pnl", 0) / inv * 100
            w   = self._weights.get(name, DEFAULT_WEIGHT)
            lines.append(f"• **{name}**: {n}T | WR {wr:.0f}% | ROI {roi:+.1f}% | wt {w:.2f}×")
        return "\n".join(lines) if len(lines) > 1 else "No performance data yet."

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _score(self, perf: dict) -> float:
        """Composite score: 0.0 (worst) to 1.0 (best)."""
        trades = perf.get("trades", [])
        n = len(trades)
        if n < MIN_TRADES_TO_RANK:
            return 0.5  # Neutral for new agents

        # ROI score
        total_inv = perf.get("total_invested", 0) or 1
        roi = perf.get("total_pnl", 0) / total_inv
        roi_score = max(0.0, min(1.0, (roi + 0.5)))

        # Win rate score
        total = perf.get("wins", 0) + perf.get("losses", 0)
        wr_score = perf.get("wins", 0) / total if total > 0 else 0.5

        # Sharpe score
        returns = [t.get("roi_pct", 0) for t in trades]
        mean_r = sum(returns) / len(returns)
        if len(returns) > 1:
            var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
            std = math.sqrt(var) if var > 0 else 1e-9
            sharpe = mean_r / std
        else:
            sharpe = 0.0
        sharpe_score = max(0.0, min(1.0, (sharpe + 1.0) / 2.0))

        return (
            SCORE_WEIGHTS["roi"]      * roi_score   +
            SCORE_WEIGHTS["win_rate"] * wr_score     +
            SCORE_WEIGHTS["sharpe"]   * sharpe_score
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self):
        if self._weights_path.exists():
            try:
                self._weights = json.loads(self._weights_path.read_text())
            except Exception:
                self._weights = {}
        if self._perf_path.exists():
            try:
                self._perf = json.loads(self._perf_path.read_text())
            except Exception:
                self._perf = {}
        if self._cycle_path.exists():
            try:
                self._cycle = int(self._cycle_path.read_text().strip())
            except Exception:
                self._cycle = 0

    def _save(self):
        try:
            self._weights_path.write_text(json.dumps(self._weights, indent=2))
            self._perf_path.write_text(json.dumps(self._perf, indent=2))
        except Exception as e:
            logger.warning(f"EvolutionEngine: save failed: {e}")
