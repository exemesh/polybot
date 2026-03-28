#!/usr/bin/env python3
"""
Infrastructure Health Agent — daily deep check of polybot + binancebot.
Verifies: git integrity, process status, disk/memory, Python env, missing files.
Posts a concise pass/fail report to Discord Sentinel.
Runs every day at 07:30 WAT via launchd.
"""

import os
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ─── Config ──────────────────────────────────────────────────────────────────
DISCORD_WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK_SENTINEL",
    "https://discord.com/api/webhooks/1483222190961721465/"
    "hoybk1d89x-O-3ldDKDR1_niWPxbiw3ppdz9YTz5guivRYpu7p5fIlUV4gWDfsMZKDJy",
)

PYTHON_BIN = "/opt/homebrew/bin/python3.11"
HOME = Path.home()

BOTS = {
    "polybot": {
        "dir": HOME / "polybot",
        "process_pattern": "main.py",
        "required_files": [
            "main.py",
            "requirements.txt",
            ".env",
            "core/portfolio.py",
            "core/polymarket_client.py",
            "config/settings.py",
            "data/bot_control.json",
        ],
        "log": HOME / "polybot/logs/polybot.log",
        "error_log": HOME / "polybot/logs/polybot.error.log",
    },
    "binancebot": {
        "dir": HOME / "bots/binancebot",
        "process_pattern": "binancebot",
        "required_files": [
            "main.py",
            "requirements.txt",
            ".env",
        ],
        "log": HOME / "bots/binancebot/logs/binancebot.log",
        "error_log": None,
    },
}

DISK_WARN_GB = 5.0      # warn if free disk < 5 GB
MEM_WARN_PCT = 85       # warn if memory pressure > 85%


# ─── Helpers ─────────────────────────────────────────────────────────────────
def run(cmd: list[str], cwd=None, timeout: int = 30) -> tuple[str, int]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "TIMEOUT", 1
    except Exception as e:
        return f"ERROR: {e}", 1


def post_to_discord(content: str):
    try:
        with httpx.Client(timeout=10) as client:
            client.post(DISCORD_WEBHOOK, json={"content": content})
    except Exception:
        pass


# ─── Check functions ──────────────────────────────────────────────────────────
def check_disk() -> tuple[str, bool]:
    total, used, free = shutil.disk_usage("/")
    free_gb = free / (1024 ** 3)
    total_gb = total / (1024 ** 3)
    pct_used = (used / total) * 100
    ok = free_gb >= DISK_WARN_GB
    status = f"{free_gb:.1f} GB free / {total_gb:.0f} GB total ({pct_used:.0f}% used)"
    return status, ok


def check_memory() -> tuple[str, bool]:
    out, _ = run(["vm_stat"])
    lines = out.splitlines()
    stats = {}
    for line in lines:
        if ":" in line:
            k, v = line.split(":", 1)
            stats[k.strip()] = v.strip().rstrip(".")
    try:
        page_size = 4096
        free = int(stats.get("Pages free", "0").replace(",", "")) * page_size
        inactive = int(stats.get("Pages inactive", "0").replace(",", "")) * page_size
        wired = int(stats.get("Pages wired down", "0").replace(",", "")) * page_size
        active = int(stats.get("Pages active", "0").replace(",", "")) * page_size
        total = free + inactive + wired + active
        used = wired + active
        pct = int((used / total) * 100) if total else 0
        free_mb = (free + inactive) / (1024 ** 2)
        ok = pct < MEM_WARN_PCT
        return f"{free_mb:.0f} MB available ({pct}% pressure)", ok
    except Exception:
        return "unknown", True


def check_python_env() -> tuple[str, bool]:
    out, code = run([PYTHON_BIN, "--version"])
    ok = code == 0 and "Python 3.1" in out
    return out or "not found", ok


def check_git(bot_dir: Path) -> tuple[str, bool]:
    if not bot_dir.exists():
        return "directory not found", False

    # Fetch silently
    run(["git", "fetch", "--quiet"], cwd=bot_dir)

    status_out, _ = run(["git", "status", "--short"], cwd=bot_dir)
    behind_out, _ = run(["git", "rev-list", "HEAD..origin/main", "--count"], cwd=bot_dir)
    ahead_out, _ = run(["git", "rev-list", "origin/main..HEAD", "--count"], cwd=bot_dir)

    parts = []
    if status_out:
        n = len(status_out.splitlines())
        parts.append(f"{n} uncommitted file{'s' if n != 1 else ''}")
    try:
        behind = int(behind_out)
        ahead = int(ahead_out)
        if behind:
            parts.append(f"{behind} commits behind")
        if ahead:
            parts.append(f"{ahead} commits ahead")
    except ValueError:
        parts.append("git error")

    ok = not parts
    return ("clean" if ok else " | ".join(parts)), ok


