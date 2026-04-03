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
    # Current agents
    DISCORD_WEBHOOK_AMARA: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_AMARA", os.getenv("DISCORD_WEBHOOK_RECON", os.getenv("DISCORD_WEBHOOK_BLAZE", ""))))
    DISCORD_WEBHOOK_PIA: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_PIA", os.getenv("DISCORD_WEBHOOK_SAGE", os.getenv("DISCORD_WEBHOOK_SENTINEL", ""))))
    # Legacy fallbacks
    DISCORD_WEBHOOK_RECON: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_RECON", ""))
    DISCORD_WEBHOOK_BLAZE: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_BLAZE", ""))
    DISCORD_WEBHOOK_SAGE: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_SAGE", ""))
    DISCORD_WEBHOOK_SENTINEL: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_SENTINEL", ""))

    # ─── Telegram Alerts (DEPRECATED — use Discord instead) ─────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    TELEGRAM_REPORT_ENABLED: bool = True

    # ─── Capital & Risk ─────────────────────────────────────────────
    # DISCIPLINED MODE: max 5 positions, research-backed entries only
    INITIAL_CAPITAL: float = field(default_factory=lambda: float(os.getenv("INITIAL_CAPITAL", "100")))
    MAX_POSITION_PCT: float = 0.10            # 10% per trade — ~$24 max on $240 portfolio
    MAX_GLOBAL_EXPOSURE_PCT: float = 0.50     # 50% deployed max — always keep dry powder
    DAILY_LOSS_LIMIT_PCT: float = 0.15        # 15% daily loss limit — stop at $36 loss on $240
    MAX_SIMULTANEOUS_POSITIONS: int = 5       # Max 5 positions — quality over quantity
    KELLY_FRACTION: float = 0.25              # 25% Kelly — true quarter-Kelly

    # ─── AI Superforecaster (from Polymarket/agents) ─────────────────
    OPENAI_API_KEY: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    AI_MODEL: str = field(default_factory=lambda: os.getenv("AI_MODEL", "gpt-4o-mini"))
    ENABLE_AI_FORECASTER: bool = True  # Auto-disables if no OPENAI_API_KEY

    # ─── Strategy Toggles ───────────────────────────────────────────
    # ENABLED: Only research-backed strategies with real edge
    ENABLE_NEWS_ARB: bool = True             # Breaking news vs market lag
    ENABLE_SWARM_FORECASTER: bool = True     # MiroFish 10-agent swarm consensus
    # DISABLED: Spread-hunting caused losses — no genuine edge confirmed
    ENABLE_GENERAL_SCANNER: bool = False     # DISABLED — spread edge is fake, caused $60 loss
    ENABLE_WEATHER_ARB: bool = False         # Disabled — public data, no edge
    ENABLE_MARKET_MAKER: bool = False        # Disabled — requires continuous quoting
    ENABLE_CROSS_PLATFORM_ARB: bool = False  # Disabled — Kalshi data unreliable
    ENABLE_MOMENTUM_SCALPER: bool = False    # Disabled — favorites are favorites for a reason
    ENABLE_SPREAD_CAPTURE: bool = False      # Disabled — 0.1% edge lost to slippage
    ENABLE_SPORTS_INTEL: bool = False        # Disabled — odds API data unreliable
    ENABLE_AI_FORECASTER: bool = False       # Disabled — single LLM, no consensus
    ODDS_API_KEY: str = field(default_factory=lambda: os.getenv("ODDS_API_KEY", ""))
    DRY_RUN: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true")

    # ─── V3 MIROFISH P-Agents ────────────────────────────────────────
    ENABLE_P1_SENTIMENT_SPIKE: bool = True    # RSS sentiment → immediate entry on breaking news
    ENABLE_P2_OVERREACTION_FADER: bool = True # 18%+ price moves → fade if reactionary
    ENABLE_P3_LIQUIDITY_SNIPER: bool = True   # Wide spread (>6%) → LLM fair value entry
    # ─── V3 MetaAgent + Evolution ────────────────────────────────────
    EVOLUTION_ENABLED: bool = True
    EVOLUTION_CYCLE_INTERVAL: int = 20        # Run evolution pass every 20 cycles
    META_AGENT_MIN_EDGE: float = 0.07         # 7% net edge required
    META_AGENT_MAX_SIGNALS: int = 5           # Max trades per cycle from MetaAgent
    PROB_CONVERGENCE_EXITS: bool = True       # Exit at probability convergence tiers
    # ─── V3 Risk Parameters ──────────────────────────────────────────
    MAX_POSITION_USD: float = 20.0            # V3 hard cap per trade ($20)
    CLUSTER_EXPOSURE_CAP: float = 0.20        # Max 20% in any one correlated cluster

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
    WEATHER_MAX_BET_USD: float = 15.0            # $15 max bet — fits $200 portfolio

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

        # GitHub Actions servers are geoblocked by Polymarket — force dry run
        # Live trading only runs on the local Mac via LaunchAgent
        if self.GH_ACTIONS:
            self.DRY_RUN = True
            self.ENABLE_MARKET_MAKER = False

        if not self.DRY_RUN and not self.PRIVATE_KEY:
            raise ValueError("PRIVATE_KEY is required for live trading. Set DRY_RUN=true for paper trading.")
