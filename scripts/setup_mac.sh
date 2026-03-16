#!/usr/bin/env bash
# setup_mac.sh — Bootstrap PolyBot on a Mac mini using launchd (every 5 minutes)
#
# This script:
#   1. Creates a Python venv at ~/polybot/.venv
#   2. Installs requirements from requirements.txt
#   3. Copies launchd/com.polybot.trader.plist to ~/Library/LaunchAgents/
#   4. Loads the plist with launchctl so the bot starts immediately and on login
#   5. Prints confirmation and next steps
#
# Usage:
#   chmod +x scripts/setup_mac.sh
#   cd ~/polybot && ./scripts/setup_mac.sh
#
# Requirements:
#   - macOS 12+ (Monterey or later recommended)
#   - Python 3.11+ installed (e.g. via Homebrew: brew install python@3.11)
#   - The polybot repo cloned to ~/polybot
#   - A .env file at ~/polybot/.env with your secrets

set -euo pipefail

POLYBOT_DIR="${HOME}/polybot"
VENV_DIR="${POLYBOT_DIR}/.venv"
PLIST_NAME="com.polybot.trader.plist"
PLIST_SRC="${POLYBOT_DIR}/launchd/${PLIST_NAME}"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_NAME}"
LOG_DIR="${POLYBOT_DIR}/logs"
SHARED_LOG_DIR="/Users/Shared/polybot/logs"

# ── Resolve python3 binary ───────────────────────────────────────────────────
if command -v python3.11 &>/dev/null; then
    PYTHON="python3.11"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
else
    echo "ERROR: python3 not found. Install it: brew install python@3.11"
    exit 1
fi

echo "============================================================"
echo "  PolyBot Mac mini Setup"
echo "============================================================"
echo "  Python:      $($PYTHON --version)"
echo "  Repo:        ${POLYBOT_DIR}"
echo "  Venv:        ${VENV_DIR}"
echo ""

# ── 1. Create Python virtual environment ────────────────────────────────────
if [ ! -d "${VENV_DIR}" ]; then
    echo "[1/4] Creating virtual environment at ${VENV_DIR} ..."
    "${PYTHON}" -m venv "${VENV_DIR}"
    echo "      Virtual environment created."
else
    echo "[1/4] Virtual environment already exists at ${VENV_DIR} — skipping."
fi

# ── 2. Install requirements ──────────────────────────────────────────────────
echo "[2/4] Installing requirements ..."
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install -r "${POLYBOT_DIR}/requirements.txt" --quiet
echo "      Requirements installed."

# ── 3. Copy plist to LaunchAgents ────────────────────────────────────────────
echo "[3/4] Installing launchd plist ..."
mkdir -p "${HOME}/Library/LaunchAgents"
mkdir -p "${LOG_DIR}"
mkdir -p "${SHARED_LOG_DIR}"

if [ ! -f "${PLIST_SRC}" ]; then
    echo "ERROR: plist not found at ${PLIST_SRC}"
    echo "       Make sure the launchd/ directory exists in the repo."
    exit 1
fi

cp "${PLIST_SRC}" "${PLIST_DEST}"
echo "      Plist installed to ${PLIST_DEST}"

# ── 4. Load the plist with launchctl ─────────────────────────────────────────
echo "[4/4] Loading launchd service ..."
# Unload first in case it was previously installed (ignore errors)
launchctl unload "${PLIST_DEST}" 2>/dev/null || true
launchctl load -w "${PLIST_DEST}"
echo "      PolyBot launchd service loaded."

echo ""
echo "============================================================"
echo "  PolyBot Mac mini setup complete!"
echo "============================================================"
echo ""
echo "  Schedule:  Every 5 minutes (StartInterval: 300)"
echo "  Logs:      ${LOG_DIR}/polybot.log"
echo "             ${SHARED_LOG_DIR}/stdout.log"
echo "             ${SHARED_LOG_DIR}/stderr.log"
echo "  Plist:     ${PLIST_DEST}"
echo ""
echo "  Common commands:"
echo "    Stop bot:    launchctl unload ${PLIST_DEST}"
echo "    Start bot:   launchctl load -w ${PLIST_DEST}"
echo "    View logs:   tail -f ${LOG_DIR}/polybot.log"
echo ""
echo "  Make sure your secrets are set in: ${POLYBOT_DIR}/.env"
echo ""
