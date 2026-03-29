#!/usr/bin/env python3
"""
Log Relay — ships recent polybot + binancebot log tails to nanoclaw/Discord.
Runs every 5 minutes via launchd. Only posts when there are errors or new trades.

Apex reads these to monitor bots remotely without Mac mini access.
"""

import os
import re
import json
import hashlib
import httpx
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────
POLYBOT_LOG    = Path.home() / "polybot/logs/polybot.log"
BINANCE_LOG    = Path.home() / "bots/binancebot/logs/binancebot.log"
ERROR_LOG      = Path.home() / "polybot/logs/polybot.error.log"
STATE_FILE     = Path.home() / ".log_relay_state.json"
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_SENTINEL",
    "https://discord.com/api/webhooks/1483222190961721465/"
    "hoybk1d89x-O-3ldDKDR1_niWPxbiw3ppdz9YTz5guivRYpu7p5fIlUV4gWDfsMZKDJy")

LINES_TO_SHIP  = 30          # tail lines to check each cycle
ERROR_KEYWORDS = ["ERROR", "EXCEPTION", "Traceback", "halted", "invalid signature",
                  "not enough balance", "CRITICAL", "ModuleNotFoundError"]
TRADE_KEYWORDS = ["Market order placed", "orderID=", "ENTER]", "opened position"]

# ─── State (track last shipped line to avoid repeats) ─────────────────────
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))

def tail_file(path: Path, n: int = LINES_TO_SHIP) -> list[str]:
    if not path.exists():
        return []
    try:
        result = subprocess.run(["tail", f"-{n}", str(path)],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip().splitlines()
    except Exception:
        return []

def fingerprint(lines: list[str]) -> str:
    return hashlib.md5("\n".join(lines[-5:]).encode()).hexdigest()

def classify_lines(lines: list[str]) -> tuple[list[str], list[str]]:
    errors, trades = [], []
    for line in lines:
        if any(k in line for k in ERROR_KEYWORDS):
            errors.append(line)
        elif any(k in line for k in TRADE_KEYWORDS):
            trades.append(line)
    return errors, trades

def post_to_discord(content: str):
    try:
        with httpx.Client(timeout=10) as client:
            client.post(DISCORD_WEBHOOK, json={"content": content})
    except Exception:
        pass

def check_process_running(name: str) -> bool:
    try:
        result = subprocess.run(["pgrep", "-f", name],
                                capture_output=True, text=True, timeout=5)
        return bool(result.stdout.strip())
    except Exception:
        return False

def main():
    state = load_state()
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    alerts = []

    # ── Polybot logs ──────────────────────────────────────────────────────
    poly_lines = tail_file(POLYBOT_LOG)
    poly_fp = fingerprint(poly_lines) if poly_lines else ""
    if poly_lines and poly_fp != state.get("poly_fp"):
        errors, trades = classify_lines(poly_lines)
        if errors:
            err_block = "\n".join(f"  {l[-120:]}" for l in errors[-5:])
            alerts.append(f"🔴 **Polybot ERRORS** [{now}]\n```\n{err_block}\n```")
        if trades:
            trade_block = "\n".join(f"  {l[-120:]}" for l in trades[-3:])
            alerts.append(f"✅ **Polybot TRADES** [{now}]\n```\n{trade_block}\n```")
        state["poly_fp"] = poly_fp

    # ── Binancebot logs ───────────────────────────────────────────────────
    bin_lines = tail_file(BINANCE_LOG)
    bin_fp = fingerprint(bin_lines) if bin_lines else ""
    if bin_lines and bin_fp != state.get("bin_fp"):
        errors, trades = classify_lines(bin_lines)
        if errors:
            err_block = "\n".join(f"  {l[-120:]}" for l in errors[-5:])
            alerts.append(f"🔴 **BinanceBot ERRORS** [{now}]\n```\n{err_block}\n```")
        if trades:
            trade_block = "\n".join(f"  {l[-120:]}" for l in trades[-3:])
            alerts.append(f"💰 **BinanceBot TRADES** [{now}]\n```\n{trade_block}\n```")
        state["bin_fp"] = bin_fp

    # ── Process health ────────────────────────────────────────────────────
    # Polybot is a one-shot launchd job (runs every 5 min then exits).
    # Don't check for live process — check last_bot_run timestamp instead.
    # Alert only if the bot hasn't run in > 15 minutes (3 missed cycles).
    bot_control = Path.home() / "polybot/data/bot_control.json"
    try:
        ctrl = json.loads(bot_control.read_text())
        last_run_str = ctrl.get("last_bot_run") or ctrl.get("updated_at", "")
        if last_run_str:
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            last_run = _dt.fromisoformat(last_run_str.replace("Z", "+00:00"))
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=_tz.utc)
            minutes_since = (_dt.now(_tz.utc) - last_run).total_seconds() / 60
            if minutes_since > 15:
                alerts.append(
                    f"⚠️ **Polybot stalled** [{now}] — last run {minutes_since:.0f} min ago "
                    f"(expected every 5 min). launchd may have failed."
                )
    except Exception:
        pass  # bot_control not readable — skip health check

    # ── Error log check ───────────────────────────────────────────────────
    err_lines = tail_file(ERROR_LOG, n=10)
    err_fp = fingerprint(err_lines) if err_lines else ""
    if err_lines and err_fp != state.get("err_fp"):
        recent = [l for l in err_lines if l.strip()][-3:]
        if recent:
            block = "\n".join(f"  {l[-120:]}" for l in recent)
            alerts.append(f"🚨 **Polybot STDERR** [{now}]\n```\n{block}\n```")
        state["err_fp"] = err_fp

    # ── Post to Discord if anything noteworthy ────────────────────────────
    if alerts:
        for alert in alerts:
            post_to_discord(alert[:1900])

    save_state(state)

if __name__ == "__main__":
    main()
