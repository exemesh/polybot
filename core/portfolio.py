"""
Portfolio tracker - tracks all positions, PnL, and trade history.
Persists to SQLite for the dashboard.

Includes position resolution: auto-closes expired markets and
tracks actual P&L based on market outcomes.
"""

import sqlite3
import json
import logging
from datetime import datetime, date, timezone
from dataclasses import dataclass
from typing import List, Optional, Dict
from pathlib import Path

import httpx

from core.fee_guard import calculate_net_pnl, get_market_type, calculate_taker_fee

logger = logging.getLogger("polybot.portfolio")


@dataclass
class Trade:
    id: Optional[int]
    timestamp: str
    strategy: str
    market_id: str
    market_question: str
    side: str
    token_id: str
    price: float
    size_usd: float
    edge_pct: float
    dry_run: bool
    order_id: Optional[str] = None
    pnl: Optional[float] = None
    status: str = "open"
    closed_at: Optional[str] = None
    close_reason: Optional[str] = None


class Portfolio:
    def __init__(self, settings):
        self.settings = settings
        self.initial_capital = settings.INITIAL_CAPITAL
        self.db_path = settings.DB_PATH
        self._wallet_balances = {"matic": 0.0, "usdc": 0.0, "error": None}
        self._init_db_safe()

    def set_wallet_balances(self, balances: dict):
        """Store on-chain wallet balances from latest check."""
        self._wallet_balances = balances

    def get_wallet_balances(self) -> dict:
        return self._wallet_balances

    def get_open_positions(self) -> List[Dict]:
        """Get all open trade positions."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='open' ORDER BY timestamp DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_closed_positions(self, limit: int = 50) -> List[Dict]:
        """Get recently closed/resolved trade positions."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status IN ('won','lost','resolved','expired') "
                "ORDER BY closed_at DESC, timestamp DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def has_open_position(self, market_id: str) -> bool:
        """Check if we already have an open position in this market (by market_id)."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE market_id=? AND status='open'",
                (market_id,)
            ).fetchone()
            return row[0] > 0

    def has_open_position_by_token(self, token_id: str) -> bool:
        """Check if we already have an open position for this specific token_id.

        This is used for deduplication: before placing any new order, callers
        should call this method and skip the order if it returns True.
        Logs a warning when a duplicate is detected.
        """
        if not token_id:
            return False
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE token_id=? AND status='open'",
                (token_id,)
            ).fetchone()
            duplicate = row[0] > 0
        if duplicate:
            logger.warning(
                f"Duplicate position skipped: token_id={token_id[:20]}... already has an open trade"
            )
        return duplicate

    def get_open_token_ids(self) -> list:
        """Return a list of token_ids for all currently open positions.

        Used for position deduplication: strategies should skip any market
        whose token_id already appears in this list.
        """
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT token_id FROM trades WHERE status='open' AND token_id IS NOT NULL"
                ).fetchall()
                return [r["token_id"] for r in rows if r["token_id"]]
        except sqlite3.DatabaseError as exc:
            logger.warning(f"get_open_token_ids DB error: {exc}")
            return []

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db_safe(self):
        """Initialize the database with graceful recovery on corruption.

        If the DB file is missing, a fresh one is created automatically.
        If the DB file exists but is corrupted (sqlite3.DatabaseError),
        a warning is logged and the file is replaced with a fresh DB so the
        bot can continue operating rather than crashing.
        """
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        if db_path.exists():
            try:
                # Quick integrity probe before init
                conn = sqlite3.connect(str(db_path))
                result = conn.execute("PRAGMA integrity_check").fetchone()
                conn.close()
                if result and result[0] != "ok":
                    raise sqlite3.DatabaseError(
                        f"integrity_check returned: {result[0]}"
                    )
            except sqlite3.DatabaseError as exc:
                logger.warning(
                    f"DB at {self.db_path} is corrupted ({exc}). "
                    "Renaming to .bak and initialising a fresh database."
                )
                bak_path = str(db_path) + ".bak"
                db_path.rename(bak_path)
                logger.warning(f"Corrupt DB backed up to {bak_path}")

        self._init_db()

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    market_id TEXT,
                    market_question TEXT,
                    side TEXT NOT NULL,
                    token_id TEXT,
                    price REAL NOT NULL,
                    size_usd REAL NOT NULL,
                    edge_pct REAL,
                    dry_run INTEGER DEFAULT 1,
                    order_id TEXT,
                    pnl REAL,
                    status TEXT DEFAULT 'open',
                    closed_at TEXT,
                    close_reason TEXT
                );

                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    total_value REAL NOT NULL,
                    cash_balance REAL NOT NULL,
                    deployed_capital REAL NOT NULL,
                    total_pnl REAL NOT NULL,
                    daily_pnl REAL NOT NULL,
                    trade_count INTEGER NOT NULL,
                    win_rate REAL
                );

                CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
                CREATE INDEX IF NOT EXISTS idx_trades_market_id ON trades(market_id);
            """)

            # Migration: add closed_at and close_reason columns if missing
            try:
                conn.execute("SELECT closed_at FROM trades LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE trades ADD COLUMN closed_at TEXT")
                conn.execute("ALTER TABLE trades ADD COLUMN close_reason TEXT")
                logger.info("Migrated DB: added closed_at, close_reason columns")

            # Migration: add market_type column for fee-aware P&L calculation
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN market_type TEXT DEFAULT 'free'")
                conn.commit()
            except Exception:
                pass  # Column already exists

        logger.info(f"Database initialized at {self.db_path}")

    def log_trade(self, trade: Trade) -> int:
        with self._get_conn() as conn:
            cur = conn.execute("""
                INSERT INTO trades
                (timestamp, strategy, market_id, market_question, side, token_id,
                 price, size_usd, edge_pct, dry_run, order_id, pnl, status,
                 closed_at, close_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                trade.strategy, trade.market_id, trade.market_question,
                trade.side, trade.token_id, trade.price, trade.size_usd,
                trade.edge_pct, int(trade.dry_run), trade.order_id,
                trade.pnl, trade.status, trade.closed_at, trade.close_reason
            ))
            trade_id = cur.lastrowid
            logger.debug(f"Trade logged: id={trade_id} strategy={trade.strategy} size=${trade.size_usd:.2f}")
            return trade_id

    def close_trade(self, trade_id: int, pnl: float, status: str = "resolved",
                    reason: str = "market_resolved"):
        """Close a trade with final P&L."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE trades SET pnl=?, status=?, closed_at=?, close_reason=? WHERE id=?",
                (pnl, status, now, reason, trade_id)
            )
        logger.info(f"Trade {trade_id} closed: status={status}, pnl=${pnl:+.4f}, reason={reason}")

    def update_trade_pnl(self, trade_id: int, pnl: float, status: str = "resolved"):
        """Legacy method — use close_trade() for new code."""
        self.close_trade(trade_id, pnl, status, "legacy_update")

    def get_total_pnl(self) -> float:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) as total FROM trades "
                "WHERE pnl IS NOT NULL AND status IN ('won', 'lost', 'resolved')"
            ).fetchone()
            return row["total"]

    def get_daily_pnl(self) -> float:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) as daily FROM trades "
                "WHERE pnl IS NOT NULL AND status IN ('won', 'lost', 'resolved') "
                "AND DATE(timestamp) = DATE('now')"
            ).fetchone()
            return row["daily"]

    def get_deployed_capital(self) -> float:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_usd), 0) as deployed FROM trades WHERE status='open'"
            ).fetchone()
            return row["deployed"]

    def get_win_rate(self) -> dict:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as total, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins "
                "FROM trades WHERE pnl IS NOT NULL AND status IN ('won', 'lost', 'resolved')"
            ).fetchone()
            total = row["total"]
            wins = int(row["wins"] or 0)
            if total == 0:
                return {"win_rate": 0.0, "total": 0, "wins": 0, "losses": 0}
            losses = total - wins
            return {
                "win_rate": round(wins / total * 100, 2),
                "total": total,
                "wins": wins,
                "losses": losses,
            }

    def get_realized_pnl_summary(self) -> dict:
        """Return a breakdown of realized (closed) vs unrealized (open) P&L."""
        with self._get_conn() as conn:
            realized_row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) as total_realized, COUNT(*) as closed_count "
                "FROM trades WHERE pnl IS NOT NULL AND status IN ('won', 'lost', 'resolved')"
            ).fetchone()
            unrealized_row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) as total_unrealized, COUNT(*) as open_count "
                "FROM trades WHERE status = 'open' AND pnl IS NOT NULL"
            ).fetchone()
        return {
            "total_realized": round(float(realized_row["total_realized"]), 4),
            "total_unrealized": round(float(unrealized_row["total_unrealized"]), 4),
            "open_positions": int(unrealized_row["open_count"]),
            "closed_trades": int(realized_row["closed_count"]),
        }

    def get_portfolio_value(self) -> float:
        """Get portfolio value — uses real wallet balance if available, else initial_capital + pnl."""
        # Combine on-chain USDC + Polymarket deposited cash
        usdc = self._wallet_balances.get("usdc", 0)
        poly_cash = self._wallet_balances.get("polymarket_cash", 0)
        total_real = usdc + poly_cash
        if total_real > 0:
            return total_real + self.get_deployed_capital()
        return self.initial_capital + self.get_total_pnl()

    def get_summary(self) -> str:
        total_pnl = self.get_total_pnl()
        daily_pnl = self.get_daily_pnl()
        deployed = self.get_deployed_capital()
        portfolio_val = self.get_portfolio_value()
        win_rate_data = self.get_win_rate()
        pct_return = (total_pnl / self.initial_capital * 100) if self.initial_capital > 0 else 0
        usdc = self._wallet_balances.get("usdc", 0)
        matic = self._wallet_balances.get("matic", 0)
        poly_cash = self._wallet_balances.get("polymarket_cash", 0)

        return (
            f"Portfolio Value: ${portfolio_val:.2f}\n"
            f"Polymarket Cash: ${poly_cash:.2f} | On-chain: ${usdc:.2f} USDC | {matic:.4f} MATIC\n"
            f"Total PnL (Realized): ${total_pnl:+.2f} ({pct_return:+.1f}%)\n"
            f"Today's PnL (Realized): ${daily_pnl:+.2f}\n"
            f"Deployed: ${deployed:.2f}\n"
            f"Win Rate: {win_rate_data['win_rate']:.1f}% ({win_rate_data['wins']}W/{win_rate_data['losses']}L of {win_rate_data['total']} closed)"
        )

    def snapshot(self):
        win_rate_data = self.get_win_rate()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO portfolio_snapshots
                (timestamp, total_value, cash_balance, deployed_capital, total_pnl, daily_pnl, trade_count, win_rate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.utcnow().isoformat(),
                self.get_portfolio_value(),
                self.initial_capital + self.get_total_pnl() - self.get_deployed_capital(),
                self.get_deployed_capital(),
                self.get_total_pnl(),
                self.get_daily_pnl(),
                self._count_trades(),
                win_rate_data["win_rate"]
            ))

    def _count_trades(self) -> int:
        with self._get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    # ─── Position Resolution ─────────────────────────────────────────────────

    async def resolve_positions(self, gamma_host: str = "https://gamma-api.polymarket.com"):
        """Check all open positions and resolve any that have settled.

        For each open trade:
        1. Query Gamma API for current market status
        2. If market is resolved/closed → calculate P&L and close position
        3. For arb trades (BOTH sides) → always profit when market resolves
        4. For value trades → profit if our side won
        """
        open_trades = self.get_open_positions()
        if not open_trades:
            return

        logger.info(f"Resolving {len(open_trades)} open positions...")
        resolved_count = 0

        # Group by market_id to batch API calls
        market_ids = set(t["market_id"] for t in open_trades if t.get("market_id"))

        market_status = {}
        for mid in market_ids:
            status = await self._check_market_status(mid, gamma_host)
            if status:
                market_status[mid] = status

        for trade in open_trades:
            mid = trade.get("market_id", "")
            if mid not in market_status:
                continue

            status = market_status[mid]
            if not status.get("resolved", False) and not status.get("closed", False):
                continue

            # Market is resolved — calculate P&L
            pnl = self._calculate_pnl(trade, status)
            win_status = "won" if pnl > 0 else "lost" if pnl < 0 else "resolved"

            self.close_trade(
                trade["id"], pnl, win_status,
                f"market_resolved: {status.get('winning_outcome', 'unknown')}"
            )
            resolved_count += 1

        if resolved_count > 0:
            logger.info(f"Resolved {resolved_count} positions")

    async def _check_market_status(self, condition_id: str,
                                    gamma_host: str) -> Optional[Dict]:
        """Query Gamma API for market resolution status."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{gamma_host}/markets",
                    params={"condition_id": condition_id, "limit": 1}
                )
                if resp.status_code != 200:
                    return None

                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    m = data[0]
                elif isinstance(data, dict):
                    m = data
                else:
                    return None

                resolved = m.get("resolved", False) or m.get("closed", False)
                winning_outcome = m.get("winningOutcome", m.get("winning_outcome"))

                # Get outcome prices (1.0 for winner, 0.0 for loser)
                outcome_prices = {}
                outcomes_raw = m.get("outcomePrices", m.get("outcome_prices", "[]"))
                outcomes_names = m.get("outcomes", '["Yes", "No"]')

                try:
                    if isinstance(outcomes_raw, str):
                        prices = json.loads(outcomes_raw)
                    else:
                        prices = outcomes_raw

                    if isinstance(outcomes_names, str):
                        names = json.loads(outcomes_names)
                    else:
                        names = outcomes_names

                    for i, name in enumerate(names):
                        if i < len(prices):
                            outcome_prices[name.upper()] = float(prices[i])
                except Exception:
                    pass

                return {
                    "resolved": resolved,
                    "closed": m.get("closed", False),
                    "winning_outcome": winning_outcome,
                    "outcome_prices": outcome_prices,
                    "end_date": m.get("endDateIso", m.get("endDate", "")),
                }
        except Exception as e:
            logger.debug(f"Market status check failed for {condition_id[:16]}: {e}")
            return None

    def _calculate_pnl(self, trade: Dict, market_status: Dict) -> float:
        """Calculate realized P&L for a resolved trade.

        Arb trades (side=BOTH): Buy YES + NO for < $1 → guaranteed $1 payout
          PnL = size_usd * (1.0 / entry_price - 1) minus fees

        Value trades (BUY_YES/BUY_NO): Profit if our outcome wins
          Win: PnL = size_usd * (1.0 / entry_price - 1) = tokens_owned * $1 - cost
          Lose: PnL = -size_usd (total loss)
        """
        side = trade.get("side", "")
        entry_price = trade.get("price", 0)
        size_usd = trade.get("size_usd", 0)
        winning = market_status.get("winning_outcome", "")
        outcome_prices = market_status.get("outcome_prices", {})

        if side == "BOTH":
            # Arb trade: bought YES + NO for entry_price (combined)
            # Payout is always $1.00 per pair
            if entry_price > 0:
                tokens_per_side = size_usd / 2 / (entry_price / 2)
                gross_pnl = tokens_per_side * 1.0 - size_usd
                # Use fee_guard for accurate per-market fee calculation
                mtype = get_market_type(market_question=trade.get("market_question", "") or "")
                shares = size_usd / entry_price if entry_price > 0 else 0
                pnl = calculate_net_pnl(gross_pnl, shares, entry_price, mtype, sides=2)
            else:
                pnl = 0.0

        elif side in ("BUY_YES", "BUY"):
            # We bought YES tokens
            mtype = get_market_type(market_question=trade.get("market_question", "") or "")
            shares = size_usd / entry_price if entry_price > 0 else 0
            if winning and winning.upper() == "YES":
                # WIN: tokens pay out $1 each
                tokens_owned = size_usd / entry_price if entry_price > 0 else 0
                gross_pnl = tokens_owned * 1.0 - size_usd
                fee = calculate_taker_fee(shares, entry_price, mtype)
                pnl = gross_pnl - fee
            elif winning and winning.upper() == "NO":
                # LOSE: YES tokens worthless
                pnl = -size_usd
            else:
                # Use outcome_prices if available
                yes_final = outcome_prices.get("YES", 0)
                if yes_final > 0.5:
                    tokens_owned = size_usd / entry_price if entry_price > 0 else 0
                    gross_pnl = tokens_owned * yes_final - size_usd
                    fee = calculate_taker_fee(shares, entry_price, mtype)
                    pnl = gross_pnl - fee
                else:
                    pnl = -size_usd

        elif side == "BUY_NO":
            # We bought NO tokens
            mtype = get_market_type(market_question=trade.get("market_question", "") or "")
            shares = size_usd / entry_price if entry_price > 0 else 0
            if winning and winning.upper() == "NO":
                tokens_owned = size_usd / entry_price if entry_price > 0 else 0
                gross_pnl = tokens_owned * 1.0 - size_usd
                fee = calculate_taker_fee(shares, entry_price, mtype)
                pnl = gross_pnl - fee
            elif winning and winning.upper() == "YES":
                pnl = -size_usd
            else:
                no_final = outcome_prices.get("NO", 0)
                if no_final > 0.5:
                    tokens_owned = size_usd / entry_price if entry_price > 0 else 0
                    gross_pnl = tokens_owned * no_final - size_usd
                    fee = calculate_taker_fee(shares, entry_price, mtype)
                    pnl = gross_pnl - fee
                else:
                    pnl = -size_usd
        else:
            pnl = 0.0

        return round(pnl, 4)

    # ─── Dashboard JSON Export ────────────────────────────────────────────────

    def export_dashboard_json(self, output_path: str = "dashboard/dashboard_data.json",
                               extra_data: dict = None):
        """Export all dashboard data to a single JSON file for GitHub Pages."""
        total_pnl = self.get_total_pnl()
        daily_pnl = self.get_daily_pnl()
        deployed = self.get_deployed_capital()
        portfolio_val = self.get_portfolio_value()
        win_rate_data = self.get_win_rate()
        pnl_summary = self.get_realized_pnl_summary()
        total_trades = self._count_trades()
        pct_return = (total_pnl / self.initial_capital * 100) if self.initial_capital > 0 else 0

        # Available cash = total portfolio minus what's locked in open trades
        available_cash = round(portfolio_val - deployed, 2)

        # Summary
        summary = {
            "portfolio_value": round(portfolio_val, 2),
            "available_cash": available_cash,
            "total_pnl": round(total_pnl, 2),
            "realized_pnl": pnl_summary["total_realized"],
            "unrealized_pnl": pnl_summary["total_unrealized"],
            "daily_pnl": round(daily_pnl, 2),
            "pct_return": round(pct_return, 2),
            "deployed_capital": round(deployed, 2),
            "win_rate": win_rate_data["win_rate"],
            "total_trades": total_trades,
            "closed_trades": pnl_summary["closed_trades"],
            "open_positions_count": pnl_summary["open_positions"],
            "initial_capital": self.initial_capital,
        }

        # Recent trades
        with self._get_conn() as conn:
            trade_rows = conn.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()
            trades = [dict(r) for r in trade_rows]

        # PnL series
        with self._get_conn() as conn:
            pnl_rows = conn.execute("""
                SELECT DATE(timestamp) as day,
                       COALESCE(SUM(pnl), 0) as daily_pnl,
                       COUNT(*) as trade_count
                FROM trades
                WHERE pnl IS NOT NULL AND timestamp >= DATE('now', '-30 days')
                GROUP BY DATE(timestamp)
                ORDER BY day
            """).fetchall()

            cumulative = self.initial_capital
            pnl_series = []
            for r in pnl_rows:
                cumulative += r["daily_pnl"]
                pnl_series.append({
                    "day": r["day"],
                    "daily_pnl": round(r["daily_pnl"], 4),
                    "portfolio_value": round(cumulative, 2),
                    "trade_count": r["trade_count"],
                })

        # Strategy breakdown
        with self._get_conn() as conn:
            strat_rows = conn.execute("""
                SELECT strategy,
                       COUNT(*) as total_trades,
                       COALESCE(SUM(pnl), 0) as total_pnl,
                       COALESCE(AVG(pnl), 0) as avg_pnl,
                       COALESCE(MAX(pnl), 0) as best_trade,
                       COALESCE(MIN(pnl), 0) as worst_trade,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                       COALESCE(SUM(size_usd), 0) as total_volume
                FROM trades
                WHERE pnl IS NOT NULL
                GROUP BY strategy
            """).fetchall()
            strategy_breakdown = [dict(r) for r in strat_rows]

        # Open positions
        with self._get_conn() as conn:
            pos_rows = conn.execute(
                "SELECT * FROM trades WHERE status='open' ORDER BY timestamp DESC"
            ).fetchall()
            open_positions = [dict(r) for r in pos_rows]

        # Closed positions (recently resolved)
        with self._get_conn() as conn:
            closed_rows = conn.execute(
                "SELECT * FROM trades WHERE status IN ('won','lost','resolved','expired') "
                "ORDER BY closed_at DESC, timestamp DESC LIMIT 50"
            ).fetchall()
            closed_positions = [dict(r) for r in closed_rows]

        # Health
        last_trade = trades[0]["timestamp"] if trades else None
        bot_active = False
        if last_trade:
            try:
                last_dt = datetime.fromisoformat(last_trade)
                bot_active = (datetime.utcnow() - last_dt).total_seconds() < 3600
            except Exception:
                pass

        dashboard_data = {
            "summary": summary,
            "wallet": {
                "matic": round(self._wallet_balances.get("matic", 0), 4),
                "usdc": round(self._wallet_balances.get("usdc", 0), 2),
                "polymarket_cash": round(self._wallet_balances.get("polymarket_cash", 0), 2),
                "address": self.settings.FUNDER_ADDRESS,
            },
            "trades": trades,
            "pnl_series": pnl_series,
            "strategy_breakdown": strategy_breakdown,
            "open_positions": open_positions,
            "closed_positions": closed_positions,
            "health": {
                "bot_active": bot_active,
                "last_trade": last_trade,
                "timestamp": datetime.utcnow().isoformat(),
            },
            "exported_at": datetime.utcnow().isoformat(),
        }

        # Merge any extra data (e.g., control panel state)
        if extra_data:
            dashboard_data.update(extra_data)

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(dashboard_data, indent=2, default=str))
        logger.info(f"Dashboard data exported to {output_path}")
