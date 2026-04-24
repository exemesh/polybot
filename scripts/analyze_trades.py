#!/usr/bin/env python3
"""Post-hoc trade analysis.

Reads logs/trades.jsonl and computes:
  - Overall win rate
  - Win rate per (price bin, time bin)
  - Per-coin stats
  - Break-even check (win rate >= avg entry price?)

Run:
  ./scripts/analyze_trades.py [--min-trades 10]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List


def bucket_price(p: float) -> str:
    for lo, hi in ((0.50, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 1.0)):
        if lo <= p < hi:
            return f"[{lo:.2f},{hi:.2f})"
    return "other"


def bucket_time(sec_left: float) -> str:
    for lo, hi in ((0, 60), (60, 120), (120, 180), (180, 240), (240, 335)):
        if lo <= sec_left < hi:
            return f"[{lo},{hi})s"
    return "other"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="logs/trades.jsonl")
    parser.add_argument("--min-trades", type=int, default=10)
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"No trade log at {path}")
        return 1

    entries: Dict[str, dict] = {}
    exits: Dict[str, dict] = {}
    with path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            oid = rec.get("order_id")
            if rec.get("kind") == "entry" and oid:
                entries[oid] = rec
            elif rec.get("kind") == "exit" and oid:
                exits[oid] = rec

    resolved = [(entries[oid], exits[oid]) for oid in entries if oid in exits]
    if not resolved:
        print("No resolved trades yet.")
        return 0

    print(f"Resolved trades: {len(resolved)}\n")

    # Overall
    pnls = [x[1]["realized_usd"] for x in resolved]
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls)
    avg_entry = statistics.fmean(x[0]["entry_price"] for x in resolved)
    total_pnl = sum(pnls)
    print("=" * 60)
    print("OVERALL")
    print("=" * 60)
    print(f"Trades              : {len(resolved)}")
    print(f"Win rate            : {win_rate:.2%}")
    print(f"Avg entry price     : {avg_entry:.3f}")
    print(f"Break-even required : {avg_entry:.2%}")
    print(f"Total P&L           : ${total_pnl:+.2f}")
    print(f"Avg P&L / trade     : ${total_pnl/len(pnls):+.3f}")
    passed = "PASS" if win_rate >= avg_entry else "FAIL"
    print(f"Break-even gate     : {passed}")

    # By coin
    print("\n" + "=" * 60)
    print("BY COIN")
    print("=" * 60)
    by_coin: Dict[str, List] = defaultdict(list)
    for e, x in resolved:
        by_coin[e["coin"]].append((e, x))
    for coin, items in by_coin.items():
        pnls = [i[1]["realized_usd"] for i in items]
        wins = sum(1 for p in pnls if p > 0)
        if len(pnls) < args.min_trades:
            continue
        print(f"{coin:<5} n={len(pnls):<4} wr={wins/len(pnls):.2%}  pnl=${sum(pnls):+.2f}")

    # By (price, time) bucket
    print("\n" + "=" * 60)
    print("BY (PRICE BIN, TIME-LEFT BIN)")
    print("=" * 60)
    buckets: Dict[str, List] = defaultdict(list)
    for e, x in resolved:
        pb = bucket_price(e["entry_price"])
        tb = bucket_time(900 - x["seconds_held"])  # approx seconds-left-at-entry
        buckets[f"{pb} / {tb}"].append(x["realized_usd"])
    for k, pnls in sorted(buckets.items()):
        if len(pnls) < args.min_trades:
            continue
        wins = sum(1 for p in pnls if p > 0)
        print(f"{k:<30} n={len(pnls):<4} wr={wins/len(pnls):.2%}  pnl=${sum(pnls):+.2f}")

    # Exit-reason breakdown
    print("\n" + "=" * 60)
    print("BY EXIT REASON")
    print("=" * 60)
    by_reason: Dict[str, List] = defaultdict(list)
    for _, x in resolved:
        by_reason[x["reason"]].append(x["realized_usd"])
    for r, pnls in by_reason.items():
        wins = sum(1 for p in pnls if p > 0)
        print(f"{r:<20} n={len(pnls):<4} wr={wins/len(pnls):.2%}  pnl=${sum(pnls):+.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
