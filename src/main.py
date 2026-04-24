"""Polybot v4 entry point.

Usage:
  python -m src.main             # dry-run (default)
  python -m src.main --live      # real orders, with confirmation prompt

The bot runs until:
  * Ctrl-C (graceful shutdown, flatten open positions)
  * EMERGENCY_STOP file appears in repo root
  * A risk breaker trips that's configured to require manual restart
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from . import __version__
from .binance_feed import BinanceFeed
from .config_loader import Config, load as load_config
from .logger import JsonlWriter, setup as setup_logger
from .market_discovery import ActiveMarket, MarketDiscovery
from .polymarket_client import PolymarketClient
from .polymarket_ws import PolymarketWS
from .position_tracker import Position, PositionTracker
from .profit_taker import ProfitTaker
from .risk_manager import RiskManager
from .safety_guard import (
    ConsecutiveRejectTracker,
    HARD_MAX_BET_USD,
    emergency_stop_file_present,
    enforce_order_envelope,
    require_live_confirmation,
    validate_config_against_hard_caps,
)
from .strategy import LateEntryV3, Signal
from .telegram_notifier import TelegramNotifier
from .trade_logger import TradeLogger

log = logging.getLogger("polybot.main")


class PolyBotV4:
    def __init__(self, cfg: Config, repo_root: Path, dry_run: bool) -> None:
        self.cfg = cfg
        self.repo_root = repo_root
        self.dry_run = dry_run
        self._stop = False

        # External integrations
        enabled_symbols = [c.binance_symbol for c in cfg.coins.values() if c.enabled]
        self.binance = BinanceFeed(cfg.secrets.binance_ws, enabled_symbols)
        self.pm_ws = PolymarketWS(cfg.secrets.polymarket_ws)
        self.pm_client = PolymarketClient(
            clob_host=cfg.secrets.clob_host,
            private_key=cfg.secrets.private_key,
            api_key=cfg.secrets.polymarket_api_key,
            api_secret=cfg.secrets.polymarket_api_secret,
            api_passphrase=cfg.secrets.polymarket_api_passphrase,
            signature_type=cfg.secrets.signature_type,
            funder_address=cfg.secrets.funder_address,
            dry_run=dry_run,
        )
        self.discovery = MarketDiscovery(cfg.secrets.gamma_host, cfg.coins)

        # Core logic
        self.strategy = LateEntryV3(
            min_elapsed_sec=cfg.strategy.min_elapsed_sec,
            max_time_left_sec=cfg.strategy.max_time_left_sec,
            min_entry_price=cfg.strategy.min_entry_price,
            max_entry_price=cfg.strategy.max_entry_price,
            min_favorite_gap_pct=cfg.strategy.min_favorite_gap_pct,
            min_vwap_deviation_pct=cfg.strategy.min_vwap_deviation_pct,
            require_positive_momentum=cfg.strategy.require_positive_momentum,
            momentum_lookback_sec=cfg.strategy.momentum_lookback_sec,
            max_spread_cents=cfg.risk.max_spread_cents,
            max_book_staleness_sec=cfg.risk.max_book_staleness_sec,
            min_book_depth_usd=cfg.risk.min_book_depth_usd,
        )
        self.profit_taker = ProfitTaker(
            stop_loss_usd_per_position=cfg.exit.stop_loss_usd_per_position,
            flip_stop_enabled=cfg.exit.flip_stop_enabled,
            flip_stop_threshold=cfg.exit.flip_stop_threshold,
        )
        self.risk = RiskManager(
            daily_loss_cap_usd=cfg.risk.daily_loss_cap_usd,
            weekly_loss_cap_usd=cfg.risk.weekly_loss_cap_usd,
            consecutive_loss_limit=cfg.risk.consecutive_loss_limit,
            max_concurrent_positions=cfg.risk.max_concurrent_positions,
            min_bankroll_usd=cfg.risk.min_bankroll_usd,
            hard_max_bet_usd=HARD_MAX_BET_USD,
        )
        self.positions = PositionTracker()
        self.trade_logger = TradeLogger(
            trade_path=cfg.logging_.trade_log_path,
            signal_path=cfg.logging_.signal_log_path,
        )
        self.telegram = TelegramNotifier(
            bot_token=cfg.secrets.telegram_bot_token,
            chat_id=cfg.secrets.telegram_chat_id,
            enabled=cfg.telegram.enabled,
        )
        self.reject_tracker = ConsecutiveRejectTracker()
        self._active_markets: Dict[str, ActiveMarket] = {}
        self._last_heartbeat = 0.0

    # --- lifecycle -----------------------------------------------------
    async def run(self) -> None:
        log.info("polybot v%s starting (dry_run=%s)", __version__, self.dry_run)
        self.telegram.send(f"*polybot v4* started (dry_run=`{self.dry_run}`)")

        await self.binance.start()
        await self.pm_ws.start()
        await self.telegram.start()

        # Warm up — wait for at least one Binance sample + one PM book
        await self._warmup()

        tick_sec = self.cfg.strategy.signal_tick_ms / 1000.0
        loop = asyncio.get_event_loop()
        stop_evt = asyncio.Event()

        def _sig(_signum, _frame):
            log.info("signal received, stopping")
            stop_evt.set()

        loop.add_signal_handler(signal.SIGINT, lambda: stop_evt.set())
        loop.add_signal_handler(signal.SIGTERM, lambda: stop_evt.set())

        try:
            while not stop_evt.is_set():
                await self._tick()
                try:
                    await asyncio.wait_for(stop_evt.wait(), timeout=tick_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._shutdown()

    async def _warmup(self) -> None:
        log.info("warming up (waiting 15s for WS connections)")
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.binance.is_healthy(max_staleness_sec=10.0):
                break
            await asyncio.sleep(1)
        # Discover initial markets + subscribe
        await self._refresh_all_markets()

    async def _shutdown(self) -> None:
        log.info("shutting down — flattening positions")
        for p in self.positions.open_positions():
            try:
                await self._close_position(p, reason="shutdown")
            except Exception as e:  # noqa: BLE001
                log.error("flatten failed for %s: %s", p.order_id, e)
        await self.binance.stop()
        await self.pm_ws.stop()
        await self.telegram.stop()
        self.telegram.send("*polybot v4* stopped")
        log.info("goodbye")

    # --- main tick -----------------------------------------------------
    async def _tick(self) -> None:
        # Emergency stop file check
        if emergency_stop_file_present(self.repo_root):
            log.error("EMERGENCY_STOP file present — flattening and exiting")
            self.telegram.send("🛑 *EMERGENCY STOP* — flattening and exiting")
            await self._shutdown()
            sys.exit(1)

        # Heartbeat
        now = time.time()
        if now - self._last_heartbeat >= self.cfg.logging_.heartbeat_sec:
            self._last_heartbeat = now
            open_n = self.positions.open_count()
            log.info(
                "heartbeat — open=%d, day_pnl=$%.2f, binance_ok=%s, paused=%s",
                open_n,
                self.risk.state.day_pnl_usd,
                self.binance.is_healthy(),
                self.risk.state.pause_reason if now < self.risk.state.auto_paused_until else "no",
            )

        # Refresh active markets periodically (they roll every 15 min)
        await self._refresh_all_markets_if_needed()

        # Evaluate exits on every open position
        for p in list(self.positions.open_positions()):
            await self._maybe_exit(p)

        # Evaluate entries per enabled coin
        for coin_name, coin_cfg in self.cfg.coins.items():
            if not coin_cfg.enabled:
                continue
            await self._maybe_enter(coin_name, coin_cfg)

    # --- market discovery ---------------------------------------------
    def _current_spot(self, coin_cfg) -> "Optional[float]":  # type: ignore
        try:
            return self.binance.get(coin_cfg.binance_symbol).last_price
        except Exception:
            return None

    async def _refresh_all_markets(self) -> None:
        for coin_name, coin_cfg in self.cfg.coins.items():
            if not coin_cfg.enabled:
                continue
            m = await self.discovery.get_active(
                coin_name,
                current_spot=self._current_spot(coin_cfg),
                min_fav_price=self.cfg.strategy.min_entry_price,
                max_fav_price=self.cfg.strategy.max_entry_price,
                target_fav_price=float(self.cfg.raw["strategy"].get("target_entry_price", 0.82)),
                force=True,
            )
            if m:
                self._active_markets[coin_name] = m
                self.pm_ws.ensure_subscribed([m.token_id_up, m.token_id_down])
        log.info("initial markets: %s", {k: v.slug for k, v in self._active_markets.items()})

    async def _refresh_all_markets_if_needed(self) -> None:
        for coin_name, coin_cfg in self.cfg.coins.items():
            if not coin_cfg.enabled:
                continue
            cached = self._active_markets.get(coin_name)
            need_refresh = (
                cached is None
                or not cached.is_active
                or cached.seconds_left < 3
                or (time.time() - cached.fetched_at) > 25
            )
            if need_refresh:
                new_m = await self.discovery.get_active(
                    coin_name,
                    current_spot=self._current_spot(coin_cfg),
                    min_fav_price=self.cfg.strategy.min_entry_price,
                    max_fav_price=self.cfg.strategy.max_entry_price,
                    target_fav_price=float(self.cfg.raw["strategy"].get("target_entry_price", 0.82)),
                    force=True,
                )
                if new_m and (cached is None or new_m.slug != cached.slug):
                    self._active_markets[coin_name] = new_m
                    self.pm_ws.ensure_subscribed([new_m.token_id_up, new_m.token_id_down])
                    log.info("picked %s market: %s (strike $%s, ends in %.0fs)",
                             coin_name, new_m.slug, int(new_m.strike), new_m.seconds_left)

    # --- entries ------------------------------------------------------
    async def _maybe_enter(self, coin: str, coin_cfg) -> None:
        if self.reject_tracker.is_paused():
            return

        market = self._active_markets.get(coin)
        if not market or not market.is_active:
            return

        book_up = self.pm_ws.get_book(market.token_id_up)
        book_down = self.pm_ws.get_book(market.token_id_down)
        binance_state = self.binance.get(coin_cfg.binance_symbol)

        signal = self.strategy.evaluate(coin, market, book_up, book_down, binance_state)
        self.trade_logger.log_signal(signal)

        if not signal.fire:
            return

        # Risk manager gate
        bankroll = await self.pm_client.get_usdc_balance()
        decision = self.risk.check_pre_trade(
            bankroll_usd=bankroll,
            open_positions=self.positions.open_count(),
            coin=coin,
            already_open_on_coin=self.positions.has_open_on_coin(coin),
            seconds_left_in_window=signal.seconds_left,
        )
        if not decision.approved:
            log.info("risk skip: %s", decision.reason)
            return

        # Sizing
        bet_usd = self.risk.pick_bet_size(
            seconds_left_in_window=signal.seconds_left,
            bet_usd_above_180=self.cfg.sizing.bet_usd_above_180s_left,
            bet_usd_120_to_180=self.cfg.sizing.bet_usd_120_to_180s_left,
            bet_usd_below_120=self.cfg.sizing.bet_usd_below_120s_left,
        )
        try:
            bet_usd_capped, size_contracts = enforce_order_envelope(bet_usd, signal.entry_price)
        except RuntimeError as e:
            log.warning("safety envelope reject: %s", e)
            return

        # Place order
        log.info(
            "FIRE %s %s @ %.3f — bet=$%.2f size=%.1f left=%.0fs (mom=%.3f%% dev=%.2f%%)",
            coin, signal.side, signal.entry_price, bet_usd_capped, size_contracts,
            signal.seconds_left, signal.binance_momentum_pct or 0,
            signal.vwap_deviation_pct or 0,
        )

        result = await self.pm_client.place_fak_buy(
            token_id=signal.token_id,
            price=signal.entry_price,
            size_contracts=size_contracts,
            simulated_ask=signal.entry_price,
        )

        if not result.success:
            self.reject_tracker.on_failure()
            log.warning("order rejected: %s", result.error)
            return
        self.reject_tracker.on_success()

        p = Position(
            coin=coin,
            market_slug=market.slug,
            condition_id=market.condition_id,
            token_id=signal.token_id,
            opposite_token_id=signal.opposite_token_id,
            side=signal.side,
            entry_price=result.filled_price,
            size_contracts=result.filled_size,
            spent_usd=result.spent_usd,
            opened_at=time.time(),
            order_id=result.order_id or f"pos-{uuid.uuid4().hex[:10]}",
            window_end_ts=market.end_ts,
            last_mark_price=result.filled_price,
            last_update=time.time(),
        )
        self.positions.add(p)
        self.trade_logger.log_entry(p, dry_run=self.dry_run)
        self.telegram.send(
            f"🟢 *Entered* {coin} {signal.side} @ `{result.filled_price:.3f}` "
            f"(${result.spent_usd:.2f}, {result.filled_size:.1f} contracts, "
            f"`{'DRY' if self.dry_run else 'LIVE'}`)"
        )

    # --- exits --------------------------------------------------------
    async def _maybe_exit(self, p: Position) -> None:
        own = self.pm_ws.get_book(p.token_id)
        opp = self.pm_ws.get_book(p.opposite_token_id)

        # Natural resolution: when the window closes, price goes to 0 or 1.
        if p.window_end_ts <= time.time():
            # Determine outcome via Binance window return sign
            coin_cfg = self.cfg.coins.get(p.coin)
            won = None
            if coin_cfg and self.binance.is_healthy():
                st = self.binance.get(coin_cfg.binance_symbol)
                ret = st.window_return_pct(p.window_end_ts - 900)  # 15 min window
                if ret is not None:
                    direction = "UP" if ret > 0 else "DOWN"
                    won = direction == p.side
            close_price = 1.0 if won else 0.0
            realized = (close_price - p.entry_price) * p.size_contracts
            await self._record_close(p, reason="natural", close_price=close_price, realized=realized)
            return

        # Active exit gates (flip-stop, stop-loss)
        if own is None:
            return
        decision = self.profit_taker.evaluate(p, own, opp)
        if own.mid is not None:
            self.positions.update_mark(p.order_id, own.mid)
        if not decision.close:
            return

        # Place exit sell
        result = await self.pm_client.place_fak_sell(
            token_id=p.token_id,
            price=decision.use_limit_price,
            size_contracts=p.size_contracts,
            simulated_bid=own.best_bid,
        )
        if not result.success:
            log.warning("exit sell failed for %s: %s", p.order_id, result.error)
            return
        realized = (result.filled_price - p.entry_price) * result.filled_size
        await self._record_close(p, reason=decision.reason, close_price=result.filled_price, realized=realized)

    async def _close_position(self, p: Position, reason: str) -> None:
        own = self.pm_ws.get_book(p.token_id)
        bid = own.best_bid if own and own.best_bid else 0.01
        result = await self.pm_client.place_fak_sell(
            token_id=p.token_id,
            price=max(0.01, bid - 0.02),
            size_contracts=p.size_contracts,
            simulated_bid=bid,
        )
        realized = (result.filled_price - p.entry_price) * result.filled_size if result.success else 0
        await self._record_close(p, reason=reason, close_price=result.filled_price if result.success else 0.0, realized=realized)

    async def _record_close(self, p: Position, reason: str, close_price: float, realized: float) -> None:
        self.positions.close(p.order_id, close_price=close_price, reason=reason, realized_usd=realized)
        self.risk.on_position_closed(realized)
        self.trade_logger.log_exit(p, reason=reason, close_price=close_price, realized_usd=realized, dry_run=self.dry_run)
        emoji = "🟢" if realized > 0 else "🔴"
        self.telegram.send(
            f"{emoji} *Closed* {p.coin} {p.side} — {reason} — "
            f"PnL `${realized:+.2f}` (entry `{p.entry_price:.3f}` → close `{close_price:.3f}`)"
        )
        log.info(
            "CLOSE %s %s %s @ %.3f — pnl=$%+.2f (%s)",
            p.coin, p.side, p.order_id, close_price, realized, reason,
        )


# ---------------- entry point -----------------------------------------

def _acquire_run_lock(repo_root: Path):
    """Prevent two instances from running simultaneously."""
    lock_path = repo_root / "data" / "polybot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(
            f"Another polybot instance holds the lock at {lock_path}. "
            f"Use `lsof {lock_path}` to find it."
        )
    fh.write(str(__import__("os").getpid()))
    fh.flush()
    return fh


def main() -> None:
    parser = argparse.ArgumentParser(description="Polybot v4 — Late Entry V3")
    parser.add_argument("--live", action="store_true", help="Place real orders")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    cfg = load_config(repo_root)

    setup_logger(cfg.secrets.log_level, cfg.logging_.main_log_path, cfg.logging_.error_log_path)
    log.info("=" * 60)
    log.info("polybot v%s", __version__)
    log.info("repo_root=%s", repo_root)
    log.info("live=%s", args.live)
    log.info("=" * 60)

    validate_config_against_hard_caps(cfg)

    if args.live:
        require_live_confirmation()

    lock = _acquire_run_lock(repo_root)  # noqa: F841 — held for process lifetime

    bot = PolyBotV4(cfg, repo_root=repo_root, dry_run=not args.live)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
