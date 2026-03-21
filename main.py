#!/usr/bin/env python3
"""
PolyBot - Polymarket Multi-Strategy Trading Bot

Strategies: WeatherArb | CrossPlatformArb | GeneralScanner | MomentumScalper
            SpreadCapture | SportsIntel | AIForecaster + ProfitTaker (active sell/stop-loss)

Arb trades: $3/trade (guaranteed profit) | Value trades: $1/trade
Starting capital: $100 USDC on Polygon

Runs as a single scan-and-trade cycle (designed for GitHub Actions cron).
"""

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

import httpx

from datetime import datetime, timezone

from config.settings import Settings
from core.bot_control import load_control, save_control
from core.portfolio import Portfolio
from core.risk_manager import RiskManager
from strategies.weather_arb import WeatherArbStrategy
from strategies.market_maker import MarketMakerStrategy
from strategies.cross_platform_arb import CrossPlatformArbStrategy
from strategies.general_scanner import GeneralScannerStrategy
from strategies.momentum_scalper import MomentumScalperStrategy
from strategies.spread_capture import SpreadCaptureStrategy
from strategies.sports_intel import SportsIntelStrategy
from strategies.profit_taker import ProfitTakerStrategy
from strategies.ai_forecaster import AIForecasterStrategy
from core.win_rate_monitor import check as check_win_rate, format_discord_alert, load_recalibration
from utils.logger import setup_logger
from utils.telegram_alerts import TelegramAlerter
from utils.discord_alerts import (
    send_trade_alert,
    send_pnl_update,
    send_error_alert,
    send_bot_status,
)

logger = setup_logger("polybot.main")


def verify_db_integrity(db_path: str) -> tuple[bool, str]:
    """Check that the SQLite database exists and is readable/not corrupted.

    Returns:
        (ok: bool, message: str)
        ok=True  → DB is healthy, safe to trade.
        ok=False → DB is missing or corrupted; caller should skip live trading.
    """
    path = Path(db_path)
    if not path.exists():
        msg = f"Database not found at {db_path} — will be created on first write"
        logger.warning(msg)
        # Not an error; portfolio will create it on _init_db()
        return True, msg

    try:
        conn = sqlite3.connect(db_path)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if result and result[0] == "ok":
            logger.info(f"DB integrity check passed: {db_path}")
            return True, "ok"
        else:
            msg = f"DB integrity_check returned: {result}"
            logger.critical(f"CRITICAL: SQLite integrity check FAILED — {msg}")
            return False, msg
    except sqlite3.DatabaseError as exc:
        msg = f"DB is corrupted or unreadable: {exc}"
        logger.critical(f"CRITICAL: {msg}")
        return False, msg
    except Exception as exc:
        msg = f"Unexpected DB check error: {exc}"
        logger.critical(f"CRITICAL: {msg}")
        return False, msg


POLYGON_RPC_URLS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
]

# Polymarket Conditional Tokens Framework (CTF) Exchange contract on Polygon
# Funds deposited to Polymarket live inside the CTF Exchange, not in the user's EOA.
POLYMARKET_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# USDC contracts on Polygon
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