def check_required_files(bot: dict) -> tuple[list[str], bool]:
    missing = []
    for f in bot["required_files"]:
        if not (bot["dir"] / f).exists():
            missing.append(f)
    return missing, len(missing) == 0


def check_process(pattern: str) -> tuple[str, bool]:
    out, code = run(["pgrep", "-f", pattern])
    running = bool(out.strip()) and code == 0
    pids = out.strip().splitlines()
    if running:
        return f"running (PID{'s' if len(pids) > 1 else ''}: {', '.join(pids[:3])})", True
    return "NOT running", False


def check_log_errors(log_path: Path) -> tuple[str, bool]:
    if not log_path or not log_path.exists():
        return "log not found", True  # not a failure — may not exist yet
    try:
        result = subprocess.run(
            ["tail", "-50", str(log_path)], capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.splitlines()
        error_kw = ["ERROR", "EXCEPTION", "Traceback", "CRITICAL", "halted", "invalid signature"]
        errors = [l for l in lines if any(k in l for k in error_kw)]
        if errors:
            return f"{len(errors)} errors in last 50 lines", False
        return "no errors in last 50 lines", True
    except Exception as e:
        return f"read error: {e}", True


def check_bot_control(bot_dir: Path) -> tuple[str, bool]:
    ctrl = bot_dir / "data/bot_control.json"
    if not ctrl.exists():
        return "not found", True
    try:
        data = json.loads(ctrl.read_text())
        enabled = data.get("trading_enabled", True)
        mode = data.get("mode", "unknown")
        reason = data.get("halt_reason")
        if not enabled:
            msg = f"HALTED ({reason or 'no reason'})"
            return msg, False
        return f"{mode} mode, trading enabled", True
    except Exception as e:
        return f"parse error: {e}", False


def check_launchd_services() -> list[tuple[str, bool]]:
    out, _ = run(["launchctl", "list"])
    services = [
        "com.polybot.trader",
        "com.polybot-interactions",
        "com.polybot.log-relay",
        "com.polybot.dep-watchdog",
        "com.polybot.infra-health",
    ]
    results = []
    for svc in services:
        loaded = svc in out
        results.append((svc, loaded))
    return results


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"🏥 **INFRA HEALTH REPORT** — {now}\n━━━━━━━━━━━━━━━━━━━━━━━━"]
    all_ok = True

    # ── System resources
    disk_status, disk_ok = check_disk()
    mem_status, mem_ok = check_memory()
    py_status, py_ok = check_python_env()

    lines.append("\n**System:**")
    lines.append(f"  {'✅' if disk_ok else '⚠️'} Disk: {disk_status}")
    lines.append(f"  {'✅' if mem_ok else '⚠️'} Memory: {mem_status}")
    lines.append(f"  {'✅' if py_ok else '🔴'} Python: {py_status}")
    if not disk_ok or not mem_ok or not py_ok:
        all_ok = False

    # ── Per-bot checks
    for bot_name, bot in BOTS.items():
        lines.append(f"\n**{bot_name}:**")

        git_status, git_ok = check_git(bot["dir"])
        lines.append(f"  {'✅' if git_ok else '⚠️'} Git: {git_status}")
        if not git_ok:
            all_ok = False

        missing, files_ok = check_required_files(bot)
        if files_ok:
            lines.append("  ✅ Files: all present")
        else:
            lines.append(f"  🔴 Files missing: {', '.join(missing)}")
            all_ok = False

        proc_status, proc_ok = check_process(bot["process_pattern"])
        lines.append(f"  {'✅' if proc_ok else '🔴'} Process: {proc_status}")
        if not proc_ok:
            all_ok = False

        log_status, log_ok = check_log_errors(bot["log"])
        lines.append(f"  {'✅' if log_ok else '⚠️'} Logs: {log_status}")
        if not log_ok:
            all_ok = False

        if bot_name == "polybot":
            ctrl_status, ctrl_ok = check_bot_control(bot["dir"])
            lines.append(f"  {'✅' if ctrl_ok else '🔴'} Control: {ctrl_status}")
            if not ctrl_ok:
                all_ok = False

    # ── launchd services
    lines.append("\n**launchd Services:**")
    svc_results = check_launchd_services()
    for svc, loaded in svc_results:
        short = svc.replace("com.polybot", "")
        lines.append(f"  {'✅' if loaded else '⚠️'} {svc}")

    # ── Summary
    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    if all_ok:
        lines.append("✅ **All systems nominal** — Apex 🔱")
    else:
        lines.append("⚠️ **Issues detected above — review required** — Apex 🔱")

    report = "\n".join(lines)
    post_to_discord(report[:1900])
    print(report)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
