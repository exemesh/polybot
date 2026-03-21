"""
AutoResearch Strategy Parameters
==================================
Modified by the AutoResearch engine (core/auto_research.py).
Do NOT edit manually — changes may be overwritten.
"""

# Minimum edge required to enter an arbitrage trade
MIN_EDGE_PCT = 0.02

# Minimum liquidity (USDC) in market orderbook
MIN_LIQUIDITY = 100.0

# Days before forcing exit on stale position
STALE_DAYS = 7

# Skip markets where price is near extremes (avoids illiquid ends)
PRICE_EXTREMES_CUTOFF = 0.05

# Maximum position size as fraction of portfolio
MAX_POSITION_FRACTION = 0.10

# Minimum confidence for value trade (non-arb) entries
MIN_VALUE_CONFIDENCE = 0.60
