#!/usr/bin/env python3
"""
One-shot script: approve USDC allowance for the Polymarket CLOB exchange contract.
Run this once whenever CLOB shows balance: 0 despite having USDC in wallet.

Usage: python3.11 ~/polybot/scripts/set_clob_allowance.py
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env from polybot directory
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
if not PRIVATE_KEY:
    print("ERROR: PRIVATE_KEY not found in .env")
    sys.exit(1)

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import AssetType
    from py_clob_client.constants import POLYGON
except ImportError:
    print("ERROR: py-clob-client not installed. Run: pip3.11 install py-clob-client")
    sys.exit(1)

HOST = "https://clob.polymarket.com"
CHAIN_ID = POLYGON

print("Connecting to Polymarket CLOB...")
client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)

print("Checking current USDC balance/allowance...")
try:
    bal = client.get_balance_allowance(asset_type=AssetType.USDC)
    print(f"USDC state: {bal}")
except Exception as e:
    print(f"Balance check error: {e}")

print("\nApproving USDC allowance for CLOB exchange contract...")
try:
    result = client.update_balance_allowance(asset_type=AssetType.USDC)
    print(f"Result: {result}")
    print("\n✅ Done — CLOB should now see your USDC balance.")
except Exception as e:
    print(f"update_balance_allowance failed: {e}")
    # Try the conditional token approval too
    try:
        result2 = client.update_balance_allowance(asset_type=AssetType.CONDITIONAL)
        print(f"Conditional result: {result2}")
        print("\n✅ Done.")
    except Exception as e2:
        print(f"All approval methods failed: {e2}")
        print("\nManual fix: go to polymarket.com → click your balance → Deposit → deposit $1")
        sys.exit(1)
