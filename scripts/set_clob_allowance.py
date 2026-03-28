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
    from py_clob_client.constants import POLYGON
except ImportError:
    print("ERROR: py-clob-client not installed. Run: pip3.11 install py-clob-client")
    sys.exit(1)

HOST = "https://clob.polymarket.com"
CHAIN_ID = POLYGON

print("Connecting to Polymarket CLOB...")
client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)

print("Checking current balance/allowance...")
try:
    bal = client.get_balance_allowance(asset_type=None)
    print(f"Current state: {bal}")
except Exception as e:
    print(f"Balance check: {e}")

print("\nSetting USDC allowance for CLOB exchange contract...")
try:
    result = client.set_allowance(asset_type=None)
    print(f"Allowance set: {result}")
    print("\n✅ Done — CLOB should now see your USDC balance.")
    print("Run the bot and it will start trading on the next cycle.")
except Exception as e:
    print(f"ERROR setting allowance: {e}")
    print("\nAlternative: go to polymarket.com → Profile → Deposit → deposit any amount via the UI.")
    sys.exit(1)
