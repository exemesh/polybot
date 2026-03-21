"""
Polymarket Fee Guard
====================
Implements the exact Polymarket taker fee formula from:
https://docs.polymarket.com/trading/fees

Key facts:
- MOST markets are fee-FREE (political, general events)
- Only crypto, NCAAB, and Serie A markets charge taker fees
- Fee formula: fee = C x p x feeRate x (p x (1-p))^exponent
  where C = shares traded, p = price of shares
- Crypto: feeRate=0.25, exponent=2, max ~1.56% at p=0.50
- Sports (NCAAB/Serie A): feeRate=0.0175, exponent=1
- Fees collected in shares on BUY, USDC on SELL
- Check market.feesEnabled before applying any fee
"""

import math
from typing import Optional

# Fee parameters by market type (from Polymarket docs)
FEE_PARAMS = {
    "crypto":  {"fee_rate": 0.25,   "exponent": 2},
    "ncaab":   {"fee_rate": 0.0175, "exponent": 1},
    "serie_a": {"fee_rate": 0.0175, "exponent": 1},
    "sports":  {"fee_rate": 0.0175, "exponent": 1},
    "free":    {"fee_rate": 0.0,    "exponent": 1},
}


def calculate_taker_fee(shares: float, price: float, market_type: str = "free") -> float:
    """
    Calculate Polymarket taker fee using official formula.
    Formula: fee = C x p x feeRate x (p x (1-p))^exponent
    Returns fee in USDC, rounded to 4 decimal places.
    """
    params = FEE_PARAMS.get(market_type, FEE_PARAMS["free"])
    fee_rate = params["fee_rate"]
    exponent = params["exponent"]

    if fee_rate == 0.0 or price <= 0.0 or price >= 1.0:
        return 0.0

    fee = shares * price * fee_rate * (price * (1.0 - price)) ** exponent
    return round(fee, 4)


def get_market_type(market: Optional[dict] = None, market_question: str = "") -> str:
    """
    Determine market fee type from market object or question string.
    Returns 'crypto', 'sports', 'ncaab', 'serie_a', or 'free'.
    """
    if market is not None:
        if not market.get("feesEnabled", False):
            return "free"
        tags = [t.get("label", "").lower() for t in market.get("tags", [])]
        category = market.get("category", "").lower()
        combined = " ".join(tags + [category, market.get("question", "").lower()])
        if any(k in combined for k in ["serie a", "serie-a", "calcio"]):
            return "serie_a"
        if any(k in combined for k in ["ncaab", "basketball", "ncaa", "college basketball"]):
            return "ncaab"
        if any(k in combined for k in ["crypto", "bitcoin", "ethereum", "btc", "eth", "sol", "price of"]):
            return "crypto"
        return "crypto"  # feesEnabled but unknown type → conservative

    q = market_question.lower()
    if any(k in q for k in ["btc", "bitcoin", "ethereum", "eth ", "crypto", "sol ", "price of"]):
        return "crypto"
    if any(k in q for k in ["ncaab", "college basketball", "march madness"]):
        return "ncaab"
    if "serie a" in q:
        return "serie_a"
    return "free"


def calculate_arb_fees(size_per_side_usd: float, yes_price: float, no_price: float,
                       market_type: str = "free") -> float:
    """Calculate total fees for a YES+NO arb trade (both sides combined)."""
    if market_type == "free":
        return 0.0
    yes_shares = size_per_side_usd / yes_price if yes_price > 0 else 0
    no_shares = size_per_side_usd / no_price if no_price > 0 else 0
    yes_fee = calculate_taker_fee(yes_shares, yes_price, market_type)
    no_fee = calculate_taker_fee(no_shares, no_price, market_type)
    return yes_fee + no_fee


def calculate_net_pnl(gross_pnl: float, shares: float, entry_price: float,
                      market_type: str = "free", sides: int = 1) -> float:
    """Calculate fee-adjusted net P&L for a resolved trade."""
    fee = calculate_taker_fee(shares, entry_price, market_type) * sides
    return gross_pnl - fee


def validate_edge_after_fees(gross_edge_pct: float, trade_size_usd: float,
                             avg_price: float, market_type: str = "free") -> tuple:
    """Returns (is_profitable: bool, net_edge_pct: float) after fee deduction."""
    if market_type == "free":
        return True, gross_edge_pct
    shares = trade_size_usd / avg_price if avg_price > 0 else 0
    fee = calculate_taker_fee(shares, avg_price, market_type)
    fee_as_pct = fee / trade_size_usd if trade_size_usd > 0 else 0
    net_edge_pct = gross_edge_pct - fee_as_pct
    return net_edge_pct > 0, net_edge_pct


def effective_fee_rate(price: float, market_type: str = "free") -> float:
    """Get effective fee rate (as decimal) for a given price and market type."""
    if market_type == "free":
        return 0.0
    shares = 1.0 / price if price > 0 else 0
    return calculate_taker_fee(shares, price, market_type)