async def check_wallet_balance(address: str, clob_host: str = "https://clob.polymarket.com") -> dict:
    """Check wallet balances: on-chain MATIC/USDC + Polymarket cash balance via CLOB API."""
    balances = {"matic": 0.0, "usdc": 0.0, "polymarket_cash": 0.0, "error": None}
    if not address:
        balances["error"] = "No wallet address configured"
        return balances

    padded_addr = address.lower().replace("0x", "").zfill(64)

    # ── 1. Check on-chain balances via Polygon RPC ──
    for rpc_url in POLYGON_RPC_URLS:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # MATIC balance
                resp = await client.post(rpc_url, json={
                    "jsonrpc": "2.0", "method": "eth_getBalance",
                    "params": [address, "latest"], "id": 1
                })
                data = resp.json()
                if "result" in data:
                    balances["matic"] = int(data["result"], 16) / 1e18

                # Native USDC balance
                resp2 = await client.post(rpc_url, json={
                    "jsonrpc": "2.0", "method": "eth_call",
                    "params": [{"to": USDC_NATIVE, "data": f"0x70a08231{padded_addr}"}, "latest"],
                    "id": 2
                })
                data2 = resp2.json()
                if "result" in data2 and data2["result"] not in ("0x", "0x0", ""):
                    balances["usdc"] = int(data2["result"], 16) / 1e6

                # Bridged USDC.e balance
                resp3 = await client.post(rpc_url, json={
                    "jsonrpc": "2.0", "method": "eth_call",
                    "params": [{"to": USDC_BRIDGED, "data": f"0x70a08231{padded_addr}"}, "latest"],
                    "id": 3
                })
                data3 = resp3.json()
                if "result" in data3 and data3["result"] not in ("0x", "0x0", ""):
                    balances["usdc"] += int(data3["result"], 16) / 1e6

                logger.info(f"On-chain via {rpc_url.split('/')[2]}: "
                          f"{balances['matic']:.4f} MATIC | ${balances['usdc']:.2f} USDC")
                break  # RPC success — stop trying others

        except Exception as e:
            logger.debug(f"RPC {rpc_url} failed: {e}")
            continue

    # ── 2. Check Polymarket cash balance via Gamma API ──
    # Polymarket holds USDC inside proxy wallets / CTF Exchange.
    # The actual "cash" is visible via the Gamma portfolio API.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://gamma-api.polymarket.com/balances",
                params={"address": address},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    balances["polymarket_cash"] = float(data.get("cash", data.get("balance", 0)))
                elif isinstance(data, list) and len(data) > 0:
                    balances["polymarket_cash"] = float(data[0].get("cash", data[0].get("balance", 0)))
                logger.info(f"Polymarket cash balance: ${balances['polymarket_cash']:.2f}")
    except Exception as e:
        logger.debug(f"Polymarket balance API failed: {e}")

    # Total USDC = on-chain + Polymarket deposits
    total = balances["usdc"] + balances["polymarket_cash"]
    logger.info(f"Total available: ${total:.2f} (on-chain: ${balances['usdc']:.2f} + Polymarket: ${balances['polymarket_cash']:.2f})")

    return balances


