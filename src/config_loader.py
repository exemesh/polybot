"""Load and validate config.json + .env. Fails fast on bad config."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv


@dataclass
class CoinConfig:
    name: str
    enabled: bool
    binance_symbol: str
    series_id: int
    series_slug: str
    window_length_sec: int


@dataclass
class StrategyConfig:
    min_elapsed_sec: int
    max_time_left_sec: int
    min_entry_price: float
    max_entry_price: float
    min_favorite_gap_pct: float
    min_vwap_deviation_pct: float
    require_positive_momentum: bool
    momentum_lookback_sec: int
    signal_tick_ms: int


@dataclass
class SizingConfig:
    bet_usd_above_180s_left: float
    bet_usd_120_to_180s_left: float
    bet_usd_below_120s_left: float


@dataclass
class RiskConfig:
    daily_loss_cap_usd: float
    weekly_loss_cap_usd: float
    consecutive_loss_limit: int
    max_concurrent_positions: int
    min_bankroll_usd: float
    max_spread_cents: int
    max_book_staleness_sec: float
    min_book_depth_usd: float


@dataclass
class ExitConfig:
    stop_loss_usd_per_position: float
    flip_stop_enabled: bool
    flip_stop_threshold: float


@dataclass
class BlackoutConfig:
    skip_first_n_minutes_of_window: int
    skip_weekends: bool


@dataclass
class LoggingConfig:
    heartbeat_sec: int
    trade_log_path: str
    signal_log_path: str
    error_log_path: str
    main_log_path: str


@dataclass
class TelegramConfig:
    enabled: bool
    alert_on_trade: bool
    alert_on_daily_pnl: bool
    alert_on_error: bool
    alert_on_auto_pause: bool
    daily_summary_utc_hour: int


@dataclass
class Secrets:
    private_key: str
    polymarket_api_key: str
    polymarket_api_secret: str
    polymarket_api_passphrase: str
    rpc_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    signature_type: int
    funder_address: str
    clob_host: str
    gamma_host: str
    polymarket_ws: str
    binance_ws: str
    log_level: str


@dataclass
class Config:
    coins: Dict[str, CoinConfig]
    strategy: StrategyConfig
    sizing: SizingConfig
    risk: RiskConfig
    exit: ExitConfig
    blackouts: BlackoutConfig
    logging_: LoggingConfig
    telegram: TelegramConfig
    secrets: Secrets
    raw: Dict[str, Any] = field(default_factory=dict)


def _require(v: str, name: str) -> str:
    if not v:
        raise RuntimeError(
            f"Missing required env var: {name}. Copy .env.example to .env and fill it in."
        )
    return v


def load(repo_root: Path) -> Config:
    """Load .env + config/config.json and validate."""
    env_path = repo_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    cfg_path = repo_root / "config" / "config.json"
    if not cfg_path.exists():
        raise RuntimeError(f"config/config.json not found at {cfg_path}")

    with cfg_path.open() as f:
        raw = json.load(f)

    # --- coins
    coins = {}
    for name, c in raw["coins"].items():
        coins[name] = CoinConfig(
            name=name,
            enabled=bool(c["enabled"]),
            binance_symbol=c["binance_symbol"],
            series_id=int(c["series_id"]),
            series_slug=c.get("series_slug", ""),
            window_length_sec=int(c.get("window_length_sec", 3600)),
        )

    # --- strategy
    s = raw["strategy"]
    strategy = StrategyConfig(
        min_elapsed_sec=int(s["min_elapsed_sec"]),
        max_time_left_sec=int(s["max_time_left_sec"]),
        min_entry_price=float(s["min_entry_price"]),
        max_entry_price=float(s["max_entry_price"]),
        min_favorite_gap_pct=float(s["min_favorite_gap_pct"]),
        min_vwap_deviation_pct=float(s["min_vwap_deviation_pct"]),
        require_positive_momentum=bool(s["require_positive_momentum"]),
        momentum_lookback_sec=int(s["momentum_lookback_sec"]),
        signal_tick_ms=int(s["signal_tick_ms"]),
    )

    # --- sizing
    z = raw["sizing"]
    sizing = SizingConfig(
        bet_usd_above_180s_left=float(z["bet_usd_above_180s_left"]),
        bet_usd_120_to_180s_left=float(z["bet_usd_120_to_180s_left"]),
        bet_usd_below_120s_left=float(z["bet_usd_below_120s_left"]),
    )

    # --- risk
    r = raw["risk"]
    risk = RiskConfig(
        daily_loss_cap_usd=float(r["daily_loss_cap_usd"]),
        weekly_loss_cap_usd=float(r["weekly_loss_cap_usd"]),
        consecutive_loss_limit=int(r["consecutive_loss_limit"]),
        max_concurrent_positions=int(r["max_concurrent_positions"]),
        min_bankroll_usd=float(r["min_bankroll_usd"]),
        max_spread_cents=int(r["max_spread_cents"]),
        max_book_staleness_sec=float(r["max_book_staleness_sec"]),
        min_book_depth_usd=float(r["min_book_depth_usd"]),
    )

    # --- exit
    e = raw["exit"]
    exit_ = ExitConfig(
        stop_loss_usd_per_position=float(e["stop_loss_usd_per_position"]),
        flip_stop_enabled=bool(e["flip_stop_enabled"]),
        flip_stop_threshold=float(e["flip_stop_threshold"]),
    )

    # --- blackouts
    b = raw["blackouts"]
    blackouts = BlackoutConfig(
        skip_first_n_minutes_of_window=int(b["skip_first_n_minutes_of_window"]),
        skip_weekends=bool(b["skip_weekends"]),
    )

    # --- logging
    lg = raw["logging"]
    logging_ = LoggingConfig(
        heartbeat_sec=int(lg["heartbeat_sec"]),
        trade_log_path=lg["trade_log_path"],
        signal_log_path=lg["signal_log_path"],
        error_log_path=lg["error_log_path"],
        main_log_path=lg["main_log_path"],
    )

    # --- telegram
    t = raw["telegram"]
    telegram = TelegramConfig(
        enabled=bool(t["enabled"]),
        alert_on_trade=bool(t["alert_on_trade"]),
        alert_on_daily_pnl=bool(t["alert_on_daily_pnl"]),
        alert_on_error=bool(t["alert_on_error"]),
        alert_on_auto_pause=bool(t["alert_on_auto_pause"]),
        daily_summary_utc_hour=int(t["daily_summary_utc_hour"]),
    )

    # --- secrets
    secrets = Secrets(
        private_key=_require(os.getenv("PRIVATE_KEY", ""), "PRIVATE_KEY"),
        polymarket_api_key=_require(os.getenv("POLYMARKET_API_KEY", ""), "POLYMARKET_API_KEY"),
        polymarket_api_secret=_require(os.getenv("POLYMARKET_API_SECRET", ""), "POLYMARKET_API_SECRET"),
        polymarket_api_passphrase=_require(os.getenv("POLYMARKET_API_PASSPHRASE", ""), "POLYMARKET_API_PASSPHRASE"),
        rpc_url=os.getenv("RPC_URL", "https://polygon-rpc.com"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        signature_type=int(os.getenv("SIGNATURE_TYPE", "0")),
        funder_address=os.getenv("FUNDER_ADDRESS", ""),
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
        gamma_host=os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com"),
        polymarket_ws=os.getenv("POLYMARKET_WS", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
        binance_ws=os.getenv("BINANCE_WS", "wss://stream.binance.com:9443"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )

    # Sanity checks
    if strategy.min_entry_price >= strategy.max_entry_price:
        raise RuntimeError("min_entry_price must be < max_entry_price")
    # For each enabled coin, sanity-check the entry window fits in the coin's window length.
    for c in coins.values():
        if c.enabled and strategy.min_elapsed_sec + strategy.max_time_left_sec > c.window_length_sec:
            raise RuntimeError(
                f"strategy: min_elapsed_sec ({strategy.min_elapsed_sec}) + "
                f"max_time_left_sec ({strategy.max_time_left_sec}) > "
                f"window_length_sec ({c.window_length_sec}) for {c.name}. "
                f"Entry window is impossible."
            )
    if risk.min_bankroll_usd < 50:
        raise RuntimeError("min_bankroll_usd must be >= 50 (safety floor)")

    return Config(
        coins=coins,
        strategy=strategy,
        sizing=sizing,
        risk=risk,
        exit=exit_,
        blackouts=blackouts,
        logging_=logging_,
        telegram=telegram,
        secrets=secrets,
        raw=raw,
    )
