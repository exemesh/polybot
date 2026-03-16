#!/usr/bin/env bash
# setup_mac_mini.sh — Bootstrap PolyBot on a Mac mini for local execution
#
# This script:
#   1. Creates a Python virtual environment at ~/polybot-env
#   2. Installs Python requirements
#   3. Creates a .env file template (if one does not already exist)
#   4. Creates a launchd plist at ~/Library/LaunchAgents/com.polybot.trader.plist
#   5. Loads the plist with launchctl so the bot starts immediately and on login
#
# Usage:
#   chmod +x scripts/setup_mac_mini.sh
#   ./scripts/setup_mac_mini.sh
#
# Requirements:
#   - macOS 12+ (Monterey or later recommended)
#   - Python 3.11+ installed (e.g. via Homebrew: brew install python@3.11)
#   - The polybot repo cloned to ~/polybot

set -euo pipefail

POLYBOT_DIR="${HOME}/polybot"
VENV_DIR="${HOME}/polybot-env"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.polybot.trader.plist"
PLIST_DEST="${HOME}/Library/LaunchAgents/com.polybot.trader.plist"
LOG_DIR="${POLYBOT_DIR}/logs"
DOT_ENV="${POLYBOT_DIR}/.env"

# ── Resolve python3 binary ───────────────────────────────────────────────────
if command -v python3.11 &>/dev/null; then
    PYTHON="python3.11"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
else
    echo "ERROR: python3 not found. Install it first: brew install python@3.11"
    exit 1
fi

echo "Using Python: $($PYTHON --version)"
echo "PolyBot directory: ${POLYBOT_DIR}"

# ── 1. Virtual environment ───────────────────────────────────────────────────
if [ ! -d "${VENV_DIR}" ]; then
    echo "[1/5] Creating virtual environment at ${VENV_DIR} ..."
    "${PYTHON}" -m venv "${VENV_DIR}"
else
    echo "[1/5] Virtual environment already exists at ${VENV_DIR}"
fi

# ── 2. Install requirements ──────────────────────────────────────────────────
echo "[2/5] Installing requirements ..."
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install -r "${POLYBOT_DIR}/requirements.txt" --quiet
echo "      Requirements installed."

# ── 3. Create .env template ──────────────────────────────────────────────────
if [ ! -f "${DOT_ENV}" ]; then
    echo "[3/5] Creating .env template at ${DOT_ENV} ..."
    cp "${POLYBOT_DIR}/.env.example" "${DOT_ENV}"
    echo "      IMPORTANT: Edit ${DOT_ENV} and fill in your secrets before trading live."
else
    echo "[3/5] .env already exists at ${DOT_ENV} — skipping."
fi

# ── 4. Create launchd plist ───────────────────────────────────────────────────
echo "[4/5] Installing launchd plist ..."
mkdir -p "${HOME}/Library/LaunchAgents"

if [ ! -f "${PLIST_SRC}" ]; then
    echo "ERROR: plist template not found at ${PLIST_SRC}"
    echo "       Make sure you are running this script from inside the polybot repo."
    exit 1
fi

# Substitute HOME path placeholders in the plist
sed "s|POLYBOT_HOME_PLACEHOLDER|${HOME}|g" "${PLIST_SRC}" > "${PLIST_DEST}"
echo "      Plist installed to ${PLIST_DEST}"

# ── 5. Load the plist ────────────────────────────────────────────────────────
echo "[5/5] Loading launchd plist ..."
# Unload first in case it was previously loaded (ignore errors)
launchctl unload "${PLIST_DEST}" 2>/dev/null || true
launchctl load -w "${PLIST_DEST}"
echo "      PolyBot launchd service loaded."

# ── Create log directory ─────────────────────────────────────────────────────
mkdir -p "${LOG_DIR}"

echo ""
echo "======================================================"
echo " PolyBot Mac mini setup complete!"
echo "======================================================"
echo ""
echo " Configuration:"
echo "   Repo:      ${POLYBOT_DIR}"
echo "   Venv:      ${VENV_DIR}"
echo "   Logs:      ${LOG_DIR}/polybot.log"
echo "   Plist:     ${PLIST_DEST}"
echo "   Runs:      every 5 minutes (StartInterval: 300)"
echo ""
echo " Next steps:"
echo "   1. Edit your secrets: nano ${DOT_ENV}"
echo "   2. Monitor logs:      tail -f ${LOG_DIR}/polybot.log"
echo "   3. Stop the bot:      launchctl unload ${PLIST_DEST}"
echo "   4. Start the bot:     launchctl load -w ${PLIST_DEST}"
echo ""
