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
from strategies.news_arb import NewsArbitrageStrategy
from strategies.profit_taker import ProfitTakerStrategy
from strategies.ai_forecaster import AIForecasterStrategy
from strategies.swarm_forecaster import SwarmForecasterStrategy
from core.win_rate_monitor import check as check_win_rate, format_discord_alert, load_recalibration
from core.key_vault import init_vault, redacted_repr as vault_repr
from core.reasoning_logger import init_reasoning_logger, log_cycle_summary
from core.order_staging import OrderStagingBuffer
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

    # ── 2. Check Polymarket cash balance via authenticated CLOB API ──
    # Polymarket holds USDC inside the CTF Exchange / proxy wallets — NOT in the
    # wallet address itself. The only reliable source is the authenticated
    # CLOB /balance-allowance endpoint, which reads what Polymarket holds for you.
    try:
        from core.key_vault import is_ready as _vault_ready, get_client as _vault_client
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        if _vault_ready():
            _clob = _vault_client()
            if _clob:
                logger.debug("Fetching Polymarket balance via CLOB /balance-allowance ...")
                creds = _clob.create_or_derive_api_creds()
                _clob.set_api_creds(creds)
                result = _clob.get_balance_allowance(
                    params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                logger.debug(f"CLOB balance-allowance raw response: {result}")
                if isinstance(result, dict):
                    raw = result.get("balance", result.get("availableBalance", 0))
                    bal = float(raw) if raw else 0.0
                    # CLOB may return raw token units (USDC = 6 decimals, $200 → 200_000_000)
                    # or human-readable. Use magnitude to distinguish.
                    if bal > 10_000:
                        bal = bal / 1_000_000
                    balances["polymarket_cash"] = bal
                    # For Magic wallets the on-chain USDC IS the same pool the CLOB
                    # reports — zero it out to avoid double-counting in portfolio value.
                    if bal > 0:
                        balances["usdc"] = 0.0
                    logger.info(f"Polymarket CLOB balance: ${balances['polymarket_cash']:.2f}")

                    # If CLOB shows $0 but on-chain USDC exists, call update_balance_allowance
                    # to tell Polymarket to re-sync the on-chain allowance.
                    if bal == 0 and balances.get("usdc", 0) > 0:
                        logger.info(
                            f"CLOB $0 but on-chain ${balances['usdc']:.2f} — "
                            "calling update_balance_allowance to sync..."
                        )
                        try:
                            _clob.update_balance_allowance(
                                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                            )
                            result2 = _clob.get_balance_allowance(
                                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                            )
                            if isinstance(result2, dict):
                                raw2 = result2.get("balance", result2.get("availableBalance", 0))
                                bal2 = float(raw2) if raw2 else 0.0
                                if bal2 > 10_000:
                                    bal2 = bal2 / 1_000_000
                                if bal2 > 0:
                                    balances["polymarket_cash"] = bal2
                                    balances["usdc"] = 0.0
                                    logger.info(f"Allowance sync OK — CLOB now ${bal2:.2f}")
                                else:
                                    logger.warning(
                                        f"CLOB still $0 after sync. On-chain ${balances.get('usdc', 0):.2f} "
                                        "USDC needs manual deposit at polymarket.com"
                                    )
                        except Exception as _sync_err:
                            logger.warning(f"update_balance_allowance failed: {_sync_err}")
                else:
                    logger.warning(f"CLOB balance-allowance unexpected response type: {type(result)} — {result}")
        else:
            logger.warning("KeyVault not ready at balance check — PRIVATE_KEY may not be loaded")
    except Exception as e:
        logger.warning(f"CLOB balance-allowance check failed: {e}", exc_info=True)

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

        # ── Security Layer 1: Key Vault ──────────────────────────────────────
        # Load private key into vault ONCE. Agents never receive the key directly.
        # Principle: "Don't trust agents with secrets"
        if self.settings.PRIVATE_KEY:
            init_vault(
                self.settings.PRIVATE_KEY,
                self.settings.CLOB_HOST,
                getattr(self.settings, "CHAIN_ID", 137),
                funder_address=getattr(self.settings, "FUNDER_ADDRESS", ""),
            )
            logger.info(f"Security: {vault_repr()}")
        else:
            logger.info("Security: KeyVault uninitialised (paper trading / no key)")

        self.portfolio = Portfolio(self.settings)
        self.risk_manager = RiskManager(self.settings, self.portfolio)
        self.alerter = TelegramAlerter(self.settings)
        # Discord alerter functions are module-level; settings provide DISCORD_WEBHOOK_URL
        self.export_dashboard = export_dashboard

        # ── Security Layer 2: Order Staging Buffer ────────────────────────────
        # Proposed orders are validated before execution.
        # Principle: "Stage and vet all writes"
        self.order_staging = OrderStagingBuffer(self.portfolio, self.settings)

        # ── Security Layer 3: Reasoning Logger ───────────────────────────────
        # Full forensic log of every agent signal and decision.
        # Principle: "Log everything"
        data_dir = str(Path(self.settings.DB_PATH).parent)
        init_reasoning_logger(self.settings.DB_PATH, data_dir)
        logger.info("Security: ReasoningLogger active")

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
        if self.settings.ENABLE_NEWS_ARB:
            self.strategies.append(NewsArbitrageStrategy(self.settings, self.portfolio, self.risk_manager))
        if self.settings.ENABLE_AI_FORECASTER:
            self.strategies.append(AIForecasterStrategy(self.settings, self.portfolio, self.risk_manager))
        if self.settings.ENABLE_SWARM_FORECASTER:
            self.strategies.append(SwarmForecasterStrategy(self.settings, self.portfolio, self.risk_manager))

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
        # Only override with wallet balance if non-zero
        # (Gamma API returns $0 when endpoint fails — fall back to INITIAL_CAPITAL)
        total_wallet = balances.get('usdc', 0) + balances.get('polymarket_cash', 0)
        if total_wallet > 0:
            self.portfolio.set_wallet_balances(balances)
        else:
            logger.warning(
                f"Wallet balance check returned $0 (API may be down) — "
                f"using INITIAL_CAPITAL=${self.settings.INITIAL_CAPITAL:.2f} as portfolio base"
            )

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

        # ── Guard: skip new trades when CLOB balance is too low ──
        _clob_cash = balances.get("polymarket_cash", 0)
        _min_trade = 5.0  # minimum USDC needed to attempt a trade
        _skip_strategies = not self.settings.DRY_RUN and _clob_cash < _min_trade
        if _skip_strategies:
            _on_chain = balances.get("usdc", 0)
            logger.warning(
                f"CLOB balance ${_clob_cash:.2f} < ${_min_trade:.0f} minimum — "
                f"skipping new trades this cycle. On-chain USDC: ${_on_chain:.2f}. "
                f"Deposit at polymarket.com to resume trading."
            )
            await send_error_alert(
                f"Trading paused: Polymarket CLOB ${_clob_cash:.2f} (need ≥${_min_trade:.0f}). "
                f"On-chain wallet has ${_on_chain:.2f} USDC — deposit at polymarket.com.",
                "low_balance"
            )

        # Run each strategy's single scan
        # Fetch open position token IDs once and pass to each strategy so they
        # can skip markets where a position already exists (deduplication).
        open_token_ids = set(self.portfolio.get_open_token_ids())
        logger.info(f"Position deduplication: {len(open_token_ids)} open token IDs loaded")

        for strategy in self.strategies:
            if _skip_strategies:
                continue
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

        # ── Win rate monitoring (debounced: max 1 alert per hour) ──
        try:
            wr_result = check_win_rate(self.portfolio)
            if wr_result.status in ("warn", "recalibrate"):
                _wr_alert_file = Path(self.settings.DATA_DIR) / "wr_last_alert.txt"
                _now_ts = time.time()
                _last_alert = float(_wr_alert_file.read_text()) if _wr_alert_file.exists() else 0.0
                if _now_ts - _last_alert > 3600:  # only alert once per hour
                    alert_msg = format_discord_alert(wr_result)
                    logging.warning(f"Win rate monitor: {wr_result.message}")
                    _wr_alert_file.write_text(str(_now_ts))
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

        # Run Nyx — supreme coordinator (Amara + Pia)
        try:
            from agents.nyx import run_nyx
            await run_nyx()
        except Exception as e:
            logger.error(f"Nyx coordinator failed: {e}", exc_info=True)


def main():
    parser = argparse.ArgumentParser(description="PolyBot - Polymarket Trading Bot")
    parser.add_argument(
        "--export-dashboard",
        action="store_true",
        help="Export dashboard data to JSON for GitHub Pages"
    )
    args = parser.parse_args()

    # ── Run-lock: prevent overlapping cycles ──────────────────────────────────
    # launchd fires every 5 min but a full scan can take longer; if the
    # previous instance is still running, skip rather than double-trade.
    import fcntl
    lock_path = Path("data/.polybot.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another polybot instance is already running — skipping this cycle.", flush=True)
        lock_fh.close()
        return
    try:
        bot = PolyBot(export_dashboard=args.export_dashboard)
        asyncio.run(bot.run_once())
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


if __name__ == "__main__":
    main()
