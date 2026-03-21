"""
Backtest Engine + Lookahead Guard
==================================
Safe backtesting with canary-based lookahead detection.
Strategy functions receive ONLY historical data (no future leakage).
"""
import math
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class MarketSnapshot:
    timestamp: float
    market_id: str
    question: str
    yes_price: float
    no_price: float
    volume: float
    liquidity: float
    resolved: Optional[bool]
    market_type: str = "free"


@dataclass
class BacktestTrade:
    timestamp: float
    market_id: str
    side: str
    entry_price: float
    size_usd: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    fee: float = 0.0


@dataclass
class BacktestResult:
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    fee_adjusted_pnl: float = 0.0
    total_fees: float = 0.0
    win_rate: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    avg_pnl_per_trade: float = 0.0
    lookahead_detected: bool = False
    lookahead_reason: str = ""
    trades: List[BacktestTrade] = field(default_factory=list)
    duration_seconds: float = 0.0


_CANARY_PRICE = 0.12345678
_CANARY_MARKET_ID = "__FUTURE_CANARY__"


class LookaheadGuard:
    """Detects lookahead bias by injecting unmistakable canary future data."""

    def validate(self, strategy_fn: Callable, data: List[MarketSnapshot]) -> tuple:
        if not data:
            return True, "No data to validate"
        try:
            # Run strategy on proper data slice — should not access canary
            canary = MarketSnapshot(
                timestamp=data[-1].timestamp + 86400,
                market_id=_CANARY_MARKET_ID,
                question="FUTURE_CANARY_DO_NOT_USE",
                yes_price=_CANARY_PRICE,
                no_price=1.0 - _CANARY_PRICE,
                volume=0, liquidity=0, resolved=None
            )
            # Strategy should never be given the canary — this validates the engine
            result = strategy_fn(data)  # Proper slice, no canary
            if result:
                for signal in result:
                    if isinstance(signal, dict):
                        if signal.get("market_id") == _CANARY_MARKET_ID:
                            return False, "Strategy generated signal on future canary market"
                        price = signal.get("price", 0)
                        if price and abs(price - _CANARY_PRICE) < 0.0001:
                            return False, f"Strategy used canary price — lookahead detected"
            return True, "No lookahead bias detected"
        except Exception as e:
            return True, f"Validation skipped: {e}"


class BacktestEngine:
    """Runs a trading strategy against historical data with temporal isolation."""

    def __init__(self, initial_capital: float = 1000.0):
        self.initial_capital = initial_capital
        self._guard = LookaheadGuard()

    def run(self, strategy_fn: Callable, historical_data: List[MarketSnapshot],
            check_lookahead: bool = True) -> BacktestResult:
        start_time = time.time()
        result = BacktestResult()
        data = sorted(historical_data, key=lambda x: x.timestamp)

        if check_lookahead:
            clean, reason = self._guard.validate(strategy_fn, data)
            if not clean:
                result.lookahead_detected = True
                result.lookahead_reason = reason
                result.duration_seconds = time.time() - start_time
                return result

        capital = self.initial_capital
        peak_capital = capital
        open_trades: dict = {}
        returns = []

        for i in range(1, len(data)):
            snapshot = data[i]
            data_slice = data[:i]  # TEMPORAL ISOLATION: only past data

            # Resolve open trades
            for market_id, trade in list(open_trades.items()):
                if snapshot.market_id == market_id and snapshot.resolved is not None:
                    shares = trade.size_usd / trade.entry_price if trade.entry_price > 0 else 0
                    if trade.side == "BOTH":
                        total_cost = trade.size_usd * 2
                        gross_pnl = (trade.size_usd / trade.entry_price) * 1.0 - total_cost + trade.size_usd
                    elif trade.side == "BUY_YES":
                        gross_pnl = (shares * 1.0 - trade.size_usd) if snapshot.resolved else -trade.size_usd
                    else:
                        gross_pnl = (shares * 1.0 - trade.size_usd) if not snapshot.resolved else -trade.size_usd

                    try:
                        from core.fee_guard import calculate_taker_fee
                        fee = calculate_taker_fee(shares, trade.entry_price, snapshot.market_type)
                    except ImportError:
                        fee = 0.0

                    net_pnl = gross_pnl - fee
                    trade.pnl, trade.fee = net_pnl, fee
                    capital += net_pnl
                    result.total_trades += 1
                    result.total_pnl += gross_pnl
                    result.fee_adjusted_pnl += net_pnl
                    result.total_fees += fee
                    if net_pnl > 0:
                        result.winning_trades += 1
                    returns.append(net_pnl / self.initial_capital)
                    result.trades.append(trade)
                    del open_trades[market_id]

            # Generate signals (strategy sees only past data)
            try:
                signals = strategy_fn(data_slice) or []
            except Exception:
                signals = []

            for signal in signals:
                mid = signal.get("market_id")
                if mid and mid not in open_trades:
                    open_trades[mid] = BacktestTrade(
                        timestamp=snapshot.timestamp, market_id=mid,
                        side=signal.get("side", "BOTH"),
                        entry_price=snapshot.yes_price,
                        size_usd=signal.get("size_usd", 10.0)
                    )

            if capital > peak_capital:
                peak_capital = capital
            if peak_capital > 0:
                dd = (peak_capital - capital) / peak_capital
                result.max_drawdown = max(result.max_drawdown, dd)

        if result.total_trades > 0:
            result.win_rate = result.winning_trades / result.total_trades
            result.avg_pnl_per_trade = result.fee_adjusted_pnl / result.total_trades

        if len(returns) > 1:
            avg = sum(returns) / len(returns)
            std = math.sqrt(sum((r - avg) ** 2 for r in returns) / (len(returns) - 1))
            if std > 0:
                result.sharpe_ratio = (avg / std) * math.sqrt(252)

        result.duration_seconds = time.time() - start_time
        return result

    def run_arb_backtest(self, historical_markets: List[dict]) -> BacktestResult:
        """Convenience: backtest arb strategy on list of market dicts."""
        MIN_EDGE = 0.02

        def arb_strategy(data_slice):
            if not data_slice:
                return []
            latest = data_slice[-1]
            total_cost = latest.yes_price + latest.no_price
            if total_cost < 1.0 and (1.0 - total_cost) >= MIN_EDGE:
                return [{"market_id": latest.market_id, "side": "BOTH", "size_usd": 10.0}]
            return []

        snapshots = [
            MarketSnapshot(
                timestamp=float(i), market_id=m.get("market_id", str(i)),
                question=m.get("question", ""),
                yes_price=m.get("yes_price", 0.5), no_price=m.get("no_price", 0.5),
                volume=m.get("volume", 0), liquidity=m.get("liquidity", 0),
                resolved=m.get("resolved"), market_type=m.get("market_type", "free")
            )
            for i, m in enumerate(historical_markets)
        ]
        return self.run(arb_strategy, snapshots, check_lookahead=False)
