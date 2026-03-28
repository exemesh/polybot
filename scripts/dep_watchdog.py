#!/usr/bin/env python3
"""
Dependency Watchdog — weekly check for Python, pip, and brew updates.
Posts a report to Discord with what needs updating.
Runs every Sunday at 09:00 WAT via launchd.
"""

import subprocess
import sys
import os
import json
import httpx
from datetime import datetime, timezone
from pathlib import Path

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_SENTINEL",
    "https://discord.com/api/webhooks/1483222190961721465/"
    "hoybk1d89x-O-3ldDKDR1_niWPxbiw3ppdz9YTz5guivRYpu7p5fIlUV4gWDfsMZKDJy")

PYTHON_BIN     = "/opt/homebrew/bin/python3.11"
BOT_DIRS       = [
    Path.home() / "polybot",
    Path.home() / "bots/binancebot",
]

def run(cmd: list[str], cwd=None) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=cwd)
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"

def check_python_version() -> dict:
    current = run([PYTHON_BIN, "--version"])
    latest_brew = run(["brew", "info", "--json=v1", "python@3.11"])
    try:
        data = json.loads(latest_brew)
        latest = data[0]["versions"]["stable"]
    except Exception:
        latest = "unknown"
    return {"current": current, "latest_brew": latest}

def check_outdated_packages(bot_dir: Path) -> list[str]:
    req = bot_dir / "requirements.txt"
    if not req.exists():
        return []
    result = run([PYTHON_BIN, "-m", "pip", "list", "--outdated", "--format=columns"])
    lines = result.strip().splitlines()
    # Filter to only packages in requirements.txt
    req_names = set()
    for line in req.read_text().splitlines():
        name = line.split(">=")[0].split("==")[0].split(">")[0].strip().lower()
        if name and not name.startswith("#"):
            req_names.add(name)
    outdated = [l for l in lines[2:] if l.split()[0].lower() in req_names]
    return outdated

def check_git_status(bot_dir: Path) -> str:
    if not bot_dir.exists():
        return "NOT FOUND"
    status = run(["git", "status", "--short"], cwd=bot_dir)
    behind = run(["git", "rev-list", "HEAD..origin/main", "--count"], cwd=bot_dir)
    if not status and behind == "0":
        return "clean ✅"
    parts = []
    if status:
        parts.append(f"unstaged: {len(status.splitlines())} files")
    if behind and behind != "0":
        parts.append(f"{behind} commits behind origin")
    return " | ".join(parts) if parts else "clean ✅"

def check_brew_outdated() -> list[str]:
    run(["brew", "update"])
    result = run(["brew", "outdated"])
    return [l for l in result.splitlines() if l.strip()]

def post_to_discord(content: str):
    try:
        with httpx.Client(timeout=10) as client:
            client.post(DISCORD_WEBHOOK, json={"content": content})
    except Exception:
        pass

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"🔧 **DEPENDENCY WATCHDOG REPORT** — {now}\n━━━━━━━━━━━━━━━━━━━━━━━━"]

    # Python version
    py = check_python_version()
    lines.append(f"\n**Python:** {py['current']} | Brew latest: {py['latest_brew']}")

    # Git status per bot
    lines.append("\n**Git Status:**")
    for bot_dir in BOT_DIRS:
        status = check_git_status(bot_dir)
        lines.append(f"  {bot_dir.name}: {status}")

    # Outdated packages
    lines.append("\n**Outdated Packages:**")
    for bot_dir in BOT_DIRS:
        if not bot_dir.exists():
            continue
        outdated = check_outdated_packages(bot_dir)
        if outdated:
            lines.append(f"  **{bot_dir.name}:**")
            for pkg in outdated[:10]:
                lines.append(f"    {pkg}")
        else:
            lines.append(f"  {bot_dir.name}: all up to date ✅")

    # Brew outdated
    brew_outdated = check_brew_outdated()
    if brew_outdated:
        lines.append(f"\n**Homebrew:** {len(brew_outdated)} packages outdated")
        for pkg in brew_outdated[:5]:
            lines.append(f"  {pkg}")
    else:
        lines.append("\n**Homebrew:** all up to date ✅")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━\n— Apex 🔱 | Weekly dependency check complete")

    report = "\n".join(lines)
    post_to_discord(report[:1900])
    print(report)

if __name__ == "__main__":
    main()
