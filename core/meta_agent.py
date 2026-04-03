"""
MetaAgent — V3 Core Brain of the MIROFISH Swarm System (Polymarket)

Aggregates signals from P1/P2/P3 agents into a True Probability (TP).
Only executes trades when net edge > MIN_EDGE_PCT.
Tracks capital weight multipliers per agent via EvolutionEngine.

Signal flow:
    P1/P2/P3.scan() → Signal[] → MetaAgent.evaluate_signals()
    → filter/aggregate by market → approved Signal[]
    → MetaAgent.execute_approved() → poly_client.place_market_order()
"""

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("polybot.meta_agent")

# ── Meta-agent constants ──────────────────────────────────────────────────────
MIN_EDGE_PCT          = 0.07   # 7% net edge required (V3 spec: 5-10%)
MIN_AGENT_AGREEMENT   = 0.40   # 40% of signals must agree on direction
MAX_SIGNALS_PER_CYCLE = 5      # Hard cap: never flood the order book in one cycle
FEE_ESTIMATE          = 0.010  # ~1% round-trip fee deducted from raw edge
DEFAULT_POSITION_USD  = 10.0   # Fallback size if not set in settings


# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class Signal:
    """A trading signal emitted by a single P-agent."""
    agent_name: str
    market_id: str             # Polymarket condition_id
    market_question: str
    token_id: str
    side: str                  # "YES" or "NO"
    agent_probability: float   # Agent's estimated true probability (0.01-0.99)
    market_price: float        # Current market mid price for the token side
    confidence: float          # Agent confidence 0.0-1.0
    size_usd: float            # Requested position size
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict = field(default_factory=dict)

    @property
    def edge_pct(self) -> float:
        """Raw edge before fees."""
        return abs(self.agent_probability - self.market_price)

    @property
    def net_edge(self) -> float:
        """Edge after estimated fee deduction."""
        return self.edge_pct - FEE_ESTIMATE


# ── MetaAgent ────────────────────────────────────────────────────────────────