class PolyBot:
    def __init__(self, export_dashboard: bool = False):
        self.settings = Settings()

        # ── DB integrity check at startup ──
        db_ok, db_msg = verify_db_integrity(self.settings.DB_PATH)
        self._db_skip_trading = not db_ok
        if self._db_skip_trading:
            logger.critical(
                f"Skipping live trading this cycle due to DB integrity failure: {db_msg}"
            )

        # ── Load control file (dashboard kill switch / mode toggle) ──
        self.control = load_control()

        # Override DRY_RUN from control file
        if self.control.mode == "live":
            if self.settings.PRIVATE_KEY:
                self.settings.DRY_RUN = False
            else:
                logger.critical("Cannot switch to LIVE mode: PRIVATE_KEY not configured. Staying in DRY RUN.")
                self.control.mode = "dry_run"
                self.settings.DRY_RUN = True
        else:
            self.settings.DRY_RUN = True

        self.portfolio = Portfolio(self.settings)
        self.risk_manager = RiskManager(self.settings, self.portfolio)
        self.alerter = TelegramAlerter(self.settings)
        # Discord alerter functions are module-level; settings provide DISCORD_WEBHOOK_URL
        self.export_dashboard = export_dashboard

        # ── Apply emergency halt from control file ──
        if self.control.is_halted:
            reason = self.control.halt_reason or "Emergency stop activated from dashboard"
            self.risk_manager._halt_trading(reason)
            logger.critical(f"TRADING HALTED by control file: {reason}")

        # Profit taker runs BEFORE strategies to close profitable positions
        self.profit_taker = ProfitTakerStrategy(self.settings, self.portfolio, self.risk_manager)

        self.strategies = []
        if self.settings.ENABLE_WEATHER_ARB:
            self.strategies.append(WeatherArbStrategy(self.settings, self.portfolio, self.risk_manager))
        if self.settings.ENABLE_MARKET_MAKER:
            self.strategies.append(MarketMakerStrategy(self.settings, self.portfolio, self.risk_manager))
        if self.settings.ENABLE_CROSS_PLATFORM_ARB:
            self.strategies.append(CrossPlatformArbStrategy(self.settings, self.portfolio, self.risk_manager))
        if self.settings.ENABLE_GENERAL_SCANNER:
            self.strategies.append(GeneralScannerStrategy(self.settings, self.portfolio, self.risk_manager))
        if self.settings.ENABLE_MOMENTUM_SCALPER:
            self.strategies.append(MomentumScalperStrategy(self.settings, self.portfolio, self.risk_manager))
        if self.settings.ENABLE_SPREAD_CAPTURE:
            self.strategies.append(SpreadCaptureStrategy(self.settings, self.portfolio, self.risk_manager))
        if self.settings.ENABLE_SPORTS_INTEL:
            self.strategies.append(SportsIntelStrategy(self.settings, self.portfolio, self.risk_manager))
        if self.settings.ENABLE_AI_FORECASTER:
            self.strategies.append(AIForecasterStrategy(self.settings, self.portfolio, self.risk_manager))

        logger.info(f"PolyBot initialized with {len(self.strategies)} strategies")
        mode_str = "LIVE TRADING" if not self.settings.DRY_RUN else "DRY RUN"
        halt_str = " [HALTED]" if self.control.is_halted else ""
        logger.info(f"Mode: {mode_str}{halt_str}")

    async def run_once(self):
        """Single scan-and-trade cycle (for GitHub Actions cron)."""
        logger.info("=" * 60)
        logger.info("  PolyBot - Single Cycle")
        logger.info(f"  Capital: ${self.settings.INITIAL_CAPITAL}")
        logger.info(f"  Strategies: {[s.__class__.__name__ for s in self.strategies]}")
        logger.info(f"  DRY_RUN: {self.settings.DRY_RUN}")
        logger.info("=" * 60)

        # ── Check on-chain wallet balance ──
        wallet_addr = self.settings.FUNDER_ADDRESS
        balances = await check_wallet_balance(wallet_addr)
        if balances["error"]:
            logger.warning(f"Wallet balance check: {balances['error']}")
        else:
            poly_cash = balances.get('polymarket_cash', 0)
            on_chain = balances.get('usdc', 0)
            logger.info(f"Wallet: Polymarket ${poly_cash:.2f} | On-chain ${on_chain:.2f} USDC | {balances['matic']:.4f} MATIC")
        self.portfolio.set_wallet_balances(balances)

        mode_str = "DRY RUN" if self.settings.DRY_RUN else "LIVE"
        portfolio_val = self.portfolio.get_portfolio_value()
        open_count = len(self.portfolio.get_open_positions())

        await self.alerter.send(
            f"PolyBot scan started\n"
            f"Mode: {mode_str}\n"
            f"Portfolio: ${portfolio_val:.2f}\n"
            f"Polymarket cash: ${balances.get('polymarket_cash', 0):.2f}"
        )
        await send_bot_status(mode_str, portfolio_val, open_count)

        # If DB is corrupted, skip all trading for this cycle
        if self._db_skip_trading:
            await send_error_alert(
                "Database integrity check failed — skipping trading this cycle. "
                "Check logs and restore DB from backup.",
                "startup"
            )
            logger.critical("Aborting trading cycle due to DB integrity failure.")
            return

        # Check if risk manager needs daily reset
        self.risk_manager.check_daily_reset()

        # ── Resolve open positions (check for market settlements) ──
        try:
            await self.portfolio.resolve_positions()
        except Exception as e:
            logger.error(f"Position resolution failed: {e}", exc_info=True)

        # ── PROFIT TAKING: Check open positions and sell for profit ──
        # This runs BEFORE new trades to:
        # 1. Realize profits on positions that moved up 25%+
        # 2. Cut losses on positions that dropped 50%+
        # 3. Free up capital locked in stale positions
        try:
            await self.profit_taker.run_once()
        except Exception as e:
            logger.error(f"Profit taker failed: {e}", exc_info=True)

        # Run each strategy's single scan
        # Fetch open position token IDs once and pass to each strategy so they
        # can skip markets where a position already exists (deduplication).
        open_token_ids = set(self.portfolio.get_open_token_ids())
        logger.info(f"Position deduplication: {len(open_token_ids)} open token IDs loaded")

        for strategy in self.strategies:
            try:
                # Inject the current set of open token IDs if the strategy
                # accepts it, otherwise fall back to the no-arg call.
                try:
                    await strategy.run_once(open_token_ids=open_token_ids)
                except TypeError:
                    await strategy.run_once()
                # Refresh after each strategy so the next one sees new positions
                open_token_ids = set(self.portfolio.get_open_token_ids())
            except Exception as e:
                logger.error(f"Strategy {strategy.__class__.__name__} failed: {e}", exc_info=True)
                await send_error_alert(str(e), strategy.__class__.__name__)

        # Take portfolio snapshot
        self.portfolio.snapshot()

        # Log open positions summary
        open_positions = self.portfolio.get_open_positions()
        if open_positions:
            logger.info(f"Open Positions ({len(open_positions)}):")
            for pos in open_positions:
                logger.info(f"  - {pos.get('market_question', 'N/A')[:60]} | "
                          f"${pos.get('size_usd', 0):.2f} invested | "
                          f"Edge: {pos.get('edge_pct', 0):.1%}")
        else:
            logger.info("No open positions")

        # Report results
        summary = self.portfolio.get_summary()
        logger.info(f"Scan complete:\n{summary}")
        await self.alerter.send(f"Scan complete\n{summary}")

        # Send Discord PnL summary
        win_rate_data = self.portfolio.get_win_rate()
        win_rate = win_rate_data.get('win_rate', 0.0) if isinstance(win_rate_data, dict) else win_rate_data
        await send_pnl_update(
            total_pnl=self.portfolio.get_total_pnl(),
            daily_pnl=self.portfolio.get_daily_pnl(),
            win_rate=win_rate,
            open_positions=len(self.portfolio.get_open_positions()),
        )

        # ── Win rate monitoring ──
        try:
            wr_result = check_win_rate(self.portfolio)
            if wr_result.status in ("warn", "recalibrate"):
                alert_msg = format_discord_alert(wr_result)
                logging.warning(f"Win rate monitor: {wr_result.message}")
                # Post to Sentinel channel if webhook available
                sentinel_webhook = getattr(self.settings, 'DISCORD_WEBHOOK_SENTINEL',
                                           os.getenv('DISCORD_WEBHOOK_SENTINEL', ''))
                if sentinel_webhook:
                    try:
                        import requests
                        requests.post(sentinel_webhook, json={"content": alert_msg}, timeout=10)
                    except Exception:
                        pass
        except Exception as e:
            logging.warning(f"Win rate monitor error: {e}")

        # ── Update control state ──
        self.control.last_bot_run = datetime.now(timezone.utc).isoformat()
        self.control.updated_by = "bot"
        self.control.updated_at = datetime.now(timezone.utc).isoformat()

        # If risk manager halted during this run, reflect in control file
        if self.risk_manager.is_halted() and self.control.trading_enabled:
            self.control.trading_enabled = False
            self.control.halt_reason = self.risk_manager._halt_reason

        save_control(self.control)

        # Export dashboard data for GitHub Pages (include control state)
        if self.export_dashboard:
            control_data = {
                "control": {
                    "mode": self.control.mode,
                    "trading_enabled": self.control.trading_enabled,
                    "halt_reason": self.control.halt_reason,
                    "updated_by": self.control.updated_by,
                    "updated_at": self.control.updated_at,
                    "last_bot_run": self.control.last_bot_run,
                }
            }
            self.portfolio.export_dashboard_json(
                "dashboard/dashboard_data.json", extra_data=control_data
            )
            logger.info("Dashboard data exported")

        # Cleanup strategies
        for strategy in self.strategies:
            await strategy.cleanup()
        await self.profit_taker.cleanup()

        # Run the Apex agent swarm (Scout, Analyst, Guardian)
        try:
            from agents.apex import run_apex
            await run_apex()
        except Exception as e:
            logger.error(f"Apex coordinator failed: {e}", exc_info=True)


def main():
    parser = argparse.ArgumentParser(description="PolyBot - Polymarket Trading Bot")
    parser.add_argument(
        "--export-dashboard",
        action="store_true",
        help="Export dashboard data to JSON for GitHub Pages"
    )
    args = parser.parse_args()

    bot = PolyBot(export_dashboard=args.export_dashboard)
    asyncio.run(bot.run_once())


if __name__ == "__main__":
    main()
