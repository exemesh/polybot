"""
core/order_staging.py — Order Staging Buffer (GitHub Security Principle #2)

Principle: "Stage and vet all writes."

Every proposed trade from a strategy is placed into a staging buffer first.
A deterministic validation pipeline reviews it before execution.
No order touches the exchange without passing all checks.

Validation pipeline (in order):
  1. Schema check        — required fields present + correct types
  2. Price bounds        — price in (0.0, 1.0), not extreme
  3. Size bounds         — within per-trade and portfolio limits
  4. Edge threshold      — edge_pct >= minimum before fees
  5. Cycle volume limit  — max N orders per scan cycle
  6. Duplicate guard     — same token_id not already in open positions
  7. Dry-run gate        — if dry_run=True, sign_and_submit is bypassed

Based on: https://github.blog/ai-and-ml/generative-ai/
          under-the-hood-security-architecture-of-github-agentic-workflows/
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("polybot.order_staging")

try:
    from core.forbidden_actions import check_trade as boundary_check_trade, log_boundary_summary
    _BOUNDARIES_AVAILABLE = True
except ImportError:
    _BOUNDARIES_AVAILABLE = False

MAX_ORDERS_PER_CYCLE = 10
MIN_EDGE_AFTER_STAGING = 0.005
MAX_PRICE = 0.97
MIN_PRICE = 0.03
MAX_SINGLE_TRADE_USD = 50.0
MAX_TOTAL_CYCLE_USD = 150.0


@dataclass
class StagedOrder:
    """Proposed order waiting for validation."""
    token_id: str
    side: str
    price: float
    size_usd: float
    edge_pct: float
    strategy: str
    market_question: str
    market_type: str = "free"
    order_type: str = "limit"
    dry_run: bool = True
    staged_at: float = field(default_factory=time.time)

    validated: bool = False
    rejected: bool = False
    reject_reason: Optional[str] = None
    order_result: Optional[dict] = None


class OrderStagingBuffer:
    """
    Collects proposed orders, validates them, then executes via KeyVault.

    Usage:
        buffer = OrderStagingBuffer(portfolio, settings)
        buffer.propose(StagedOrder(...))
        results = buffer.flush()
    """

    def __init__(self, portfolio, settings):
        self.portfolio = portfolio
        self.settings = settings
        self._staged: list = []
        self._cycle_usd_committed: float = 0.0

    def propose(self, order: StagedOrder) -> None:
        """Add an order to the staging buffer. Does NOT execute yet."""
        logger.info(
            f"[STAGED] {order.strategy} | {order.side} {order.token_id[:12]}... "
            f"@ {order.price:.3f} | edge={order.edge_pct:.1%} | ${order.size_usd:.2f}"
        )
        self._staged.append(order)

    def _validate(self, order: StagedOrder) -> Optional[str]:
        """Run validation pipeline. Returns rejection reason or None."""
        if not order.token_id or not order.side or order.price <= 0 or order.size_usd <= 0:
            return "schema_error: missing or invalid required fields"

        if not (MIN_PRICE <= order.price <= MAX_PRICE):
            return f"price_bounds: price {order.price:.4f} outside [{MIN_PRICE}, {MAX_PRICE}]"

        if order.size_usd > MAX_SINGLE_TRADE_USD:
            return f"size_limit: ${order.size_usd:.2f} exceeds max ${MAX_SINGLE_TRADE_USD}"

        if self._cycle_usd_committed + order.size_usd > MAX_TOTAL_CYCLE_USD:
            return (
                f"cycle_limit: cycle total would reach "
                f"${self._cycle_usd_committed + order.size_usd:.2f} > ${MAX_TOTAL_CYCLE_USD}"
            )

        # ── Forbidden Actions boundary check ────────────────────────────────
        if _BOUNDARIES_AVAILABLE:
            portfolio_value = getattr(self.portfolio, 'get_portfolio_value', lambda: 0)()
            boundary_ok, boundary_reason = boundary_check_trade(
                size_usd=order.size_usd,
                price=order.price,
                edge_pct=order.edge_pct,
                portfolio_value=portfolio_value,
                agent_name=order.strategy,
            )
            if not boundary_ok:
                return f"boundary_violation: {boundary_reason}"

        if order.edge_pct < MIN_EDGE_AFTER_STAGING:
            return f"edge_too_low: {order.edge_pct:.3%} < {MIN_EDGE_AFTER_STAGING:.3%}"

        approved_so_far = sum(1 for o in self._staged if o.validated)
        if approved_so_far >= MAX_ORDERS_PER_CYCLE:
            return f"cycle_volume: already approved {approved_so_far} orders this cycle"

        try:
            open_tokens = {t.get("token_id") for t in self.portfolio.get_open_trades()}
            if order.token_id in open_tokens and order.side == "BUY":
                return f"duplicate: token {order.token_id[:12]}... already in open positions"
        except Exception:
            pass

        return None

    def flush(self) -> list:
        """Validate all staged orders and execute the ones that pass."""
        from core.key_vault import sign_and_submit

        if not self._staged:
            return []

        logger.info(f"OrderStaging: validating {len(self._staged)} staged order(s)")
        results = []

        for order in self._staged:
            reject_reason = self._validate(order)

            if reject_reason:
                order.rejected = True
                order.reject_reason = reject_reason
                logger.warning(
                    f"[REJECTED] {order.strategy} | {order.side} {order.token_id[:12]}... "
                    f"— {reject_reason}"
                )
            else:
                order.validated = True
                shares = order.size_usd / order.price if order.price > 0 else 0
                order_params = {
                    "token_id": order.token_id,
                    "side": order.side,
                    "price": order.price,
                    "size": round(shares, 2),
                    "order_type": order.order_type,
                }
                result = sign_and_submit(order_params, dry_run=order.dry_run)
                order.order_result = result
                self._cycle_usd_committed += order.size_usd

                if result["success"]:
                    logger.info(
                        f"[EXECUTED] {order.strategy} | {order.side} {order.token_id[:12]}... "
                        f"order_id={result['order_id']}"
                    )
                else:
                    logger.error(f"[EXEC_FAIL] {order.strategy} | {result['error']}")

            try:
                from core.reasoning_logger import log_order_decision
                log_order_decision(order)
            except Exception:
                pass

            results.append(order)

        self._staged = []
        self._cycle_usd_committed = 0.0
        return results

    def summary(self, results: list) -> dict:
        executed = [o for o in results if o.validated and o.order_result and o.order_result["success"]]
        rejected = [o for o in results if o.rejected]
        failed = [o for o in results if o.validated and o.order_result and not o.order_result["success"]]
        return {
            "total_staged": len(results),
            "executed": len(executed),
            "rejected": len(rejected),
            "exec_failed": len(failed),
            "total_usd": sum(o.size_usd for o in executed),
            "rejection_reasons": [o.reject_reason for o in rejected],
        }
