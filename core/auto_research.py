"""
AutoResearch Engine
===================
Karpathy-style autonomous strategy optimisation.
Generates hypotheses, backtests them, keeps improvements, reverts failures.

Run: python core/auto_research.py [iterations]
Schedule: Weekly via launchd (NOT every trading cycle)
"""
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRAM_MD = os.path.join(BASE_DIR, "strategies", "program.md")
AUTO_STRATEGY = os.path.join(BASE_DIR, "strategies", "auto_strategy.py")
AUTO_STRATEGY_BACKUP = os.path.join(BASE_DIR, "strategies", "auto_strategy.py.bak")
DB_PATH = os.path.join(BASE_DIR, "data", "polybot.db")
LOG_FILE = os.path.join(BASE_DIR, "data", "auto_research_log.json")

DEFAULT_ITERATIONS = 5
MIN_SHARPE_IMPROVEMENT = 0.05


def _init_db_table():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS auto_research_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL, hypothesis TEXT,
        sharpe_before REAL, sharpe_after REAL, improvement REAL,
        kept INTEGER DEFAULT 0, reason TEXT, iteration INTEGER
    )""")
    conn.commit()
    conn.close()


def _run_baseline_backtest() -> float:
    try:
        from core.backtest_engine import BacktestEngine, MarketSnapshot
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT market_id, price, size_usd, timestamp, pnl, status
            FROM trades WHERE status IN ('won', 'lost', 'resolved')
            ORDER BY timestamp ASC LIMIT 100
        """)
        trades = cursor.fetchall()
        conn.close()
        if len(trades) < 10:
            return 0.0
        markets = [{
            "market_id": t[0] or f"m{i}",
            "yes_price": float(t[1]) if t[1] else 0.5,
            "no_price": 1.0 - (float(t[1]) if t[1] else 0.5),
            "question": "", "resolved": t[4] is not None and t[4] > 0 if t[5] == "won" else (False if t[5] == "lost" else None),
            "market_type": "free"
        } for i, t in enumerate(trades)]
        engine = BacktestEngine(initial_capital=1000.0)
        result = engine.run_arb_backtest(markets)
        return result.sharpe_ratio
    except Exception as e:
        print(f"Backtest error: {e}")
        return 0.0


def _generate_hypothesis(program_md: str, current_code: str, iteration: int) -> str:
    try:
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": f"""Optimise a Polymarket trading bot. Suggest ONE parameter change.

## Objective
{program_md}

## Current parameters (auto_strategy.py)
{current_code}

## Rules
- Format exactly: "Change PARAM_NAME from X to Y: brief reason"
- ONE change only, based on trading logic
- Iteration {iteration}/{DEFAULT_ITERATIONS} — vary suggestions
- Do NOT touch risk limits or fee params

Reply with ONLY the hypothesis line."""}]
        )
        return response.content[0].text.strip()
    except Exception:
        fallbacks = [
            "Change MIN_EDGE_PCT from 0.02 to 0.03: Higher edge filter reduces marginal entries",
            "Change MIN_LIQUIDITY from 100.0 to 150.0: More liquid markets have lower price impact",
            "Change STALE_DAYS from 7 to 5: Faster exit reduces capital lockup in stale positions",
            "Change PRICE_EXTREMES_CUTOFF from 0.05 to 0.08: Skip more extreme prices for better fills",
            "Change MAX_POSITION_FRACTION from 0.10 to 0.08: Slightly smaller positions reduce concentration",
        ]
        return fallbacks[iteration % len(fallbacks)]


def _apply_hypothesis(hypothesis: str, current_code: str) -> Optional[str]:
    match = re.search(r'Change\s+(\w+)\s+from\s+([0-9.]+)\s+to\s+([0-9.]+)', hypothesis, re.IGNORECASE)
    if match:
        param, old_val, new_val = match.group(1), match.group(2), match.group(3)
        new_code = re.sub(rf'({param}\s*=\s*){re.escape(old_val)}', rf'\g<1>{new_val}', current_code)
        return new_code if new_code != current_code else None
    match = re.search(r'Set\s+(\w+)\s+to\s+([0-9.]+)', hypothesis, re.IGNORECASE)
    if match:
        param, new_val = match.group(1), match.group(2)
        new_code = re.sub(rf'({param}\s*=\s*)[0-9.]+', rf'\g<1>{new_val}', current_code)
        return new_code if new_code != current_code else None
    return None


def _log_result(iteration, hypothesis, sharpe_before, sharpe_after, kept, reason):
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "iteration": iteration, "hypothesis": hypothesis,
        "sharpe_before": sharpe_before, "sharpe_after": sharpe_after,
        "improvement": sharpe_after - sharpe_before, "kept": kept, "reason": reason
    }
    logs = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                logs = json.load(f)
        except Exception:
            pass
    logs.append(record)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "w") as f:
        json.dump(logs[-100:], f, indent=2)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""INSERT INTO auto_research_log
            (timestamp, hypothesis, sharpe_before, sharpe_after, improvement, kept, reason, iteration)
            VALUES (?,?,?,?,?,?,?,?)""",
            (record["timestamp"], hypothesis, sharpe_before, sharpe_after,
             sharpe_after - sharpe_before, int(kept), reason, iteration))
        conn.commit()
        conn.close()
    except Exception:
        pass


