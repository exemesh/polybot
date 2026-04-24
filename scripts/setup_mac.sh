#!/usr/bin/env bash
# Polybot v4 — one-command macOS setup.
# Creates venv, installs deps, copies .env template, installs launchd plist.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Polybot v4 setup — repo at $REPO_DIR"

# Python check
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Install via: brew install python@3.11"
    exit 1
fi
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "    python $PY_VERSION"

# venv
if [ ! -d venv ]; then
    echo "==> Creating venv"
    python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "==> Upgrading pip"
pip install --upgrade pip >/dev/null

echo "==> Installing requirements"
pip install -r requirements.txt

# .env
if [ ! -f .env ]; then
    echo "==> Creating .env from template"
    cp .env.example .env
    echo "    Edit .env to fill in credentials: nano $REPO_DIR/.env"
else
    echo "    .env already exists, skipping"
fi

# logs + data dirs
mkdir -p logs data

# launchd plist
PLIST_NAME="com.polybot.v4.plist"
DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"
if [ -f "$DEST" ]; then
    echo "    launchd plist already at $DEST — skipping. Edit manually if paths changed."
else
    echo "==> Installing launchd plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    sed "s|__REPO_DIR__|$REPO_DIR|g; s|__USER__|$USER|g" \
        launchd/com.polybot.v4.plist.template > "$DEST"
    echo "    Installed at $DEST"
    echo "    Load with: launchctl load -w $DEST"
    echo "    NOT loading automatically — test in dry-run first!"
fi

echo
echo "==> Setup complete. Next steps:"
echo "    1. nano .env  # fill in credentials"
echo "    2. ./scripts/run.sh  # dry-run"
echo "    3. After 50+ dry-run trades: ./scripts/run.sh --live"
