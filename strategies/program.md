# AutoResearch: Polymarket Trading Strategy Optimisation

## Objective
Improve the Sharpe ratio of polybot's arbitrage and prediction strategies by testing parameter variations against historical Polymarket data.

## Current Strategy Overview
Polybot uses arbitrage: buys YES+NO when total price < $1. Profit = $1 - total_cost.

## Editable Parameters
Only modify `strategies/auto_strategy.py`. Do NOT touch any other file.

## Metric
**Sharpe Ratio** (higher is better). Minimum improvement threshold: 0.05 to adopt a change.

## Constraints (DO NOT VIOLATE)
- Do NOT use future market resolution data (lookahead bias)
- Do NOT modify `core/fee_guard.py`, `core/backtest_engine.py`, or `core/risk_manager.py`
- Do NOT change position sizing beyond range (MIN: 5%, MAX: 20%)
- Do NOT remove stop-loss or daily loss limit checks
- ONE parameter change per iteration only
- Do NOT modify this file

## Parameter Ranges
| Parameter | Current | Min | Max | Notes |
|-----------|---------|-----|-----|-------|
| MIN_EDGE_PCT | 0.02 | 0.01 | 0.08 | Minimum arb edge to trade |
| MIN_LIQUIDITY | 100.0 | 50 | 500 | Min USDC liquidity in market |
| STALE_DAYS | 7 | 3 | 14 | Days before forcing position exit |
| PRICE_EXTREMES_CUTOFF | 0.05 | 0.02 | 0.15 | Skip markets near 0/1 extremes |