def run(iterations: int = DEFAULT_ITERATIONS, webhook_url: Optional[str] = None) -> dict:
    print(f"\n AutoResearch starting — {iterations} iterations")
    _init_db_table()
    program_md = open(PROGRAM_MD).read() if os.path.exists(PROGRAM_MD) else ""
    if not program_md:
        return {"error": "strategies/program.md not found"}

    summary = {"iterations_run": 0, "improvements_found": 0, "kept": 0, "discarded": 0, "results": []}
    baseline_sharpe = _run_baseline_backtest()
    current_sharpe = baseline_sharpe
    print(f"   Baseline Sharpe: {baseline_sharpe:.3f}")

    for i in range(iterations):
        print(f"\n   Iteration {i+1}/{iterations}")
        current_code = open(AUTO_STRATEGY).read() if os.path.exists(AUTO_STRATEGY) else ""
        hypothesis = _generate_hypothesis(program_md, current_code, i)
        print(f"   Hypothesis: {hypothesis}")
        new_code = _apply_hypothesis(hypothesis, current_code)
        if not new_code:
            reason = "Could not parse hypothesis into code change"
            _log_result(i+1, hypothesis, current_sharpe, current_sharpe, False, reason)
            summary["discarded"] += 1
            summary["results"].append({"iteration": i+1, "hypothesis": hypothesis, "kept": False, "reason": reason})
            continue
        if os.path.exists(AUTO_STRATEGY):
            shutil.copy2(AUTO_STRATEGY, AUTO_STRATEGY_BACKUP)
        with open(AUTO_STRATEGY, "w") as f:
            f.write(new_code)
        new_sharpe = _run_baseline_backtest()
        improvement = new_sharpe - current_sharpe
        print(f"   Sharpe: {current_sharpe:.3f} -> {new_sharpe:.3f} (D{improvement:+.3f})")
        if improvement >= MIN_SHARPE_IMPROVEMENT:
            kept, reason = True, f"Sharpe improved by {improvement:.3f}"
            current_sharpe = new_sharpe
            summary["kept"] += 1
            summary["improvements_found"] += 1
            print(f"   KEPT")
        else:
            kept, reason = False, f"No meaningful improvement (D{improvement:+.3f})"
            if os.path.exists(AUTO_STRATEGY_BACKUP):
                shutil.copy2(AUTO_STRATEGY_BACKUP, AUTO_STRATEGY)
            summary["discarded"] += 1
            print(f"   REVERTED")
        before = current_sharpe - improvement if kept else current_sharpe
        _log_result(i+1, hypothesis, before, new_sharpe if kept else current_sharpe, kept, reason)
        summary["results"].append({"iteration": i+1, "hypothesis": hypothesis, "kept": kept, "reason": reason})
        summary["iterations_run"] += 1

    if webhook_url:
        try:
            import requests
            delta = current_sharpe - baseline_sharpe
            kept_list = [r["hypothesis"][:60] for r in summary["results"] if r["kept"]]
            kept_text = "\n".join(f"  - {h}" for h in kept_list) or "  None this session"
            msg = (
                f"{'📈' if delta > 0 else '📊'} **AUTORESEARCH SESSION COMPLETE**\n"
                f"Iterations: {summary['iterations_run']} | Kept: {summary['kept']} | Discarded: {summary['discarded']}\n"
                f"Sharpe: {baseline_sharpe:.3f} -> {current_sharpe:.3f} ({delta:+.3f})\n\n"
                f"**Improvements adopted:**\n{kept_text}"
            )
            requests.post(webhook_url, json={"content": msg}, timeout=10)
        except Exception:
            pass

    print(f"\n AutoResearch done: {summary['kept']} kept, {summary['discarded']} discarded")
    return summary


if __name__ == "__main__":
    webhook = os.getenv("DISCORD_WEBHOOK_SAGE", "")
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ITERATIONS
    result = run(iterations=iters, webhook_url=webhook)
    print(json.dumps(result, indent=2))