class MetaAgent:
    """
    Core Brain: aggregates P-agent signals, computes True Probability,
    enforces edge threshold, and executes approved trades.
    """

    def __init__(self, settings, portfolio, risk_manager, evolution_engine=None):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.evolution = evolution_engine
        self._log_path = Path(settings.DATA_DIR) / "meta_agent_log.json"
        self._log: list[dict] = self._load_log()

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_agents(
        self,
        agents: list,
        open_token_ids: set,
    ) -> int:
        """
        Run all P-agents in parallel, evaluate signals, execute approved trades.
        Returns number of trades executed.
        """
        if not agents:
            return 0

        # Run agents concurrently
        results = await asyncio.gather(
            *[a.scan(open_token_ids=open_token_ids) for a in agents],
            return_exceptions=True,
        )

        all_signals: list[Signal] = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                agent_name = agents[i].__class__.__name__
                logger.warning(f"MetaAgent: {agent_name} scan failed: {res}")
                continue
            all_signals.extend(res)

        if not all_signals:
            logger.info("MetaAgent: no signals from P-agents this cycle")
            return 0

        logger.info(f"MetaAgent: collected {len(all_signals)} raw signals from {len(agents)} agents")

        approved = await self.evaluate_signals(all_signals)
        executed = await self.execute_approved(approved, open_token_ids)
        return executed

    async def evaluate_signals(self, signals: list[Signal]) -> list[Signal]:
        """
        Group signals by market → compute True Probability → filter by edge.
        Returns approved signals ready for execution.
        """
        if not signals:
            return []

        weights = self.evolution.get_weights() if self.evolution else {}

        # Group by market
        by_market: dict[str, list[Signal]] = {}
        for sig in signals:
            by_market.setdefault(sig.market_id, []).append(sig)

        approved: list[Signal] = []

        for market_id, market_signals in by_market.items():
            result = self._aggregate_market_signals(market_signals, weights)
            if result:
                approved.append(result)

        # Sort by net edge, enforce cycle cap
        approved.sort(key=lambda s: s.metadata.get("net_edge", 0), reverse=True)
        if len(approved) > MAX_SIGNALS_PER_CYCLE:
            dropped = len(approved) - MAX_SIGNALS_PER_CYCLE
            logger.info(f"MetaAgent: cycle cap — keeping top {MAX_SIGNALS_PER_CYCLE}, dropping {dropped}")
            approved = approved[:MAX_SIGNALS_PER_CYCLE]

        logger.info(f"MetaAgent: {len(signals)} signals → {len(approved)} approved")
        self._append_log(signals, approved)
        return approved

    async def execute_approved(self, signals: list[Signal], open_token_ids: set) -> int:
        """Execute approved signals via poly_client + portfolio tracking."""
        if not signals:
            return 0

        from core.polymarket_client import PolymarketClient
        from core.portfolio import Trade
        from datetime import datetime

        poly_client = PolymarketClient(self.settings)
        executed = 0

        for sig in signals:
            if sig.token_id in open_token_ids:
                logger.debug(f"MetaAgent: skip {sig.token_id[:12]} — already open")
                continue

            # Risk check
            approved, reason = self.risk_manager.approve_trade(
                sig.size_usd, sig.agent_name, sig.market_id
            )
            if not approved:
                logger.info(f"MetaAgent: risk rejected '{sig.market_question[:40]}' — {reason}")
                continue

            logger.info(
                f"MetaAgent EXECUTE [{sig.side}] '{sig.market_question[:55]}' | "
                f"TP={sig.agent_probability:.0%} mkt={sig.market_price:.0%} "
                f"edge={sig.metadata.get('net_edge', sig.net_edge):.1%} "
                f"${sig.size_usd:.2f}"
            )

            result = await poly_client.place_market_order(
                sig.token_id, sig.size_usd, "BUY", self.settings.DRY_RUN
            )

            if result.success:
                trade = Trade(
                    id=None,
                    timestamp=datetime.utcnow().isoformat(),
                    strategy=sig.agent_name,
                    market_id=sig.market_id,
                    market_question=sig.market_question,
                    side=f"BUY_{sig.side}",
                    token_id=sig.token_id,
                    price=sig.market_price,
                    size_usd=sig.size_usd,
                    edge_pct=sig.metadata.get("net_edge", sig.net_edge),
                    dry_run=self.settings.DRY_RUN,
                    order_id=result.order_id,
                    pnl=None,
                    status="open",
                )
                self.portfolio.log_trade(trade)
                open_token_ids.add(sig.token_id)
                executed += 1

                await self._post_trade_alert(sig)
            else:
                logger.warning(f"MetaAgent: order failed — {result.error}")

        return executed

    # ── Signal aggregation ────────────────────────────────────────────────────

    def _aggregate_market_signals(
        self, signals: list[Signal], weights: dict[str, float]
    ) -> Optional[Signal]:
        """
        Compute True Probability from weighted average of agent estimates.
        Returns an approved Signal or None if edge insufficient.
        """
        # Weighted True Probability
        total_weight = 0.0
        weighted_sum = 0.0
        for sig in signals:
            w = weights.get(sig.agent_name, 1.0) * sig.confidence
            weighted_sum += sig.agent_probability * w
            total_weight += w

        if total_weight == 0:
            return None

        true_prob = weighted_sum / total_weight
        market_price = signals[0].market_price
        net_edge = abs(true_prob - market_price) - FEE_ESTIMATE

        if net_edge < MIN_EDGE_PCT:
            logger.debug(
                f"MetaAgent: '{signals[0].market_question[:40]}' "
                f"net_edge={net_edge:.1%} < {MIN_EDGE_PCT:.0%} → skip"
            )
            return None

        # Consensus check
        side = "YES" if true_prob > market_price else "NO"
        agree_count = sum(
            1 for s in signals
            if (side == "YES" and s.agent_probability > market_price) or
               (side == "NO"  and s.agent_probability < market_price)
        )
        agreement_rate = agree_count / len(signals)

        if agreement_rate < MIN_AGENT_AGREEMENT:
            logger.debug(
                f"MetaAgent: '{signals[0].market_question[:40]}' "
                f"agreement={agreement_rate:.0%} < {MIN_AGENT_AGREEMENT:.0%} → skip"
            )
            return None

        # Pick best signal template, scale size by edge strength
        best = max(signals, key=lambda s: s.confidence * weights.get(s.agent_name, 1.0))
        max_pos = getattr(self.settings, "MAX_POSITION_USD", DEFAULT_POSITION_USD)
        edge_mult = min(2.0, net_edge / MIN_EDGE_PCT)
        adjusted_size = min(best.size_usd * edge_mult, max_pos)

        return Signal(
            agent_name=f"meta[{','.join(sorted(set(s.agent_name for s in signals)))}]",
            market_id=signals[0].market_id,
            market_question=signals[0].market_question,
            token_id=best.token_id if side == best.side else signals[0].token_id,
            side=side,
            agent_probability=round(true_prob, 4),
            market_price=market_price,
            confidence=min(1.0, agreement_rate * 1.2),
            size_usd=round(adjusted_size, 2),
            metadata={
                "true_probability": true_prob,
                "net_edge": net_edge,
                "agreement_rate": agreement_rate,
                "agent_count": len(signals),
                "source_agents": [s.agent_name for s in signals],
            },
        )

    # ── Discord alert ─────────────────────────────────────────────────────────

    async def _post_trade_alert(self, sig: Signal):
        webhook = getattr(self.settings, "DISCORD_WEBHOOK_BLAZE", "")
        if not webhook:
            return
        mode = "DRY RUN" if self.settings.DRY_RUN else "LIVE"
        edge = sig.metadata.get("net_edge", sig.net_edge)
        agents = sig.metadata.get("source_agents", [sig.agent_name])
        msg = (
            f"🧠 **METAAGENT {sig.side} [{mode}]**\n"
            f"**Market:** {sig.market_question[:120]}\n"
            f"**True Probability:** `{sig.agent_probability:.0%}` vs market `{sig.market_price:.0%}`\n"
            f"**Net Edge:** `{edge:.1%}` | **Size:** `${sig.size_usd:.2f}`\n"
            f"**Agents:** {', '.join(agents)}\n"
            f"**Agreement:** `{sig.metadata.get('agreement_rate', 0):.0%}` of `{sig.metadata.get('agent_count', 1)}` agents"
        )
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(webhook, json={"content": msg, "username": "🧠 MetaAgent"})
        except Exception:
            pass

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _load_log(self) -> list[dict]:
        if self._log_path.exists():
            try:
                return json.loads(self._log_path.read_text())
            except Exception:
                pass
        return []

    def _append_log(self, all_signals: list[Signal], approved: list[Signal]):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_signals": len(all_signals),
            "approved": len(approved),
            "agents": list(set(s.agent_name for s in all_signals)),
            "approved_markets": [s.market_question[:60] for s in approved],
        }
        self._log.append(entry)
        if len(self._log) > 100:
            self._log = self._log[-100:]
        try:
            self._log_path.write_text(json.dumps(self._log[-50:], indent=2))
        except Exception:
            pass
