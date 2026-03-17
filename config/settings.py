"""
PolyBot Configuration
Reads from environment variables (GitHub Actions secrets) or .env file.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not required when env vars are set directly (GH Actions)


@dataclass
class Settings:
    # ─── Environment Detection ─────────────────────────────────────
    GH_ACTIONS: bool = field(default_factory=lambda: os.getenv("GITHUB_ACTIONS", "").lower() == "true")

    # ─── Wallet / Auth ─────────────────────────────────────────────
    PRIVATE_KEY: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    FUNDER_ADDRESS: str = field(default_factory=lambda: os.getenv("FUNDER_ADDRESS", ""))
    CHAIN_ID: int = 137  # Polygon mainnet

    # ─── Polymarket API ─────────────────────────────────────────────
    CLOB_HOST: str = "https://clob.polymarket.com"
    GAMMA_HOST: str = "https://gamma-api.polymarket.com"

    # ─── Kalshi API (cross-platform arb) ────────────────────────────
    KALSHI_API_KEY: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY", ""))
    KALSHI_API_SECRET: str = field(default_factory=lambda: os.getenv("KALSHI_API_SECRET", ""))
    KALSHI_BASE_URL: str = "https://api.elections.kalshi.com/trade-api/v2"

    # ─── Weather Data ───────────────────────────────────────────────
    NOAA_API_TOKEN: str = field(default_factory=lambda: os.getenv("NOAA_API_TOKEN", ""))
    OPENMETEO_BASE_URL: str = "https://api.open-meteo.com/v1/forecast"

    # ─── Discord Alerts ─────────────────────────────────────────────
    DISCORD_WEBHOOK_URL: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", ""))
    DISCORD_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", ""))
    DISCORD_TRADE_CHANNEL_ID: str = field(default_factory=lambda: os.getenv("DISCORD_TRADE_CHANNEL_ID", ""))
    DISCORD_ALERT_CHANNEL_ID: str = field(default_factory=lambda: os.getenv("DISCORD_ALERT_CHANNEL_ID", ""))
    DISCORD_ANALYST_CHANNEL_ID: str = field(default_factory=lambda: os.getenv("DISCORD_ANALYST_CHANNEL_ID", ""))

    # ─── Discord Agent Webhooks (per-agent identity) ─────────────────
    DISCORD_WEBHOOK_RECON: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_RECON", ""))
    DISCORD_WEBHOOK_BLAZE: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_BLAZE", ""))
    DISCORD_WEBHOOK_SAGE: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_SAGE", ""))
    DISCORD_WEBHOOK_SENTINEL: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_SENTINEL", ""))

    # ─── Telegram Alerts (DEPRECATED — use Discord instead) ─────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    TELEGRAM_REPORT_ENABLED: bool = True

    # ─── Capital & Risk ─────────────────────────────────────────────
    # AGGRESSIVE MODE: $100 paper trading, push to limits
    INITIAL_CAPITAL: float = field(default_factory=lambda: float(os.getenv("INITIAL_CAPITAL", "100")))
    MAX_POSITION_PCT: float = 0.20            # 20% per trade — room for $10 longshots
    MAX_GLOBAL_EXPOSURE_PCT: float = 0.50     # 50% deployed — prudent risk management
    DAILY_LOSS_LIMIT_PCT: float = 0.10        # 10% daily loss limit — protect capital
    MAX_SIMULTANEOUS_POSITIONS: int = 15      # Up to 15 positions — focused portfolio
    KELLY_FRACTION: float = 0.25              # 25% Kelly — true quarter-Kelly

    # ─── AI Superforecaster (from Polymarket/agents) ─────────────────
    OPENAI_API_KEY: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    AI_MODEL: str = field(default_factory=lambda: os.getenv("AI_MODEL", "gpt-4o-mini"))
    ENABLE_AI_FORECASTER: bool = True  # Auto-disables if no OPENAI_API_KEY

    # ─── Strategy Toggles ───────────────────────────────────────────
    ENABLE_WEATHER_ARB: bool = True
    ENABLE_MARKET_MAKER: bool = False  # Disabled - requires continuous quoting
    ENABLE_CROSS_PLATFORM_ARB: bool = True
    ENABLE_GENERAL_SCANNER: bool = True
    ENABLE_MOMENTUM_SCALPER: bool = True
    ENABLE_SPREAD_CAPTURE: bool = True
    ENABLE_SPORTS_INTEL: bool = True
    ODDS_API_KEY: str = field(default_factory=lambda: os.getenv("ODDS_API_KEY", ""))
    DRY_RUN: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true")

    # ─── Weather Strategy Config ─────────────────────────────────────
    WEATHER_CITIES: list = field(default_factory=lambda: [
        {"name": "New York", "lat": 40.7128, "lon": -74.0060, "station": "KNYC"},
        {"name": "London", "lat": 51.5074, "lon": -0.1278, "station": "EGLC"},
        {"name": "Chicago", "lat": 41.8781, "lon": -87.6298, "station": "KORD"},
        {"name": "Seoul", "lat": 37.5665, "lon": 126.9780, "station": "RKSS"},
        {"name": "Sydney", "lat": -33.8688, "lon": 151.2093, "station": "YSSY"},
        {"name": "Dallas", "lat": 32.7767, "lon": -96.7970, "station": "KDFW"},
    ])
    WEATHER_MIN_EDGE: float = 0.08              # 8% edge min — AGGRESSIVE (was 15%)
    WEATHER_MIN_LIQUIDITY: float = 100.0         # Lower liq req (was 500)
    WEATHER_MAX_HOURS_OUT: int = 504             # 21 days max (was 14)
    WEATHER_MIN_HOURS_OUT: int = 4               # 4 hours minimum (was 12)
    WEATHER_SCAN_INTERVAL: int = 120             # Faster scanning (was 300)
    WEATHER_MAX_BET_USD: float = 10.0            # $10 max bet (was $5)

    # ─── Market Maker Config ─────────────────────────────────────────
    MM_MIN_SPREAD: float = 0.03
    MM_MAX_SPREAD: float = 0.10
    MM_ORDER_SIZE_USD: float = 10.0
    MM_UPDATE_INTERVAL: int = 30
    MM_MIN_VOLUME_24H: float = 1000.0
    MM_MAX_POSITION_SIZE: float = 50.0
    MM_INVENTORY_SKEW: float = 0.3

    # ─── Cross-Platform Arb Config ───────────────────────────────────
    ARB_MIN_EDGE_PCT: float = 0.02            # 2% edge min — ensures real fees and slippage are absorbed (was 0.5%)
    ARB_SCAN_INTERVAL: int = 5                # Faster scanning
    ARB_MAX_POSITION_USD: float = 50.0        # Up to $50 per arb (was $20)
    ARB_POLY_FEE: float = 0.001
    ARB_KALSHI_FEE: float = 0.007
    ARB_MIN_HOURS_TO_RESOLUTION: int = 2      # 2 hours minimum (was 24)

    # ─── Portfolio / Reporting ──────────────────────────────────────
    REPORT_INTERVAL_SECONDS: int = 3600
    DATA_DIR: Path = field(default_factory=lambda: Path("data"))
    LOG_DIR: Path = field(default_factory=lambda: Path("logs"))
    DB_PATH: str = "data/polybot.db"

    def __post_init__(self):
        self.DATA_DIR.mkdir(exist_ok=True)
        self.LOG_DIR.mkdir(exist_ok=True)

        # Auto-disable market maker in GH Actions (needs continuous quoting)
        if self.GH_ACTIONS:
            self.ENABLE_MARKET_MAKER = False

        if not self.DRY_RUN and not self.PRIVATE_KEY:
            raise ValueError("PRIVATE_KEY is required for live trading. Set DRY_RUN=true for paper trading.")
