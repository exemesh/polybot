#!/usr/bin/env python3
"""
One-shot script: approve USDC + conditional token allowances for Polymarket CLOB.
Run this once whenever CLOB shows balance: 0 despite having USDC in wallet,
or when SELL orders fail with 'not enough balance / allowance'.

Usage: python3.11 ~/polybot/scripts/set_clob_allowance.py
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
if not PRIVATE_KEY:
    print("ERROR: PRIVATE_KEY not found in .env")
    sys.exit(1)

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    from py_clob_client.constants import POLYGON
except ImportError:
    print("ERROR: py-clob-client not installed. Run: pip3.11 install py-clob-client")
    sys.exit(1)

HOST = "https://clob.polymarket.com"
CHAIN_ID = POLYGON

print("Connecting to Polymarket CLOB...")
kwargs = dict(signature_type=1)  # Magic Link / Polymarket-issued key
if FUNDER_ADDRESS:
    kwargs["funder"] = FUNDER_ADDRESS
    print(f"  funder: {FUNDER_ADDRESS}")
client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, **kwargs)

creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

print("\nChecking current balances...")
for asset in [AssetType.COLLATERAL, AssetType.CONDITIONAL]:
    try:
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=asset))
        print(f"  {asset}: {bal}")
    except Exception as e:
        print(f"  {asset} check error: {e}")

print("\nSyncing USDC (COLLATERAL) allowance...")
try:
    r = client.update_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"  COLLATERAL sync: {r}")
except Exception as e:
    print(f"  COLLATERAL sync failed: {e}")

print("\nSyncing outcome token (CONDITIONAL) allowances for open positions...")
try:
    import sqlite3
    db_path = Path(__file__).parent.parent / "data" / "polybot.db"
    conn = sqlite3.connect(str(db_path))
    tokens = conn.execute(
        "SELECT DISTINCT token_id FROM trades WHERE status='open' AND dry_run=0 AND token_id IS NOT NULL"
    ).fetchall()
    conn.close()
    if tokens:
        for (tid,) in tokens:
            try:
                r = client.update_balance_allowance(
                    params=BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
                )
                print(f"  {tid[:16]}... sync: {r or 'OK'}")
            except Exception as e:
                print(f"  {tid[:16]}... failed: {e}")
    else:
        print("  No open live positions found in DB")
except Exception as e:
    print(f"  CONDITIONAL sync failed: {e}")

print("\nRe-checking balances after sync...")
for asset in [AssetType.COLLATERAL, AssetType.CONDITIONAL]:
    try:
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=asset))
        print(f"  {asset}: {bal}")
    except Exception as e:
        print(f"  {asset}: {e}")

print("\n✅ Done. If CLOB still shows $0, go to polymarket.com → click balance → Deposit.")
