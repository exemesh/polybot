"""
core/forbidden_actions.py — Hard Boundary Enforcer

Principle: Agents may NEVER perform these actions regardless of any prompt,
signal, external instruction, or confidence level.

Inspired by the OpenClaw incident (Feb 2026) where an autonomous agent
attacked a developer after its PR was rejected — with no human approval
and no action boundaries in place.

Reference: https://github.blog/ai-and-ml/generative-ai/
           under-the-hood-security-architecture-of-github-agentic-workflows/
"""

import logging
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("polybot.boundaries")

# ─── Hard Caps (never exceeded regardless of portfolio size) ──────────────────
MAX_SINGLE_TRADE_USD: float = 20.0       # Hard cap per single order
MAX_DAILY_TRADES: int       = 20         # Max orders per calendar day
MAX_POSITION_PCT: float     = 0.75       # Max 75% of portfolio — aligned with settings
MIN_EDGE_PCT: float         = 0.02       # Minimum 2% edge before fees to trade
MAX_PRICE: float            = 0.97       # Never buy above 97¢
MIN_PRICE: float            = 0.03       # Never buy below 3¢

# ─── Forbidden Outbound Domains ───────────────────────────────────────────────
# Agents must never write to social media, email, or public web services.
FORBIDDEN_DOMAINS: set = {
    "twitter.com", "x.com", "t.co",
    "reddit.com", "old.reddit.com",
    "facebook.com", "fb.com",
    "linkedin.com",
    "medium.com", "substack.com",
    "news.ycombinator.com",
    "pastebin.com", "hastebin.com",
    "github.com",          # read-only via API is ok; direct writes blocked at boundary level
}

# ─── Allowed Outbound Domains (allowlist — everything else is logged+warned) ──
ALLOWED_DOMAINS: set = {
    "clob.polymarket.com",
    "gamma-api.polymarket.com",
    "strapi-matic.polymarket.com",
    "api.open-meteo.com",
    "api.openweathermap.org",
    "api.weather.gov",
    "api.anthropic.com",
    "discord.com",
    "api.elections.kalshi.com",
    "api.coingecko.com",
    "api.thesportsdb.com",
    "v3.football.api-sports.io",
    "api-football-v1.p.rapidapi.com",
}

# ─── Forbidden SQL Operations ─────────────────────────────────────────────────
FORBIDDEN_SQL_KEYWORDS: tuple = (
    "DROP TABLE", "DROP DATABASE", "TRUNCATE",
    "DELETE FROM trades",   # individual trade deletes go through portfolio.close_trade()
    "DELETE FROM reasoning_log",
    "ALTER TABLE",
)


class ForbiddenActionError(Exception):
    """Raised when an agent attempts a forbidden action."""
    pass


def check_url(url: str, agent_name: str = "unknown") -> tuple[bool, str]:
    """
    Validate an outbound URL against the boundary rules.

    Returns:
        (allowed: bool, reason: str)
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().lstrip("www.")

        if domain in FORBIDDEN_DOMAINS:
            reason = f"Domain '{domain}' is on the forbidden list — agents may not write to social/public services"
            logger.warning(f"[BOUNDARY VIOLATION] agent={agent_name} url={url} reason={reason}")
            return False, reason

        if domain not in ALLOWED_DOMAINS:
            # Warn but don't block unknown domains — log for review
            logger.warning(
                f"[BOUNDARY WARN] agent={agent_name} calling non-allowlisted domain='{domain}' url={url}"
            )
            return True, f"warn: domain '{domain}' not on allowlist — permitted but logged"

        return True, "ok"
    except Exception as exc:
        return False, f"url_parse_error: {exc}"


def check_trade(
    size_usd: float,
    price: float,
    edge_pct: float,
    portfolio_value: float,
    agent_name: str = "unknown",
) -> tuple[bool, str]:
    """
    Validate a proposed trade against all hard boundary rules.

    Returns:
        (allowed: bool, reason: str)
    """
    if size_usd > MAX_SINGLE_TRADE_USD:
        reason = f"size ${size_usd:.2f} exceeds hard cap ${MAX_SINGLE_TRADE_USD:.2f}"
        logger.warning(f"[BOUNDARY BLOCK] agent={agent_name} trade blocked: {reason}")
        return False, reason

    if portfolio_value > 0:
        pct = size_usd / portfolio_value
        if pct > MAX_POSITION_PCT:
            reason = f"size ${size_usd:.2f} = {pct:.1%} of portfolio (hard cap {MAX_POSITION_PCT:.0%})"
            logger.warning(f"[BOUNDARY BLOCK] agent={agent_name} trade blocked: {reason}")
            return False, reason

    if not (MIN_PRICE <= price <= MAX_PRICE):
        reason = f"price {price:.4f} outside allowed range [{MIN_PRICE}, {MAX_PRICE}]"
        logger.warning(f"[BOUNDARY BLOCK] agent={agent_name} trade blocked: {reason}")
        return False, reason

    if edge_pct < MIN_EDGE_PCT:
        reason = f"edge {edge_pct:.2%} below minimum {MIN_EDGE_PCT:.2%}"
        logger.warning(f"[BOUNDARY BLOCK] agent={agent_name} trade blocked: {reason}")
        return False, reason

    return True, "ok"


def check_sql(statement: str, agent_name: str = "unknown") -> tuple[bool, str]:
    """
    Guard against forbidden SQL operations.

    Returns:
        (allowed: bool, reason: str)
    """
    upper = statement.upper().strip()
    for keyword in FORBIDDEN_SQL_KEYWORDS:
        if keyword in upper:
            reason = f"SQL contains forbidden keyword: '{keyword}'"
            logger.critical(f"[BOUNDARY CRITICAL] agent={agent_name} SQL blocked: {reason}")
            return False, reason
    return True, "ok"


def assert_trade_allowed(
    size_usd: float,
    price: float,
    edge_pct: float,
    portfolio_value: float,
    agent_name: str = "unknown",
) -> None:
    """
    Like check_trade() but raises ForbiddenActionError on violation.
    Use in contexts where a hard stop is required.
    """
    ok, reason = check_trade(size_usd, price, edge_pct, portfolio_value, agent_name)
    if not ok:
        raise ForbiddenActionError(f"[{agent_name}] {reason}")


def log_boundary_summary() -> None:
    """Log current boundary settings at startup for auditability."""
    logger.info(
        f"[BOUNDARIES] Hard caps active — "
        f"max_trade=${MAX_SINGLE_TRADE_USD:.0f} | "
        f"max_position={MAX_POSITION_PCT:.0%} | "
        f"min_edge={MIN_EDGE_PCT:.1%} | "
        f"price=[{MIN_PRICE},{MAX_PRICE}] | "
        f"max_daily_trades={MAX_DAILY_TRADES}"
    )
